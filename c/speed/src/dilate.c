/*
 * dilate.c — ptrace/seccomp-based time dilation supervisor (x86_64 Linux)
 *
 * Runs a target program under a scaled clock. Time-related syscalls are
 * trapped at the seccomp boundary; everything else runs natively.
 *
 * Usage: dilate <factor> -- <program> [args...]
 * Build: make, or see dilate.sh
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <signal.h>
#include <time.h>
#include <stdint.h>
#include <stddef.h>
#include <sys/ptrace.h>
#include <sys/user.h>
#include <sys/wait.h>
#include <sys/uio.h>
#include <sys/syscall.h>
#include <sys/prctl.h>
#include <sys/types.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <linux/audit.h>
#include <linux/elf.h>
#include <linux/futex.h>

#define MAX_TRACEES 4096

#define WANT_CLOCK(id) \
    ((id) == CLOCK_REALTIME || (id) == CLOCK_MONOTONIC || \
     (id) == CLOCK_MONOTONIC_RAW || (id) == CLOCK_BOOTTIME || \
     (id) == CLOCK_REALTIME_COARSE || (id) == CLOCK_MONOTONIC_COARSE)

/* ---- global state ---- */
static double g_factor = 1.0;
static struct timespec g_ref_mono_t0;
static struct timespec g_base[8];
static int g_base_valid[8];
static pid_t g_pids[MAX_TRACEES];
static int g_npids = 0;

/* ---- helpers ---- */
static void die(const char *msg) { perror(msg); exit(1); }

static void ts_normalize(struct timespec *ts) {
    while (ts->tv_nsec >= 1000000000L) { ts->tv_nsec -= 1000000000L; ts->tv_sec++; }
    while (ts->tv_nsec < 0)            { ts->tv_nsec += 1000000000L; ts->tv_sec--; }
}
static double ts_to_double(const struct timespec *ts) {
    return (double)ts->tv_sec + (double)ts->tv_nsec / 1e9;
}
static void double_to_ts(double secs, struct timespec *ts) {
    ts->tv_sec  = (time_t)secs;
    ts->tv_nsec = (long)((secs - (double)ts->tv_sec) * 1e9);
    ts_normalize(ts);
}
static double real_elapsed_now(void) {
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (double)(now.tv_sec  - g_ref_mono_t0.tv_sec) +
           (double)(now.tv_nsec - g_ref_mono_t0.tv_nsec) / 1e9;
}
static void fake_time_for_clock(int clockid, struct timespec *out) {
    if (clockid < 0 || clockid >= 8 || !g_base_valid[clockid]) {
        clock_gettime(clockid, out);
        return;
    }
    double base = ts_to_double(&g_base[clockid]);
    double dilated = base + real_elapsed_now() * g_factor;
    double_to_ts(dilated, out);
}
static void track_pid(pid_t pid) {
    for (int i = 0; i < g_npids; i++) if (g_pids[i] == pid) return;
    if (g_npids < MAX_TRACEES) g_pids[g_npids++] = pid;
}
static void untrack_pid(pid_t pid) {
    for (int i = 0; i < g_npids; i++)
        if (g_pids[i] == pid) { g_pids[i] = g_pids[--g_npids]; return; }
}

/* ---- registers ---- */
static int get_regs(pid_t pid, struct user_regs_struct *regs) {
    struct iovec iov = { regs, sizeof(*regs) };
    return ptrace(PTRACE_GETREGSET, pid, (void *)NT_PRSTATUS, &iov) == -1 ? -1 : 0;
}
static int set_regs(pid_t pid, struct user_regs_struct *regs) {
    struct iovec iov = { regs, sizeof(*regs) };
    return ptrace(PTRACE_SETREGSET, pid, (void *)NT_PRSTATUS, &iov) == -1 ? -1 : 0;
}

/* ---- tracee memory ---- */
static int tracee_read(pid_t pid, void *remote, void *local, size_t len) {
    struct iovec liov = { local, len }, riov = { remote, len };
    if (process_vm_readv(pid, &liov, 1, &riov, 1, 0) == (ssize_t)len) return 0;
    size_t words = (len + sizeof(long) - 1) / sizeof(long);
    for (size_t i = 0; i < words; i++) {
        errno = 0;
        long w = ptrace(PTRACE_PEEKDATA, pid, (char *)remote + i * sizeof(long), NULL);
        if (w == -1 && errno) return -1;
        size_t chunk = (i == words - 1) ? (len - i * sizeof(long)) : sizeof(long);
        memcpy((char *)local + i * sizeof(long), &w, chunk);
    }
    return 0;
}
static int tracee_write(pid_t pid, void *remote, const void *local, size_t len) {
    struct iovec liov = { (void *)local, len }, riov = { remote, len };
    if (process_vm_writev(pid, &liov, 1, &riov, 1, 0) == (ssize_t)len) return 0;
    size_t words = (len + sizeof(long) - 1) / sizeof(long);
    for (size_t i = 0; i < words; i++) {
        long w;
        size_t chunk = (i == words - 1) ? (len - i * sizeof(long)) : sizeof(long);
        if (chunk < sizeof(long)) {
            errno = 0;
            w = ptrace(PTRACE_PEEKDATA, pid, (char *)remote + i * sizeof(long), NULL);
            if (w == -1 && errno) return -1;
        }
        memcpy(&w, (const char *)local + i * sizeof(long), chunk);
        if (ptrace(PTRACE_POKEDATA, pid, (char *)remote + i * sizeof(long), (void *)w) == -1)
            return -1;
    }
    return 0;
}

/* ---- seccomp filter ----
 *
 * Layout:
 *   [arch check]                 (3)
 *   [ld nr]                      (1)
 *   [per-syscall: JEQ ; JA trace]  * N
 *   [JEQ futex ; JA futex_check] (2)
 *   RET_ALLOW                    (1)
 *   futex_check:
 *     [ld args[1] ; AND 0x7f]
 *     [JEQ WAIT ; JA trace]      (2)
 *     [JEQ WAIT_BITSET ; JA trace] (2)
 *     [JEQ WAIT_REQUEUE_PI ; JA trace] (2)
 *     [#ifdef FUTEX_LOCK_PI2 ...] (2)
 *     RET_ALLOW                  (1)
 *   trace:
 *     RET_TRACE                  (1)
 *
 * JA offsets are patched after emission.
 */
#define FILTER_MAX 128
static struct sock_filter g_filter[FILTER_MAX];
static int g_filter_len;

static void emit_stmt(unsigned short code, unsigned int k) {
    g_filter[g_filter_len++] = (struct sock_filter){ code, 0, 0, k };
}
static void emit_jump(unsigned short code, unsigned int k,
                      unsigned char jt, unsigned char jf) {
    g_filter[g_filter_len++] = (struct sock_filter){ code, jt, jf, k };
}
static void patch_ja(int at, int target) {
    g_filter[at].k = (unsigned int)(target - (at + 1));
}

static void build_filter(void) {
    g_filter_len = 0;

    /* Arch check */
    emit_stmt(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, arch));
    emit_jump(BPF_JMP | BPF_JEQ | BPF_K, AUDIT_ARCH_X86_64, 1, 0);
    emit_stmt(BPF_RET | BPF_K, SECCOMP_RET_ALLOW);

    /* Load syscall nr */
    emit_stmt(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr));

    int main_ja[32];
    int n_main = 0;
#define TRAP(nr) do { \
        emit_jump(BPF_JMP | BPF_JEQ | BPF_K, (nr), 0, 1); \
        main_ja[n_main++] = g_filter_len; \
        emit_stmt(BPF_JMP | BPF_JA, 0); \
    } while (0)

    TRAP(SYS_clock_gettime);
    TRAP(SYS_gettimeofday);
    TRAP(SYS_nanosleep);
    TRAP(SYS_clock_nanosleep);
#ifdef SYS_time
    TRAP(SYS_time);
#endif
    TRAP(SYS_ppoll);
    TRAP(SYS_poll);
    TRAP(SYS_select);
    TRAP(SYS_pselect6);
    TRAP(SYS_epoll_wait);
    TRAP(SYS_epoll_pwait);
#ifdef SYS_epoll_pwait2
    TRAP(SYS_epoll_pwait2);
#endif
#undef TRAP

    /* futex: dispatch to a sub-block that also checks the op */
    emit_jump(BPF_JMP | BPF_JEQ | BPF_K, SYS_futex, 0, 1);
    int futex_dispatch_ja = g_filter_len;
    emit_stmt(BPF_JMP | BPF_JA, 0);

    /* Fall-through allow for everything else */
    emit_stmt(BPF_RET | BPF_K, SECCOMP_RET_ALLOW);

    /* ---- futex_check block ---- */
    int futex_check_idx = g_filter_len;
    emit_stmt(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, args[1]));
    emit_stmt(BPF_ALU | BPF_AND | BPF_K, FUTEX_CMD_MASK);

    int futex_ja[8];
    int n_futex = 0;
#define TRAP_FUTEX(cmd) do { \
        emit_jump(BPF_JMP | BPF_JEQ | BPF_K, (cmd), 0, 1); \
        futex_ja[n_futex++] = g_filter_len; \
        emit_stmt(BPF_JMP | BPF_JA, 0); \
    } while (0)
    TRAP_FUTEX(FUTEX_WAIT);
    TRAP_FUTEX(FUTEX_WAIT_BITSET);
    TRAP_FUTEX(FUTEX_WAIT_REQUEUE_PI);
#ifdef FUTEX_LOCK_PI2
    TRAP_FUTEX(FUTEX_LOCK_PI2);
#endif
#undef TRAP_FUTEX

    emit_stmt(BPF_RET | BPF_K, SECCOMP_RET_ALLOW);

    /* ---- trace block ---- */
    int trace_idx = g_filter_len;
    emit_stmt(BPF_RET | BPF_K, SECCOMP_RET_TRACE);

    /* Patch all forward JAs */
    for (int i = 0; i < n_main; i++)  patch_ja(main_ja[i], trace_idx);
    for (int i = 0; i < n_futex; i++) patch_ja(futex_ja[i], trace_idx);
    patch_ja(futex_dispatch_ja, futex_check_idx);
}

static void install_seccomp_filter(void) {
    build_filter();
    struct sock_fprog prog = {
        .len = (unsigned short)g_filter_len, .filter = g_filter
    };
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) == -1) die("prctl(NO_NEW_PRIVS)");
    if (prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &prog) == -1) die("prctl(SECCOMP_MODE_FILTER)");
}

/* ---- syscall handlers ---- */
static void cont(pid_t pid) { ptrace(PTRACE_CONT, pid, 0, 0); }

static void handle_clock_gettime_family(pid_t pid, struct user_regs_struct *regs,
                                        int is_gettimeofday) {
    long clockid   = is_gettimeofday ? CLOCK_REALTIME : (long)regs->rdi;
    void *out_ptr  = is_gettimeofday ? (void *)regs->rdi  : (void *)regs->rsi;

    if (!is_gettimeofday && !WANT_CLOCK(clockid)) { cont(pid); return; }

    regs->orig_rax = SYS_getpid;
    if (set_regs(pid, regs) == -1) { cont(pid); return; }

    ptrace(PTRACE_SYSCALL, pid, 0, 0);
    int status;
    if (waitpid(pid, &status, 0) == -1) return;
    if (!WIFSTOPPED(status)) return;

    struct timespec fake;
    fake_time_for_clock((int)clockid, &fake);

    struct user_regs_struct er;
    if (get_regs(pid, &er) == 0) { er.rax = 0; set_regs(pid, &er); }

    if (is_gettimeofday) {
        struct { long s; long us; } tv = { fake.tv_sec, fake.tv_nsec / 1000 };
        tracee_write(pid, out_ptr, &tv, sizeof tv);
    } else {
        struct { long s; long ns; } ts = { fake.tv_sec, fake.tv_nsec };
        tracee_write(pid, out_ptr, &ts, sizeof ts);
    }
    cont(pid);
}

static void handle_time(pid_t pid, struct user_regs_struct *regs) {
    void *out_ptr = (void *)regs->rdi;
    regs->orig_rax = SYS_getpid;
    set_regs(pid, regs);
    ptrace(PTRACE_SYSCALL, pid, 0, 0);
    int status;
    if (waitpid(pid, &status, 0) == -1) return;
    if (!WIFSTOPPED(status)) return;

    struct timespec fake;
    fake_time_for_clock(CLOCK_REALTIME, &fake);
    struct user_regs_struct er;
    if (get_regs(pid, &er) == 0) {
        er.rax = (unsigned long long)fake.tv_sec;
        set_regs(pid, &er);
    }
    if (out_ptr) tracee_write(pid, out_ptr, &fake.tv_sec, sizeof(long));
    cont(pid);
}

static void handle_nanosleep(pid_t pid, struct user_regs_struct *regs) {
    void *req_ptr = (void *)regs->rdi;
    struct { long s; long ns; } req;
    if (tracee_read(pid, req_ptr, &req, sizeof req) == 0 && g_factor > 0.0) {
        double secs = (double)req.s + (double)req.ns / 1e9;
        struct timespec nt;
        double_to_ts(secs / g_factor, &nt);
        struct { long s; long ns; } nr = { nt.tv_sec, nt.tv_nsec };
        tracee_write(pid, req_ptr, &nr, sizeof nr);
    }
    cont(pid);
}

static void handle_clock_nanosleep(pid_t pid, struct user_regs_struct *regs) {
    long clockid  = (long)regs->rdi;
    long flags    = (long)regs->rsi;
    void *req_ptr = (void *)regs->rdx;
    const int TIMER_ABSTIME_LOCAL = 1;

    struct { long s; long ns; } req;
    if (tracee_read(pid, req_ptr, &req, sizeof req) != 0 || g_factor <= 0.0) {
        cont(pid); return;
    }

    struct timespec nt;
    if (flags & TIMER_ABSTIME_LOCAL) {
        struct timespec fake_now;
        fake_time_for_clock((int)clockid, &fake_now);
        double target = (double)req.s + (double)req.ns / 1e9;
        double rem_real = (target - ts_to_double(&fake_now)) / g_factor;
        clockid_t rc = ((int)clockid >= 0 && (int)clockid < 8)
                       ? (clockid_t)clockid : CLOCK_REALTIME;
        struct timespec real_now;
        clock_gettime(rc, &real_now);
        double_to_ts(ts_to_double(&real_now) + rem_real, &nt);
    } else {
        double secs = (double)req.s + (double)req.ns / 1e9;
        double_to_ts(secs / g_factor, &nt);
    }
    struct { long s; long ns; } nr = { nt.tv_sec, nt.tv_nsec };
    tracee_write(pid, req_ptr, &nr, sizeof nr);
    cont(pid);
}

/* futex(uaddr, op, val, timeout, uaddr2, val3) -> rdi rsi rdx r10 r8 r9
 * timeout (r10) is only meaningful for WAIT-ish ops; the BPF filter
 * already restricted us to those, but we still NULL-check. */
static void handle_futex(pid_t pid, struct user_regs_struct *regs) {
    int op = (int)regs->rsi;
    void *timeout_ptr = (void *)regs->r10;
    if (!timeout_ptr) { cont(pid); return; }

    int base_cmd = op & FUTEX_CMD_MASK;
    struct { long s; long ns; } req;
    if (tracee_read(pid, timeout_ptr, &req, sizeof req) != 0 || g_factor <= 0.0) {
        cont(pid); return;
    }

    int absolute = (base_cmd == FUTEX_WAIT_BITSET) && (op & FUTEX_CLOCK_REALTIME);
    struct timespec nt;
    if (absolute) {
        struct timespec fake_now;
        fake_time_for_clock(CLOCK_REALTIME, &fake_now);
        double target   = (double)req.s + (double)req.ns / 1e9;
        double rem_real = (target - ts_to_double(&fake_now)) / g_factor;
        struct timespec real_now;
        clock_gettime(CLOCK_REALTIME, &real_now);
        double_to_ts(ts_to_double(&real_now) + rem_real, &nt);
    } else {
        double secs = (double)req.s + (double)req.ns / 1e9;
        double_to_ts(secs / g_factor, &nt);
    }
    struct { long s; long ns; } nr = { nt.tv_sec, nt.tv_nsec };
    tracee_write(pid, timeout_ptr, &nr, sizeof nr);
    cont(pid);
}

/* ppoll(fds, nfds, timeout_ts, sigmask, sigsetsize) -> rdx */
static void handle_ppoll(pid_t pid, struct user_regs_struct *regs) {
    void *tp = (void *)regs->rdx;
    if (!tp) { cont(pid); return; }
    struct { long s; long ns; } req;
    if (tracee_read(pid, tp, &req, sizeof req) != 0 || g_factor <= 0.0) { cont(pid); return; }
    double secs = (double)req.s + (double)req.ns / 1e9;
    struct timespec nt;
    double_to_ts(secs / g_factor, &nt);
    struct { long s; long ns; } nr = { nt.tv_sec, nt.tv_nsec };
    tracee_write(pid, tp, &nr, sizeof nr);
    cont(pid);
}

/* poll(fds, nfds, timeout_ms) -> rdx (int in register, no memory) */
static void handle_poll(pid_t pid, struct user_regs_struct *regs) {
    int t = (int)regs->rdx;
    if (t > 0 && g_factor > 0.0) {
        int s = (int)(t / g_factor); if (s < 0) s = 0;
        regs->rdx = (unsigned long long)s;
        set_regs(pid, regs);
    }
    cont(pid);
}

/* select(nfds, r, w, e, timeout_tv) -> r8 */
static void handle_select(pid_t pid, struct user_regs_struct *regs) {
    void *tp = (void *)regs->r8;
    if (!tp) { cont(pid); return; }
    struct { long s; long us; } req;
    if (tracee_read(pid, tp, &req, sizeof req) != 0 || g_factor <= 0.0) { cont(pid); return; }
    double secs = (double)req.s + (double)req.us / 1e6;
    double sc   = secs / g_factor;
    struct { long s; long us; } nv;
    nv.s  = (long)sc;
    nv.us = (long)((sc - (double)nv.s) * 1e6);
    if (nv.us < 0) nv.us = 0;
    if (nv.us >= 1000000) { nv.s++; nv.us -= 1000000; }
    tracee_write(pid, tp, &nv, sizeof nv);
    cont(pid);
}

/* pselect6(nfds, r, w, e, timeout_ts, sig) -> r8 */
static void handle_pselect6(pid_t pid, struct user_regs_struct *regs) {
    void *tp = (void *)regs->r8;
    if (!tp) { cont(pid); return; }
    struct { long s; long ns; } req;
    if (tracee_read(pid, tp, &req, sizeof req) != 0 || g_factor <= 0.0) { cont(pid); return; }
    double secs = (double)req.s + (double)req.ns / 1e9;
    struct timespec nt;
    double_to_ts(secs / g_factor, &nt);
    struct { long s; long ns; } nr = { nt.tv_sec, nt.tv_nsec };
    tracee_write(pid, tp, &nr, sizeof nr);
    cont(pid);
}

/* epoll_wait(epfd, events, maxevents, timeout_ms) -> r10 (int in register) */
static void handle_epoll_wait(pid_t pid, struct user_regs_struct *regs) {
    int t = (int)regs->r10;
    if (t > 0 && g_factor > 0.0) {
        int s = (int)(t / g_factor); if (s < 0) s = 0;
        regs->r10 = (unsigned long long)s;
        set_regs(pid, regs);
    }
    cont(pid);
}

/* epoll_pwait(epfd, events, maxevents, timeout_ms, sigmask, sigsetsize)
 * -> same int timeout in r10 as epoll_wait */
static void handle_epoll_pwait(pid_t pid, struct user_regs_struct *regs) {
    handle_epoll_wait(pid, regs);
}

/* epoll_pwait2(epfd, events, maxevents, timeout_ts, sigmask, sigsetsize)
 * -> r10 is struct timespec * */
static void handle_epoll_pwait2(pid_t pid, struct user_regs_struct *regs) {
    void *tp = (void *)regs->r10;
    if (!tp) { cont(pid); return; }
    struct { long s; long ns; } req;
    if (tracee_read(pid, tp, &req, sizeof req) != 0 || g_factor <= 0.0) { cont(pid); return; }
    double secs = (double)req.s + (double)req.ns / 1e9;
    struct timespec nt;
    double_to_ts(secs / g_factor, &nt);
    struct { long s; long ns; } nr = { nt.tv_sec, nt.tv_nsec };
    tracee_write(pid, tp, &nr, sizeof nr);
    cont(pid);
}

/* ---- dispatch ---- */
static void seed_base_clocks(void) {
    clock_gettime(CLOCK_MONOTONIC, &g_ref_mono_t0);
    int clocks[] = { CLOCK_REALTIME, CLOCK_MONOTONIC, CLOCK_MONOTONIC_RAW,
                     CLOCK_BOOTTIME, CLOCK_REALTIME_COARSE, CLOCK_MONOTONIC_COARSE };
    for (size_t i = 0; i < sizeof(clocks)/sizeof(clocks[0]); i++) {
        int c = clocks[i];
        if (c < 0 || c >= 8) continue;
        if (clock_gettime((clockid_t)c, &g_base[c]) == 0) g_base_valid[c] = 1;
    }
}

static void dispatch_seccomp_stop(pid_t pid) {
    struct user_regs_struct regs;
    if (get_regs(pid, &regs) == -1) { cont(pid); return; }
    long nr = regs.orig_rax;
    switch (nr) {
    case SYS_clock_gettime:    handle_clock_gettime_family(pid, &regs, 0); break;
    case SYS_gettimeofday:     handle_clock_gettime_family(pid, &regs, 1); break;
    case SYS_nanosleep:        handle_nanosleep(pid, &regs); break;
    case SYS_clock_nanosleep:  handle_clock_nanosleep(pid, &regs); break;
#ifdef SYS_time
    case SYS_time:             handle_time(pid, &regs); break;
#endif
    case SYS_futex:            handle_futex(pid, &regs); break;
    case SYS_ppoll:            handle_ppoll(pid, &regs); break;
    case SYS_poll:             handle_poll(pid, &regs); break;
    case SYS_select:           handle_select(pid, &regs); break;
    case SYS_pselect6:         handle_pselect6(pid, &regs); break;
    case SYS_epoll_wait:       handle_epoll_wait(pid, &regs); break;
    case SYS_epoll_pwait:      handle_epoll_pwait(pid, &regs); break;
#ifdef SYS_epoll_pwait2
    case SYS_epoll_pwait2:     handle_epoll_pwait2(pid, &regs); break;
#endif
    default:                   cont(pid); break;
    }
}

/* ---- main ---- */
int main(int argc, char **argv) {
    if (argc < 4 || strcmp(argv[2], "--") != 0) {
        fprintf(stderr, "usage: %s <factor> -- <program> [args...]\n", argv[0]);
        return 2;
    }
    g_factor = atof(argv[1]);
    if (g_factor <= 0.0) { fprintf(stderr, "factor must be > 0\n"); return 2; }

    pid_t pid = fork();
    if (pid == -1) die("fork");

    if (pid == 0) {
        if (ptrace(PTRACE_TRACEME, 0, 0, 0) == -1) die("PTRACE_TRACEME");
        install_seccomp_filter();
        raise(SIGSTOP);
        execvp(argv[3], &argv[3]);
        die("execvp");
    }

    int status;
    waitpid(pid, &status, 0);

    long opts = PTRACE_O_TRACESECCOMP | PTRACE_O_EXITKILL |
                PTRACE_O_TRACECLONE   | PTRACE_O_TRACEFORK |
                PTRACE_O_TRACEVFORK;
    if (ptrace(PTRACE_SETOPTIONS, pid, 0, opts) == -1) die("PTRACE_SETOPTIONS");

    seed_base_clocks();
    track_pid(pid);
    ptrace(PTRACE_CONT, pid, 0, 0);

    fprintf(stderr, "[dilate] tracing pid %d at factor %.4g\n", pid, g_factor);

    while (g_npids > 0) {
        int ws;
        pid_t w = waitpid(-1, &ws, __WALL);
        if (w == -1) {
            if (errno == ECHILD) break;
            if (errno == EINTR) continue;
            die("waitpid");
        }
        if (WIFEXITED(ws) || WIFSIGNALED(ws)) {
            untrack_pid(w);
            if (w == pid) {
                int code = WIFEXITED(ws) ? WEXITSTATUS(ws) : 128 + WTERMSIG(ws);
                while (g_npids > 0) {
                    pid_t w2 = waitpid(-1, &ws, __WALL);
                    if (w2 == -1) break;
                    if (WIFEXITED(ws) || WIFSIGNALED(ws)) untrack_pid(w2);
                }
                return code;
            }
            continue;
        }
        if (!WIFSTOPPED(ws)) continue;
        int sig   = WSTOPSIG(ws);
        int event = ws >> 16;

        if (event == PTRACE_EVENT_SECCOMP) {
            dispatch_seccomp_stop(w);
        } else if (event == PTRACE_EVENT_CLONE ||
                   event == PTRACE_EVENT_FORK  ||
                   event == PTRACE_EVENT_VFORK) {
            unsigned long newpid = 0;
            if (ptrace(PTRACE_GETEVENTMSG, w, 0, &newpid) == 0)
                track_pid((pid_t)newpid);
            ptrace(PTRACE_CONT, w, 0, 0);
        } else {
            ptrace(PTRACE_CONT, w, 0, sig ? sig : 0);
        }
    }
    return 0;
}