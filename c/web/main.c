/*
 * Tiny static file server (POSIX: Linux, macOS, *BSD, Cygwin).
 *
 * Build: cc -std=c11 -O2 -Wall -Wextra -o server main.c
 * Usage: ./server [port=8080] [docroot=www] [bind_addr=127.0.0.1]
 *        ./server 8080 www 0.0.0.0     # listen on all interfaces
 *
 * Requires only standard C11 + POSIX.1-2008 (SUSv4); no GNU extensions.
 * Native Windows (MSVC, MinGW) is rejected below with a single #error,
 * since it lacks the BSD socket API and the fork() model this server uses.
 * Cygwin is allowed through because it provides both.
 */
#define _XOPEN_SOURCE 700       /* SUSv4: POSIX.1-2008 + XSI (realpath etc.) */
#define _POSIX_C_SOURCE 200809L

#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <strings.h>
#include <ctype.h>
#include <errno.h>
#include <limits.h>
#include <signal.h>

#if defined(_WIN32) && !defined(__CYGWIN__)
#  error "unsupported platform: this server needs POSIX sockets and fork()"
#endif

#include <sys/types.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <fcntl.h>
#include <unistd.h>

#define KB 1024
#define MAX_CLIENTS 64       /* concurrent connections (one process each) */
#define IO_TIMEOUT_SEC 5     /* max idle time on a single read/write      */
#define TOTAL_TIMEOUT_SEC 30 /* max lifetime of one connection            */

#ifndef PATH_MAX
#  define PATH_MAX 4096
#endif

#define MAX_URI 1024U        /* longest URI we will accept (bytes)        */

static volatile sig_atomic_t children = 0;

static void on_sigchld(int sig) {
    (void)sig;
    int saved = errno;
    while (waitpid(-1, NULL, WNOHANG) > 0) {
        if (children > 0) children--;
    }
    errno = saved;
}

/* ---------- small helpers ---------- */

static const struct { const char *ext, *type; } types[] = {
    {".html", "text/html; charset=utf-8"},
    {".htm",  "text/html; charset=utf-8"},
    {".css",  "text/css; charset=utf-8"},
    {".js",   "text/javascript; charset=utf-8"},
    {".json", "application/json"},
    {".txt",  "text/plain; charset=utf-8"},
    {".svg",  "image/svg+xml"},
    {".png",  "image/png"},
    {".jpg",  "image/jpeg"},
    {".jpeg", "image/jpeg"},
    {".gif",  "image/gif"},
    {".ico",  "image/x-icon"},
    {".webp", "image/webp"},
    {".pdf",  "application/pdf"},
};

static const char *content_type(const char *name) {
    const char *dot = strrchr(name, '.');
    if (dot)
        for (size_t i = 0; i < sizeof(types) / sizeof(*types); i++)
            if (strcasecmp(dot, types[i].ext) == 0) return types[i].type;
    return "application/octet-stream";
}

/* write everything, handling partial writes and EINTR */
static int send_all(int fd, const void *buf, size_t len) {
    const char *p = buf;
    while (len > 0) {
        ssize_t n = write(fd, p, len);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return -1;      /* error or timeout: give up */
        p   += (size_t)n;
        len -= (size_t)n;
    }
    return 0;
}

static void reply_error(int client, int code, const char *reason, const char *extra) {
    char body[128], head[512];
    int bl = snprintf(body, sizeof body, "<h1>%d %s</h1>\n", code, reason);
    if (bl < 0) return;
    if ((size_t)bl >= sizeof body) bl = (int)(sizeof body - 1);
    int hl = snprintf(head, sizeof head,
            "HTTP/1.0 %d %s\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "Content-Length: %d\r\n"
            "X-Content-Type-Options: nosniff\r\n"
            "%s"
            "Connection: close\r\n"
            "\r\n", code, reason, bl, extra);
    if (hl <= 0) return;
    if ((size_t)hl >= sizeof head) hl = (int)(sizeof head - 1);
    if (send_all(client, head, (size_t)hl) == 0)
        send_all(client, body, (size_t)bl);
}

/* returns 0..15 for a hex digit, -1 otherwise */
static int hexval(unsigned char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/* in-place %XX decode; rejects bad escapes and control chars (incl. %00, %0d, %0a) */
static int url_decode(char *s) {
    char *o = s;
    for (; *s; s++) {
        unsigned char c = (unsigned char)*s;
        if (c == '%') {
            int hi = isxdigit((unsigned char)s[1]) ? hexval((unsigned char)s[1]) : -1;
            int lo = isxdigit((unsigned char)s[2]) ? hexval((unsigned char)s[2]) : -1;
            if (hi < 0 || lo < 0) return -1;
            c = (unsigned char)((hi << 4) | lo);
            s += 2;
        }
        if (c < 0x20 || c == 0x7f) return -1;
        *o++ = (char)c;
    }
    *o = '\0';
    return 0;
}

/*
 * Resolve rel inside root and open it (regular files only).
 * Uses realpath(.., NULL) so we get the canonical path with malloc'd
 * storage (POSIX.1-2008), then refuses anything that doesn't land
 * strictly inside root.  Symlinks pointing outside root are rejected.
 * Returns fd or -1.
 */
static int open_in_root(const char *root, size_t rlen, const char *rel, struct stat *st) {
    char   full[PATH_MAX + MAX_URI + 16];
    size_t rel_len = strlen(rel);

    /* root is at most PATH_MAX-1 bytes, rel at most MAX_URI+10, so this
     * can never fire, but the guard makes the memcpy obviously safe. */
    if (rlen + rel_len + 1 > sizeof full) return -1;
    memcpy(full,      root, rlen);
    memcpy(full + rlen, rel, rel_len + 1);          /* includes NUL */

    char *real = realpath(full, NULL);              /* POSIX.1-2008 */
    if (!real) return -1;

    int fd = -1;
    if (strncmp(real, root, rlen) == 0 && real[rlen] == '/') {
        fd = open(real, O_RDONLY | O_NOFOLLOW);
        if (fd >= 0) {
            if (fstat(fd, st) < 0 || !S_ISREG(st->st_mode)) {
                close(fd);
                fd = -1;
            }
        }
    }
    free(real);
    return fd;
}

/* ---------- one connection ---------- */

static void handle_client(int client, const char *root, size_t rlen) {
    char   req[8 * KB];
    size_t len = 0;
    int    complete = 0;

    while (len < sizeof(req) - 1) {
        ssize_t n = recv(client, req + len, sizeof(req) - 1 - len, 0);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return;                         /* closed, error or timeout */
        len += (size_t)n;
        req[len] = '\0';
        if (strstr(req, "\r\n\r\n") || strstr(req, "\n\n")) { complete = 1; break; }
    }
    if (!complete) { reply_error(client, 431, "Request Header Fields Too Large", ""); return; }

    char method[8], path[MAX_URI + 1], version[16];
    /* %1024s writes <= 1024 chars + NUL into path[1025] — exactly fits. */
    if (sscanf(req, "%7s %1024s %15s", method, path, version) != 3 ||
        strncmp(version, "HTTP/", 5) != 0) {
        reply_error(client, 400, "Bad Request", "");
        return;
    }
    if (strlen(path) >= MAX_URI) { reply_error(client, 414, "URI Too Long", ""); return; }

    int head_only = strcmp(method, "HEAD") == 0;
    if (!head_only && strcmp(method, "GET") != 0) {
        reply_error(client, 405, "Method Not Allowed", "Allow: GET, HEAD\r\n");
        return;
    }

    char *q = strpbrk(path, "?#");                  /* drop query / fragment */
    if (q) *q = '\0';
    if (path[0] != '/' || url_decode(path) < 0) { reply_error(client, 400, "Bad Request", ""); return; }

    /* no ".." and no hidden files (.git, .env, ...) anywhere in the path */
    if (strstr(path, "/.")) { reply_error(client, 404, "Not Found", ""); return; }

    /* rel = path (+ "index.html" if the path ends in '/')
     * After the 414 check above, pl <= MAX_URI-1 <= 1023, so the worst
     * output is 1023 + 10 = 1033 chars + NUL = 1034 bytes into 1035. */
    char   rel[MAX_URI + sizeof("index.html")];
    size_t pl = strlen(path);
    int    rl = snprintf(rel, sizeof rel, "%s%s",
                         path, path[pl - 1] == '/' ? "index.html" : "");
    if (rl < 0 || (size_t)rl >= sizeof rel) {
        reply_error(client, 500, "Internal Server Error", "");
        return;
    }

    struct stat st;
    int file = open_in_root(root, rlen, rel, &st);
    if (file < 0) { reply_error(client, 404, "Not Found", ""); return; }

    printf("%s %s\n", method, rel);

    char header[512];
    int hl = snprintf(header, sizeof header,
            "HTTP/1.0 200 OK\r\n"
            "Content-Type: %s\r\n"
            "Content-Length: %lld\r\n"
            "X-Content-Type-Options: nosniff\r\n"
            "Connection: close\r\n"
            "\r\n", content_type(rel), (long long)st.st_size);

    if (hl > 0 && send_all(client, header, (size_t)hl) == 0 && !head_only) {
        char  buf[16 * KB];
        ssize_t n;
        while ((n = read(file, buf, sizeof buf)) > 0)
            if (send_all(client, buf, (size_t)n) < 0) break;
    }
    close(file);
}

/* ---------- main ---------- */

int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IOLBF, 0);   /* log lines survive _exit() in children */

    const char *port_s = argc > 1 ? argv[1] : "8080";
    const char *dir    = argc > 2 ? argv[2] : "www";
    const char *bind_s = argc > 3 ? argv[3] : "127.0.0.1";

    char *end;
    long port = strtol(port_s, &end, 10);
    if (*end || port < 1 || port > 65535) { fprintf(stderr, "bad port\n"); return 1; }

    char root[PATH_MAX];
    if (!realpath(dir, root)) { perror(dir); return 1; }
    size_t rlen = strlen(root);
    if (rlen == 1) { fprintf(stderr, "refusing to serve /\n"); return 1; }

    signal(SIGPIPE, SIG_IGN);                       /* client hang-ups must not kill us */
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_sigchld;
    sa.sa_flags   = SA_RESTART | SA_NOCLDSTOP;
    sigaction(SIGCHLD, &sa, NULL);

    /* used to make fork()+children++ atomic w.r.t. the SIGCHLD handler */
    sigset_t chld_set, old_set;
    sigemptyset(&chld_set);
    sigaddset(&chld_set, SIGCHLD);

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof addr);
    addr.sin_family = AF_INET;
    addr.sin_port   = htons((uint16_t)port);
    if (inet_pton(AF_INET, bind_s, &addr.sin_addr) != 1) {
        fprintf(stderr, "bad bind address\n");
        return 1;
    }

    int server = socket(AF_INET, SOCK_STREAM, 0);
    if (server < 0) { perror("socket"); return 1; }

    int yes = 1;
    setsockopt(server, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof yes);

    if (bind(server, (const struct sockaddr *)&addr, sizeof addr) < 0) { perror("bind"); return 1; }
    if (listen(server, 32) < 0) { perror("listen"); return 1; }

    printf("serving %s on %s:%ld\n", root, bind_s, port);

    for (;;) {
        int client = accept(server, NULL, NULL);
        if (client < 0) {
            if (errno != EINTR && errno != ECONNABORTED) perror("accept");
            continue;
        }

        /* set timeouts BEFORE the overload reply so it can never block the parent */
        struct timeval tv = { IO_TIMEOUT_SEC, 0 };
        setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
        setsockopt(client, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);

        if (children >= MAX_CLIENTS) {
            reply_error(client, 503, "Service Unavailable", "");
            close(client);
            continue;
        }

        /* Block SIGCHLD across fork + increment so a fast-exiting child
         * cannot be reaped before we have counted it. */
        sigprocmask(SIG_BLOCK, &chld_set, &old_set);
        children++;
        pid_t pid = fork();
        if (pid == 0) {                             /* child */
            sigprocmask(SIG_SETMASK, &old_set, NULL);
            close(server);
            alarm(TOTAL_TIMEOUT_SEC);               /* kills slow-drip clients */
            handle_client(client, root, rlen);
            close(client);
            fflush(stdout);                         /* _exit() does not flush */
            _exit(0);
        }
        if (pid < 0) {
            children--;
            sigprocmask(SIG_SETMASK, &old_set, NULL);
            perror("fork");
            reply_error(client, 503, "Service Unavailable", "");
            close(client);
            continue;
        }
        sigprocmask(SIG_SETMASK, &old_set, NULL);
        close(client);
    }
}

