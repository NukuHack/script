/*
 * Tiny static file server (POSIX: Linux, macOS, *BSD).
 *
 * Build: cc -std=c11 -O2 -Wall -Wextra -o server main.c
 * Usage: ./server [port=8080] [docroot=www] [bind_addr=127.0.0.1]
 *        ./server 8080 www 0.0.0.0     # listen on all interfaces
 */
#define _POSIX_C_SOURCE 200809L

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <ctype.h>
#include <errno.h>
#include <limits.h>
#include <signal.h>

#if defined(_WIN32)
#  error "unsupported platform (needs POSIX sockets)"
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
#define MAX_CLIENTS 64      /* concurrent connections (one process each) */
#define IO_TIMEOUT_SEC 5    /* max idle time on a single read/write      */
#define TOTAL_TIMEOUT_SEC 30 /* max lifetime of one connection           */

#ifndef PATH_MAX
#  define PATH_MAX 4096
#endif

static volatile sig_atomic_t children = 0;

static void on_sigchld(int sig) {
    (void)sig;
    int saved = errno;
    while (waitpid(-1, NULL, WNOHANG) > 0) children--;
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
        p += n;
        len -= (size_t)n;
    }
    return 0;
}

static void reply_error(int client, int code, const char *reason, const char *extra) {
    char body[128], head[512];
    int bl = snprintf(body, sizeof body, "<h1>%d %s</h1>\n", code, reason);
    int hl = snprintf(head, sizeof head,
            "HTTP/1.0 %d %s\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "Content-Length: %d\r\n"
            "X-Content-Type-Options: nosniff\r\n"
            "%s"
            "Connection: close\r\n"
            "\r\n", code, reason, bl, extra);
    if (send_all(client, head, (size_t)hl) == 0)
        send_all(client, body, (size_t)bl);
}

static int hexval(int c) {
    return isdigit(c) ? c - '0' : (tolower(c) - 'a' + 10);
}

/* in-place %XX decode; rejects bad escapes and control chars (incl. %00, %0d, %0a) */
static int url_decode(char *s) {
    char *o = s;
    for (; *s; s++) {
        unsigned char c = (unsigned char)*s;
        if (c == '%') {
            if (!isxdigit((unsigned char)s[1]) || !isxdigit((unsigned char)s[2])) return -1;
            c = (unsigned char)((hexval(s[1]) << 4) | hexval(s[2]));
            s += 2;
        }
        if (c < 0x20 || c == 0x7f) return -1;
        *o++ = (char)c;
    }
    *o = '\0';
    return 0;
}

/* Resolve rel inside root, open it. Returns fd or -1. Symlinks pointing outside root are refused. */
static int open_in_root(const char *root, size_t rlen, const char *rel, struct stat *st) {
    char full[PATH_MAX], real[PATH_MAX];
    int n = snprintf(full, sizeof full, "%s%s", root, rel);
    if (n < 0 || (size_t)n >= sizeof full) return -1;
    if (!realpath(full, real)) return -1;
    if (strncmp(real, root, rlen) != 0 || real[rlen] != '/') return -1;

    int fd = open(real, O_RDONLY | O_NOFOLLOW);
    if (fd < 0) return -1;
    if (fstat(fd, st) < 0 || !S_ISREG(st->st_mode)) { close(fd); return -1; }
    return fd;
}

/* ---------- one connection ---------- */

static void handle_client(int client, const char *root, size_t rlen) {
    char req[8 * KB];
    size_t len = 0;
    int complete = 0;

    while (len < sizeof(req) - 1) {
        ssize_t n = recv(client, req + len, sizeof(req) - 1 - len, 0);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return;                         /* closed, error or timeout */
        len += (size_t)n;
        req[len] = '\0';
        if (strstr(req, "\r\n\r\n") || strstr(req, "\n\n")) { complete = 1; break; }
    }
    if (!complete) { reply_error(client, 431, "Request Header Fields Too Large", ""); return; }

    char method[8], path[1024], version[16];
    if (sscanf(req, "%7s %1023s %15s", method, path, version) != 3 ||
        strncmp(version, "HTTP/", 5) != 0) {
        reply_error(client, 400, "Bad Request", "");
        return;
    }
    if (strlen(path) >= sizeof(path) - 1) { reply_error(client, 414, "URI Too Long", ""); return; }

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

    char rel[1100];
    size_t pl = strlen(path);
    snprintf(rel, sizeof rel, "%s%s", path, path[pl - 1] == '/' ? "index.html" : "");

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

    if (send_all(client, header, (size_t)hl) == 0 && !head_only) {
        char buf[16 * KB];
        ssize_t n;
        while ((n = read(file, buf, sizeof buf)) > 0)
            if (send_all(client, buf, (size_t)n) < 0) break;
    }
    close(file);
}

/* ---------- main ---------- */

int main(int argc, char **argv) {
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
    sa.sa_flags = SA_RESTART | SA_NOCLDSTOP;
    sigaction(SIGCHLD, &sa, NULL);

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof addr);
    addr.sin_family = AF_INET;
    addr.sin_port = htons((uint16_t)port);
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

        if (children >= MAX_CLIENTS) {
            reply_error(client, 503, "Service Unavailable", "");
            close(client);
            continue;
        }

        struct timeval tv = { IO_TIMEOUT_SEC, 0 };
        setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
        setsockopt(client, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);

        pid_t pid = fork();
        if (pid < 0) {
            perror("fork");
            reply_error(client, 503, "Service Unavailable", "");
        } else if (pid == 0) {                      /* child */
            close(server);
            alarm(TOTAL_TIMEOUT_SEC);               /* kills slow-drip clients */
            handle_client(client, root, rlen);
            close(client);
            _exit(0);
        } else {
            children++;
        }
        close(client);
    }
}
