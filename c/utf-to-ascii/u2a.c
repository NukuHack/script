/*
 * u2a - make any program's output ASCII-only, without touching its input.
 *
 * USAGE
 *   u2a [-d] [-r STR] < in > out          filter: stdin -> stdout
 *   cmd | u2a                             same, in a pipeline
 *   u2a [opts] [--] CMD [ARGS...]         wrapper: run CMD, convert its output
 *   u2a [opts] -c 'SHELL COMMAND'         same, via your interactive shell
 *   u2a --init                            print shell setup (see ALIASES)
 *
 *   -d      delete characters with no ASCII form (default: replace with ?)
 *   -r STR  replace them with STR (ASCII, max 8 chars) instead of "?"
 *   -c STR  run STR through your interactive shell ($SHELL -i -c STR), so
 *           aliases and shell functions work. Whatever your rc files print
 *           on startup (welcome banners, ...) is hidden.
 *   -x      run CMD directly: no shell lookup, no rc files (fastest start)
 *   -h      help
 *
 * EXAMPLES
 *   u2a python3 app.py        interactive Python app, ASCII output
 *   u2a bash                  a whole shell session
 *   u2a ssh user@host         remote session
 *   u2a myalias --flag        aliases, functions, builtins work too
 *   u2a ls > out.txt          redirected output has plain \n line endings
 *
 * WRAPPER MODE (POSIX only: Linux, macOS, BSD, WSL)
 *   CMD runs on a pseudo-terminal, so it behaves exactly as if it were
 *   attached to your terminal: prompts appear immediately (no pipe
 *   buffering), line editing / colours / Ctrl-C / Ctrl-D / window resizes
 *   all work. Your keystrokes are forwarded byte-for-byte (the terminal is
 *   put in raw mode); only CMD's output goes through the converter.
 *   Exit status is CMD's (126 = cannot execute, 127 = not found). If stdin
 *   is not a terminal it is forwarded followed by an end-of-file (^D).
 *   Caveat: replacing one character by several ("..." for the ellipsis)
 *   shifts columns; full-screen programs (vim, htop) may redraw slightly off.
 *   Best for REPLs, prompts, CLIs and logs.
 *
 * HOW THE FIRST WORD IS RESOLVED (like typing it in your shell)
 *   - contains a '/'             -> run directly
 *   - alias, function, builtin or keyword in your shell (even when a program
 *     of the same name exists, e.g. /usr/bin/fg) -> run through the shell
 *   - program on $PATH           -> run directly
 *   - not found anywhere         -> clear error, exit 127. With the setup
 *     below it also tells a typo from a recursive alias (alias app="app").
 *
 * ALIASES
 *   Aliases/functions from your rc files (~/.bashrc, ~/.zshrc) always work.
 *   Aliases typed into a running shell session are invisible to a child
 *   process, unless your shell hands them over. One line in your rc file:
 *       eval "$(/path/to/u2a --init)"
 *   defines  alias u2a='U2A_ALIASES="$(alias)" /path/to/u2a '  which (1) makes
 *   your shell expand aliases before u2a starts (trailing space), and (2)
 *   passes the live alias table along, so `u2a -c '...'` can use session
 *   aliases too and errors can be diagnosed precisely. Tested with bash.
 *
 * FILTER MODE works on every OS with a C compiler (incl. Windows).
 * BUILD:  cc -O2 -o u2a u2a.c        (needs u2a.h next to it)
 * The conversion itself lives in u2a.h and can be embedded in other programs.
 */
#ifdef __linux__
#define _GNU_SOURCE
#endif
#define U2A_IMPLEMENTATION
#include "u2a.h"
#include <errno.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#include <io.h>
#include <fcntl.h>
#define RD(f, b, n) _read(f, b, (unsigned)(n))
#define WR(f, b, n) _write(f, b, (unsigned)(n))
#else
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <termios.h>
#include <unistd.h>
#define RD(f, b, n) read(f, b, n)
#define WR(f, b, n) write(f, b, n)
#endif

#define CHUNK 4096
#define MARK  "@@u2a-ready@@"           /* printed just before a shell command */
#define PRE   "printf '\\n" MARK "\\n'\n"
#define SETUP "eval \"$(/path/to/u2a --init)\""
static char obuf[U2A_BOUND(CHUNK)];

/* write() all of b, retrying on partial writes / EINTR; 0 = ok */
static int wall(int fd, const char *b, size_t n)
{
    while (n) {
        long k = (long)WR(fd, b, n);
        if (k < 0 && errno == EINTR) continue;
        if (k <= 0) return -1;
        b += k;
        n -= (size_t)k;
    }
    return 0;
}

static int usage(FILE *fp, int rc)
{
    fputs("usage: u2a [-d] [-r STR] [-x] [--] [COMMAND [ARGS...]]\n"
          "       u2a [-d] [-r STR] -c 'SHELL COMMAND'\n"
          "       u2a --init\n"
          "  no COMMAND : filter stdin to stdout, converting UTF-8 to ASCII\n"
          "  COMMAND    : run it on a pty, convert its output, pass input through\n"
          "  -c STR     run STR via $SHELL -i -c (aliases and functions work)\n"
          "  -x         run COMMAND directly, no shell lookup\n"
          "  -d         delete unknown characters\n"
          "  -r STR     replace unknown characters with STR (default ?)\n"
          "  --init     print the shell setup line for session aliases:\n"
          "             " SETUP "\n", fp);
    return rc;
}

/* ---- filter mode: stdin -> stdout (streaming, works on any OS) ---------- */
static int filter(const char *unk)
{
    static char in[CHUNK];
    u2a cv;
    long n;

#ifdef _WIN32
    _setmode(0, _O_BINARY);
    _setmode(1, _O_BINARY);
#endif
    u2a_init(&cv, unk);
    for (;;) {
        n = (long)RD(0, in, CHUNK);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) break;
        if (wall(1, obuf, u2a_conv(&cv, in, (size_t)n, obuf))) return 1;
    }
    return wall(1, obuf, u2a_flush(&cv, obuf)) ? 1 : 0;
}

#ifdef _WIN32
/* ---- Windows: wrapper mode needs a POSIX pty ----------------------------- */
static int run_cmd(const char *unk, char *cstr, int direct, char **av, int n)
{
    (void)unk; (void)cstr; (void)direct; (void)av; (void)n;
    fputs("u2a: running commands needs a POSIX pty; not supported on Windows.\n"
          "     Use filter mode (cmd | u2a) or run it under WSL.\n", stderr);
    return 1;
}
static int do_init(const char *a0)
{
    (void)a0;
    fputs("u2a: --init is for POSIX shells.\n", stderr);
    return 1;
}
#else
/* ======================= POSIX: pty wrapper =============================== */

static int sp[2];                       /* self-pipe: signal -> main loop */

static void on_sig(int sig)
{
    int e = errno;
    char c = (char)sig;
    if (write(sp[1], &c, 1) < 0) { /* pipe full: a wakeup is already queued */ }
    errno = e;
}

static void sync_size(int from, int to)
{
    struct winsize ws;
    if (ioctl(from, TIOCGWINSZ, &ws) == 0) ioctl(to, TIOCSWINSZ, &ws);
}

/* convert b[0..n) and write to stdout; 1 = ok, -1 = stdout failed */
static int emit_out(u2a *cv, const char *b, size_t n)
{
    while (n) {
        size_t k = n > CHUNK ? CHUNK : n;
        if (wall(1, obuf, u2a_conv(cv, b, k, obuf))) return -1;
        b += k;
        n -= k;
    }
    return 1;
}

/* Startup output of a shell command is held back until MARK shows up, then
 * dropped (see PRE). If the marker never comes, it is all released at exit. */
static int ready = 1;
static char hold[CHUNK * 16];
static size_t hn;

static char *find(char *h, size_t n, const char *k)
{
    size_t kl = strlen(k), i;
    for (i = 0; i + kl <= n; i++)
        if (!memcmp(h + i, k, kl)) return h + i;
    return NULL;
}

/* master -> converter -> stdout. 1 = ok, 0 = pty closed, -1 = stdout failed */
static int pump(int m, u2a *cv)
{
    static char b[CHUNK];
    char *q, *e;
    int r;
    ssize_t n = read(m, b, sizeof b);
    if (n < 0 && (errno == EINTR || errno == EAGAIN)) return 1;
    if (n <= 0) return 0;               /* EIO/0: slave side closed */
    if (ready) return emit_out(cv, b, (size_t)n);

    if (hn + (size_t)n > sizeof hold) {             /* too much: give up */
        ready = 1;
        r = emit_out(cv, hold, hn);
        hn = 0;
        return r < 0 ? r : emit_out(cv, b, (size_t)n);
    }
    memcpy(hold + hn, b, (size_t)n);
    hn += (size_t)n;
    if (!(q = find(hold, hn, MARK))) return 1;
    if (!(e = memchr(q, '\n', hn - (size_t)(q - hold)))) return 1;
    ready = 1;                                      /* drop everything before */
    r = emit_out(cv, e + 1, hn - (size_t)(e + 1 - hold));
    hn = 0;
    return r;
}

/* ---- finding programs and asking the user's shell ----------------------- */

static int is_dir(const char *f)
{
    struct stat st;
    return stat(f, &st) == 0 && S_ISDIR(st.st_mode);
}

/* executable `name` somewhere on $PATH? full path goes to out */
static int find_exe(const char *name, char *out, size_t sz)
{
    const char *path = getenv("PATH"), *e, *d;
    int l;

    while (path && *path) {
        e = strchr(path, ':');
        if (!e) e = path + strlen(path);
        l = (int)(e - path);
        d = path;
        if (!l) { d = "."; l = 1; }                 /* empty entry = cwd */
        snprintf(out, sz, "%.*s/%s", l, d, name);
        if (access(out, X_OK) == 0 && !is_dir(out)) return 1;
        path = *e ? e + 1 : e;
    }
    return 0;
}

static int in_path(const char *name)
{
    char b[4096];
    return find_exe(name, b, sizeof b);
}

static const char *user_shell(void)
{
    const char *s = getenv("SHELL");
    return s && *s && access(s, X_OK) == 0 ? s : "/bin/sh";
}

static int safe_ch(int c)
{
    return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'z') ||
           (c >= 'A' && c <= 'Z') || (c && strchr("_.+:@%-", c));
}

static int safe_word(const char *s, size_t n)
{
    size_t i;
    for (i = 0; i < n; i++)
        if (!safe_ch((unsigned char)s[i])) return 0;
    return n > 0;
}

enum { R_PROG, R_SHELL, R_NONE, R_UNK };

/* Ask the user's interactive shell what `name` is (`command -V`).
 *   R_PROG  plain program      R_SHELL alias/function/builtin/keyword
 *   R_NONE  nothing by that name   R_UNK  could not tell (unknown shell etc.)
 * noalias: ignore aliases, i.e. does anything real exist behind the name? */
static int resolve(const char *name, int noalias)
{
    const char *sh = user_shell(), *b;
    char cmd[1024], buf[8192], *q, *e;
    size_t n, l = strlen(name);
    FILE *fp;

    if (!safe_word(name, l) || strchr(sh, '\'')) return R_UNK;
    b = strrchr(sh, '/');
    b = b ? b + 1 : sh;
    if (strcmp(b, "bash") && strcmp(b, "zsh") && strcmp(b, "sh") &&
        strcmp(b, "dash") && strcmp(b, "ksh") && strcmp(b, "mksh"))
        return R_UNK;                               /* no `command -V` assumed */
    snprintf(cmd, sizeof cmd,
             "'%s' -i -c \"printf '\\n%s\\n'; %scommand -V %s\" 2>/dev/null </dev/null",
             sh, MARK, noalias ? "unalias -a 2>/dev/null; " : "", name);
    if (!(fp = popen(cmd, "r"))) return R_UNK;
    n = fread(buf, 1, sizeof buf - 1, fp);
    pclose(fp);
    buf[n] = 0;
    if (!(q = strstr(buf, MARK)) || !(e = strchr(q, '\n')))
        return R_UNK;                               /* shell did not run */
    e++;
    if (!strncmp(e, name, l) && !strncmp(e + l, " is ", 4)) {
        e += l + 4;
        return (*e == '/' || !strncmp(e, "hashed (", 8)) ? R_PROG : R_SHELL;
    }
    return R_NONE;
}

/* ---- live alias table handed over by the setup alias (env U2A_ALIASES) --- */

static const char *atab;                /* NULL = setup not in use */

/* single-quote decoding as printed by bash; returns the end of the value */
static const char *unquote(const char *s, char *o, size_t sz)
{
    int q = 0;
    size_t n = 0;
    for (; *s && (q || *s != '\n'); s++) {
        if (q) {
            if (*s == '\'') q = 0;
            else if (n + 1 < sz) o[n++] = *s;
        } else if (*s == '\'') q = 1;
        else if (*s == '\\' && s[1]) { s++; if (n + 1 < sz) o[n++] = *s; }
        else if (n + 1 < sz) o[n++] = *s;
    }
    o[n] = 0;
    return s;
}

/* next alias from the table; understands bash ("alias n='v'") and zsh ("n=v") */
static int alias_next(const char **p, char *name, size_t ns, char *val, size_t vs)
{
    const char *s = *p, *line, *e;
    size_t l;

    while (s && *s) {
        line = s;
        if (!strncmp(line, "alias ", 6)) line += 6;
        for (e = line; *e && *e != '=' && *e != '\n'; e++) { }
        l = (size_t)(e - line);
        if (*e != '=' || l >= ns || !safe_word(line, l)) {      /* skip line */
            s = strchr(s, '\n');
            s = s ? s + 1 : "";
            continue;
        }
        memcpy(name, line, l);
        name[l] = 0;
        s = unquote(e + 1, val, vs);
        if (*s == '\n') s++;
        *p = s;
        return 1;
    }
    *p = s;
    return 0;
}

static int alias_get(const char *name, char *val, size_t vs)
{
    const char *p = atab;
    char n[256];
    while (p && alias_next(&p, n, sizeof n, val, vs))
        if (!strcmp(n, name)) return 1;
    return 0;
}

/* first command word of an alias value, skipping VAR=value prefixes */
static void first_word(const char *v, char *o, size_t sz)
{
    size_t n;
    for (;;) {
        while (*v == ' ' || *v == '\t') v++;
        for (n = 0; v[n] && !strchr(" \t;|&<>()", v[n]); n++) { }
        if (n > 1 && memchr(v, '=', n) && v[0] != '=' && !(v[0] >= '0' && v[0] <= '9')) {
            v += n;
            continue;
        }
        break;
    }
    if (n >= sz) n = sz - 1;
    memcpy(o, v, n);
    o[n] = 0;
}

/* Follow alias `name` -> ... Returns 1 if it ends in a loop (self-reference or
 * cycle). `end` = last name reached, `path` = "a -> b -> c". */
static int alias_chain(const char *name, char *end, size_t sz, char *path, size_t psz)
{
    char val[1024], w[256], seen[16][256];
    int n = 0, i;

    snprintf(end, sz, "%s", name);
    snprintf(path, psz, "%s", name);
    snprintf(seen[n++], 256, "%s", name);
    while (alias_get(end, val, sizeof val)) {
        first_word(val, w, sizeof w);
        if (!*w) return 0;
        if (strlen(path) + strlen(w) + 5 < psz) {
            strcat(path, " -> ");
            strcat(path, w);
        }
        if (!strcmp(w, end)) return 1;                       /* a -> a */
        for (i = 0; i < n; i++)
            if (!strcmp(seen[i], w)) {                       /* a -> b -> a */
                snprintf(end, sz, "%s", w);
                return 1;
            }
        if (n >= 16) return 0;
        snprintf(seen[n++], 256, "%s", w);
        snprintf(end, sz, "%s", w);
    }
    return 0;
}

enum { P_DIRECT, P_SHELL, P_FAIL };

/* Decide how to run first word `w`; prints the error itself on P_FAIL. */
static int plan(const char *w)
{
    char val[1024], end[256], path[1024];
    const char *a;
    int loop, r, arrows = 0;

    if (alias_get(w, val, sizeof val)) {            /* alias in live session */
        loop = alias_chain(w, end, sizeof end, path, sizeof path);
        if (in_path(end) || resolve(end, 1) != R_NONE) return P_SHELL;
        for (a = path; (a = strstr(a, "->")); a += 2) arrows++;
        if (loop && arrows == 1)
            fprintf(stderr, "u2a: '%s' is an alias for itself (recursion) and no "
                    "program, function or builtin named '%s' exists.\n", w, w);
        else if (loop)
            fprintf(stderr, "u2a: alias loop (%s); no program, function or "
                    "builtin named '%s' exists.\n", path, end);
        else
            fprintf(stderr, "u2a: alias '%s' leads to '%s' (%s), which does not "
                    "exist.\n", w, end, path);
        return P_FAIL;
    }
    if (in_path(w))                                 /* program, unless the */
        return resolve(w, 0) == R_SHELL ? P_SHELL : P_DIRECT;  /* shell shadows it */
    r = resolve(w, 0);
    if (r != R_NONE) return P_SHELL;                /* alias/function/unsure */
    fprintf(stderr, "u2a: %s: command not found", w);
    if (!atab)                                      /* only if it could help */
        fputs(" (if it is an alias from this shell session, see: u2a -h)", stderr);
    fputc('\n', stderr);
    return P_FAIL;
}

/* Shell command line: PRE, the live aliases re-defined safely, then the
 * command. With av: av[0] verbatim if it looks like a plain name (so an alias
 * can expand), the rest single-quoted so spaces and quotes survive. */
static char *build_line(const char *cstr, char **av, int n)
{
    size_t len = strlen(PRE) + 1, alen = atab ? strlen(atab) : 0;
    char *s, *o, name[256], val[4096];
    const char *p, *a;
    int i;

    if (alen > 100000) alen = 0;
    len += alen * 4 + 64;
    if (cstr) len += strlen(cstr);
    for (i = 0; i < n; i++) len += 4 * strlen(av[i]) + 3;
    if (!(s = malloc(len))) return NULL;
    strcpy(s, PRE);
    o = s + strlen(PRE);

    for (p = alen ? atab : NULL; p && alias_next(&p, name, sizeof name, val, sizeof val); ) {
        o += sprintf(o, "alias %s='", name);        /* re-quoted: no injection */
        for (a = val; *a; a++) {
            if (*a == '\'') { memcpy(o, "'\\''", 4); o += 4; }
            else *o++ = *a;
        }
        *o++ = '\'';
        *o++ = '\n';
    }
    if (cstr) strcpy(o, cstr);
    for (i = 0; !cstr && i < n; i++) {
        if (i) *o++ = ' ';
        if (i == 0 && safe_word(av[0], strlen(av[0]))) {
            strcpy(o, av[0]);
            o += strlen(av[0]);
            continue;
        }
        *o++ = '\'';
        for (a = av[i]; *a; a++) {
            if (*a == '\'') { memcpy(o, "'\\''", 4); o += 4; }
            else *o++ = *a;
        }
        *o++ = '\'';
    }
    if (!cstr) *o = 0;
    return s;
}

/* ---- run a command on a pty -------------------------------------------- */
static int run(const char *unk, char **cmd, int wait)
{
    struct termios orig, raw;
    struct winsize ws;
    struct sigaction sa;
    struct pollfd p[3];
    char kb[1024], sg[16], *sn;
    int m, s, r, k, e, st = 0, reaped = 0, in_open = 1;
    int tty = isatty(0), out_tty = isatty(1), have_ws = 0, tfd = -1;
    ssize_t n, j;
    pid_t pid;
    u2a cv;

    ready = !wait;
    if (tty && tcgetattr(0, &orig) != 0) tty = 0;
    if (tty && tcgetpgrp(0) != getpgrp()) {         /* background job: leave */
        tty = 0;                                    /* the terminal alone    */
        in_open = 0;
    }
    for (k = 0; k < 3 && tfd < 0; k++)              /* any terminal gives size */
        if (isatty(k) && ioctl(k, TIOCGWINSZ, &ws) == 0) { tfd = k; have_ws = 1; }

    m = posix_openpt(O_RDWR | O_NOCTTY);
    if (m < 0 || grantpt(m) < 0 || unlockpt(m) < 0 || !(sn = ptsname(m))) {
        perror("u2a: cannot open pty");
        return 1;
    }
    if (pipe(sp) < 0) { perror("u2a: pipe"); return 1; }
    fcntl(sp[0], F_SETFL, O_NONBLOCK);
    fcntl(sp[1], F_SETFL, O_NONBLOCK);

    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_sig;
    sigemptyset(&sa.sa_mask);
    sigaction(SIGWINCH, &sa, NULL);
    sigaction(SIGCHLD, &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGHUP, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);                   /* forwarded to the child */
    signal(SIGPIPE, SIG_IGN);

    pid = fork();
    if (pid < 0) { perror("u2a: fork"); return 1; }
    if (pid == 0) {                                 /* child */
        signal(SIGPIPE, SIG_DFL);
        setsid();                                   /* new session */
        s = open(sn, O_RDWR);                       /* -> controlling tty */
        if (s < 0) { perror("u2a: open pty slave"); _exit(127); }
#ifdef TIOCSCTTY
        ioctl(s, TIOCSCTTY, 0);
#endif
        if (tty) tcsetattr(s, TCSANOW, &orig);      /* same tty settings */
        if (!out_tty) {                             /* redirected: plain \n */
            struct termios t;
            if (tcgetattr(s, &t) == 0) {
                t.c_oflag &= ~(tcflag_t)ONLCR;
                tcsetattr(s, TCSANOW, &t);
            }
        }
        if (have_ws) ioctl(s, TIOCSWINSZ, &ws);     /* same size */
        dup2(s, 0); dup2(s, 1); dup2(s, 2);
        if (s > 2) close(s);
        close(m); close(sp[0]); close(sp[1]);
        execvp(cmd[0], cmd);
        e = errno;
        perror(cmd[0]);
        _exit(e == ENOENT ? 127 : 126);
    }

    if (tty) {                      /* raw: forward every keystroke untouched */
        raw = orig;
        raw.c_iflag &= ~(tcflag_t)(BRKINT | ICRNL | INPCK | ISTRIP | IXON);
        raw.c_oflag &= ~(tcflag_t)OPOST;
        raw.c_cflag |= CS8;
        raw.c_lflag &= ~(tcflag_t)(ECHO | ICANON | IEXTEN | ISIG);
        raw.c_cc[VMIN] = 1;
        raw.c_cc[VTIME] = 0;
        tcsetattr(0, TCSAFLUSH, &raw);
    }

    u2a_init(&cv, unk);
    for (;;) {
        p[0].fd = in_open ? 0 : -1; p[0].events = POLLIN;
        p[1].fd = m;                p[1].events = POLLIN;
        p[2].fd = sp[0];            p[2].events = POLLIN;
        if (poll(p, 3, -1) < 0) { if (errno == EINTR) continue; break; }

        if (p[2].revents & POLLIN) {                    /* signals */
            n = read(sp[0], sg, sizeof sg);
            for (j = 0; j < n; j++) {
                if (sg[j] == SIGWINCH) { if (tfd >= 0) sync_size(tfd, m); }
                else if (sg[j] == SIGCHLD) {
                    if (waitpid(pid, &st, WNOHANG) == pid) reaped = 1;
                } else kill(pid, sg[j]);                /* INT / TERM / HUP */
            }
            if (reaped) {                               /* flush what's left */
                p[1].fd = m;
                while (poll(&p[1], 1, 0) > 0 && pump(m, &cv) > 0) { }
                break;
            }
        }
        if (p[1].revents & (POLLIN | POLLHUP | POLLERR)) {   /* child output */
            r = pump(m, &cv);
            if (r < 0) kill(pid, SIGHUP);
            if (r <= 0) break;
        }
        if (p[0].revents & (POLLIN | POLLHUP | POLLERR)) {   /* user input */
            n = read(0, kb, sizeof kb);
            if (n > 0) wall(m, kb, (size_t)n);
            else if (n == 0 || (errno != EINTR && errno != EAGAIN)) {
                in_open = 0;
                if (!tty) wall(m, "\004", 1);           /* piped input: send ^D */
            }
        }
    }

    if (!ready && hn) emit_out(&cv, hold, hn);          /* marker never came */
    wall(1, obuf, u2a_flush(&cv, obuf));
    while (!reaped && waitpid(pid, &st, 0) < 0 && errno == EINTR) { }
    if (tty) tcsetattr(0, TCSAFLUSH, &orig);
    close(m);
    return WIFEXITED(st) ? WEXITSTATUS(st) :
           WIFSIGNALED(st) ? 128 + WTERMSIG(st) : 1;
}

/* Run av[0..n) (or the shell string cstr) the way the user's shell would. */
static int run_cmd(const char *unk, char *cstr, int direct, char **av, int n)
{
    char *line, *shv[5];
    int rc, p = P_SHELL;

    atab = getenv("U2A_ALIASES");
    if (!cstr) {
        if (!*av[0]) { fputs("u2a: empty command\n", stderr); return 2; }
        p = direct || strchr(av[0], '/') ? P_DIRECT : plan(av[0]);
        if (p == P_FAIL) return 127;
        if (p == P_DIRECT) return run(unk, av, 0);
    }
    if (!(line = build_line(cstr, av, n))) return 1;
    shv[0] = (char *)user_shell();
    shv[1] = "-i"; shv[2] = "-c"; shv[3] = line; shv[4] = NULL;
    rc = run(unk, shv, 1);
    free(line);
    if (rc == 127 && !atab)
        fputs("u2a: (exit 127. If that was an alias from this shell session, "
              "see: u2a -h)\n", stderr);
    return rc;
}

/* print the rc-file line: expands aliases first and hands the live table over */
static int do_init(const char *a0)
{
    char exe[4096], *real;
    const char *path = a0, *c;

    if (!strchr(a0, '/')) {
        if (!find_exe(a0, exe, sizeof exe)) { fputs("u2a: cannot locate myself\n", stderr); return 1; }
        path = exe;
    }
    if (!(real = realpath(path, NULL))) { perror("u2a: realpath"); return 1; }
    for (c = real; *c; c++)
        if (strchr("'\"$`\\\n", *c)) {
            fputs("u2a: install path has characters that cannot be quoted safely\n", stderr);
            return 1;
        }
    printf("alias u2a=\"U2A_ALIASES=\\\"\\$(alias)\\\" '%s' \"\n", real);
    free(real);
    return 0;
}
#endif

int main(int argc, char **argv)
{
    const char *unk = "?", *c;
    char *cstr = NULL;
    int i, direct = 0;

    for (i = 1; i < argc && argv[i][0] == '-' && argv[i][1]; i++) {
        if (!strcmp(argv[i], "--")) { i++; break; }
        else if (!strcmp(argv[i], "-d")) unk = "";
        else if (!strcmp(argv[i], "-x")) direct = 1;
        else if (!strcmp(argv[i], "-r") && i + 1 < argc) unk = argv[++i];
        else if (!strcmp(argv[i], "-c") && i + 1 < argc) cstr = argv[++i];
        else if (!strcmp(argv[i], "--init")) return do_init(argv[0]);
        else if (!strcmp(argv[i], "-h") || !strcmp(argv[i], "--help")) return usage(stdout, 0);
        else return usage(stderr, 2);
    }
    for (c = unk; *c; c++)
        if ((unsigned char)*c >= 0x80 || (c - unk) >= U2A_UNK_MAX) {
            fprintf(stderr, "u2a: -r STR must be ASCII, at most %d characters\n", U2A_UNK_MAX);
            return 2;
        }
    return cstr || i < argc ? run_cmd(unk, cstr, direct, argv + i, argc - i) : filter(unk);
}
