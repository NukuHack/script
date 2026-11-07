
#include <stdio.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <stdbool.h>
#include <errno.h>
#include <limits.h>
#include <math.h>

#if defined(_WIN32) || defined(_WIN64)
/* Windows code */
#  error "unsupported platform"
#elif defined(__APPLE__) || defined(__MACH__)
/* macOS code */
#  error "unsupported platform"
#elif defined(__linux__)
/* Linux code */
#  include <sys/socket.h>
#  include <sys/stat.h>
#  include <netinet/in.h>
#  include <fcntl.h>
#  include <arpa/inet.h>
#  include <unistd.h>
#elif defined(__FreeBSD__)
/* FreeBSD code */
#  error "unsupported platform"
#else
/* Unknown/unsupported OS */
#  error "unsupported platform"
#endif

#define KB 1024

char *content_type(char *file_name) {
    char *dot = strrchr(file_name, '.');
    if (dot == NULL) return "text/plain";
    if (strcmp(dot, ".html") == 0) return "text/html";
    if (strcmp(dot, ".css") == 0) return "text/css";
    if (strcmp(dot, ".png") == 0) return "image/png";
    if (strcmp(dot, ".jpeg") == 0) return "image/jpeg";
    if (strcmp(dot, ".jpg") == 0) return "image/jpeg";
    return "text/html";
}

void o4o4(int client) {
    const char *resp =
        "HTTP/1.0 404 Not Found\r\n"
        "Content-Type: text/html\r\n"
        "Content-Length: 15\r\n"
        "\r\n"
        "<h1>404</h1>\n";
    write(client, resp, strlen(resp));
    close(client);
}

int main(int argc, char **argv) {
#if 0    
    int socket(int domain, int type, int protocol);
#endif
    int server = socket(
            AF_INET /* ipv4 */,
            SOCK_STREAM /* tcp stream n ipv4 */,
            0 /* the best for the current pair */
    );
    if (server < 0) {
        perror("Server socket");
        exit(1);
    }

    printf("socket %d \n", server);

#if 0
    int bind(int sockfd, sockaddr *sockaddr, socklen_t addrlen);
#endif
#if 0
    struct sockaddr_in {
        sa_family_t _; /* AF_INET */
        in_port_t _; /* Port number */
        struct in_addr _; /* IPv4 addr */
    }
    struct in_addr {
        in_addr_t _;
    }
#endif

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = INADDR_ANY;
    /* u16 h-to-ns(u16) */
    addr.sin_port = htons(8080);

#if 0
    int getsockopt(int sockfd, int level, int optname,
            void optval[restrict *.optlen],
            socklen_t *restrict optlen);
    int setsockopt(int sockfd, int level, int optname,
            const void optval[.optlen],
            socklen_t optlen);
#endif
    int yes = 1;
    setsockopt(server, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));

    int bi = bind(
            server, /* scked we made */
            (const struct sockaddr *)&addr, /* generic pointer */
            sizeof(addr) /* length of pointer */
    );
    if (bi < 0) {
        perror("Bind socket");
        exit(1);
    }

#if 0
    int listen(int sockfd, int backlog);
    int accept(int sockfd, struct sockaddr *_Nullable restrict addr,
            socklen_t *_Nullable restrict addrlen);
#endif
    if (listen(server, 10) < 0) {
        perror("Listen socket");
        exit(1);
    }
#if 0
    ssize_t read(int fd, void buff[count], size_t count);
#endif
    printf("listening on :8080\n");
    while (1) {
        int client = accept(server, NULL, NULL);
        if (client < 0) {
            perror("Accept socket");
            exit(1);
        }
        printf("client %d\n", client);

        char req[4*KB];
        ssize_t n = read(client, req, sizeof(req) - 1); // -1 for the null term, for the string
        if (n <= 0) {
            close(client);
            continue;
        }
        req[n] = '\0';
        // int sscanf(const char *restrict str, ...);
        char method[8], path[256];
        if (sscanf(req, "%7s %255s", method, path) != 2) {
            close(client);
            continue;
        }

        char file_name[300];
        bool bad = strstr(path, "..") != NULL;
        snprintf(file_name, sizeof(file_name), "www%s",
                strcmp(path, "/") == 0 ? "/index.html" : path);

        printf("%s %s -> %s\n", method, path, file_name);

        struct stat info;
        int file = -1;
        if (bad || stat(file_name, &info) < 0 || !S_ISREG(info.st_mode)) {
            o4o4(client);
            continue;
        }

        file = open(file_name, O_RDONLY);
        if (file < 0) {
            o4o4(client);
            continue;
        }

        char header[256];
        int length = snprintf(header, sizeof(header),
                "HTTP/1.0 200 OK\r\n"
                "Content-Type: %s\r\n"
                "Content-Length: %ld\r\n"
                "\r\n",
                content_type(file_name), (long)info.st_size);
        write(client, header, length);

        char buffer[4*KB];
        ssize_t count;
        while ((count = read(file, buffer, sizeof(buffer))) > 0)
            write(client, buffer, count);

        close(file);
        close(client);
    }

    return 0;
}







