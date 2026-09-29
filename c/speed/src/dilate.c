/*
 * dilate.c — ptrace/seccomp time-dilation supervisor (x86_64 Linux)
 *
 * Runs a target under a scaled clock:
 *   factor > 1 : target's time runs faster
 *   factor < 1 : target's time runs slower
 *
 * How it works
 *   1. vDSO is disabled for every exec'd image (AT_SYSINFO_EHDR is blanked
 *      in the aux vector).  Without this, clock_gettime()/gettimeofday()/
 *      time() are answered in userspace and never reach the kernel.
 *   2. Time-reading syscalls are trapped (seccomp RET_TRACE) and answered
 *      by the supervisor with a scaled clock; the real syscall is skipped.
 *   3. Every syscall that takes a timeout (sleep/poll/select/epoll/futex/
 *      timerfd/itimer...) gets its timeout rewritten, using scratch space
 *      below the tracee's stack pointer.
 *   4. rdtsc/rdtscp are trapped (PR_SET_TSC) and emulated with a scaled TSC.
 *
 * 32-bit (i386 compat) tracees are fully supported: Wine's 32-bit host
 * (wine32 / old-style WoW64), 32-bit games run through WoW64 (rdtsc executed
 * from a 32-bit code segment inside a 64-bit process), and native i386
 * binaries.  Every ptrace stop decides per-stop whether the tracee is in
 * 32-bit or 64-bit mode (the kernel hands out a differently-sized register
 * set depending on the task's current code segment), and both the seccomp
 * filter and the syscall handlers know about the i386 syscall numbers and
 * timespec layouts (including the *_time64 variants).
 *
 * Usage: dilate <factor> -- <program> [args...]
 * Env:   DILATE_DEBUG=1|2     verbose log
 *        DILATE_KEEP_VDSO=1   don't strip the vDSO (debug only)
 *        DILATE_NO_TSC=1      don't emulate rdtsc
 *        DILATE_NO_EPOLL=1    don't trap epoll_wait*
 *        DILATE_OFF=vdso,tsc,epoll,futex,timeouts,clock  (bisecting aid)
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
#include <x86intrin.h>
#include <sys/ptrace.h>
#include <sys/user.h>
#include <sys/wait.h>
#include <sys/uio.h>
#include <sys/syscall.h>
#include <sys/prctl.h>
#include <sys/time.h>
#include <sys/types.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <linux/audit.h>
#include <linux/elf.h>
#include <linux/futex.h>

#ifndef PR_SET_TSC
#define PR_SET_TSC 26
#endif
#ifndef PR_TSC_SIGSEGV
#define PR_TSC_SIGSEGV 2
#endif
#ifndef AT_SYSINFO_EHDR
#define AT_SYSINFO_EHDR 33
#endif
#ifndef AUDIT_ARCH_I386
#define AUDIT_ARCH_I386 0x40000003u
#endif
#ifndef FUTEX_LOCK_PI2
#define FUTEX_LOCK_PI2 13
#endif

#define MAX_TRACEES 4096

/* /dev/ntsync (Linux 6.14+): _IOWR('N', 0x82/0x83, struct ntsync_wait_args) */
#define NTSYNC_IOC_WAIT_ANY 0xc0284e82u
#define NTSYNC_IOC_WAIT_ALL 0xc0284e83u
#define NTSYNC_WAIT_REALTIME 0x1u

#define WANT_CLOCK(id) \
    ((id) == CLOCK_REALTIME || (id) == CLOCK_MONOTONIC || \
     (id) == CLOCK_MONOTONIC_RAW || (id) == CLOCK_BOOTTIME || \
     (id) == CLOCK_REALTIME_COARSE || (id) == CLOCK_MONOTONIC_COARSE)

/* Floors so scaled timeouts never round to 0 and spin the tracee. */
#define MIN_SCALED_NS 100000   /* 100 us */
#define MIN_SCALED_MS 1

/* ---- global state ---- */
static double   g_factor = 1.0;
static int      g_debug = 0, g_keep_vdso = 0, g_no_tsc = 0, g_trap_epoll = 1;
static int      g_off_futex = 0, g_off_timeouts = 0, g_off_clock = 0;
static volatile sig_atomic_t g_stall_flag = 0;
static int64_t  g_ref_mono_ns;          /* CLOCK_MONOTONIC at start */
static int64_t  g_base_ns[8];
static int      g_base_valid[8];
static uint64_t g_tsc0;
static pid_t    g_pids[MAX_TRACEES];
static int      g_npids = 0;

#define DBG(...) do { if (g_debug) { \
    fprintf(stderr, "[dilate] " __VA_ARGS__); fputc('\n', stderr); } } while (0)

static void die(const char *msg) { perror(msg); exit(1); }

/* ---- time helpers (integer ns; doubles lose precision at epoch scale) ---- */
static int64_t real_ns(clockid_t c) {
    struct timespec t;
    clock_gettime(c, &t);
    return (int64_t)t.tv_sec * 1000000000LL + t.tv_nsec;
}
static int64_t fake_ns(int clk) {
    if (clk < 0 || clk >= 8 || !g_base_valid[clk]) return real_ns((clockid_t)clk);
    int64_t el = real_ns(CLOCK_MONOTONIC) - g_ref_mono_ns;
    return g_base_ns[clk] + (int64_t)((double)el * g_factor);
}
static int clock_is_dilated(int clk) { return clk >= 0 && clk < 8 && g_base_valid[clk]; }

static int64_t scale_rel_ns(int64_t ns) {
    if (ns <= 0) return ns;
    int64_t s = (int64_t)((double)ns / g_factor);
    return s < MIN_SCALED_NS ? MIN_SCALED_NS : s;
}
static int scale_ms(int ms) {
    if (ms <= 0) return ms;
    int s = (int)((double)ms / g_factor);
    return s < MIN_SCALED_MS ? MIN_SCALED_MS : s;
}
/* absolute deadline on `clk` in fake time -> absolute deadline in real time */
static int64_t abs_fake_to_real(int clk, int64_t target_ns) {
    int64_t rem = target_ns - fake_ns(clk);
    if (rem < 0) rem = 0;
    return real_ns((clockid_t)clk) + (int64_t)((double)rem / g_factor);
}
static uint64_t fake_tsc(void) {
    uint64_t now = __rdtsc();
    return g_tsc0 + (uint64_t)((double)(now - g_tsc0) * g_factor);
}

static void track_pid(pid_t pid) {
    for (int i = 0; i < g_npids; i++) if (g_pids[i] == pid) return;
    if (g_npids < MAX_TRACEES) g_pids[g_npids++] = pid;
}
static void untrack_pid(pid_t pid) {
    for (int i = 0; i < g_npids; i++)
        if (g_pids[i] == pid) { g_pids[i] = g_pids[--g_npids]; return; }
}

/* ---- registers ----
 * The kernel returns the *32-bit* register set (68 bytes) whenever the task
 * is currently executing in a 32-bit code segment (native i386 process, or
 * 32-bit code inside a 64-bit process such as WoW64), and the 64-bit set
 * (216 bytes) otherwise.  The returned iov_len tells us which one we got.
 * We normalise both into a struct user_regs_struct (32-bit values are
 * sign-extended for eax/orig_eax so -ENOSYS / -1 compare naturally, and
 * zero-extended for addresses) and convert back on set_regs().
 */
struct user_regs32 {
    uint32_t ebx, ecx, edx, esi, edi, ebp, eax;
    uint32_t xds, xes, xfs, xgs;
    uint32_t orig_eax, eip, xcs, eflags, esp, xss;
};

typedef struct {
    int is32;
    struct user_regs_struct r;      /* normalised view; edit these */
    struct user_regs32 raw32;       /* original compat regs (for write-back) */
} regs_t;

static int get_regs(pid_t pid, regs_t *g) {
    union { struct user_regs_struct r64; struct user_regs32 r32; } u;
    memset(&u, 0, sizeof u);
    struct iovec iov = { &u, sizeof u };
    if (ptrace(PTRACE_GETREGSET, pid, (void *)NT_PRSTATUS, &iov) == -1) return -1;
    memset(&g->r, 0, sizeof g->r);
    if (iov.iov_len == sizeof(struct user_regs32)) {
        struct user_regs32 *c = &u.r32;
        g->is32 = 1;
        g->raw32 = *c;
        g->r.rbx = c->ebx; g->r.rcx = c->ecx; g->r.rdx = c->edx;
        g->r.rsi = c->esi; g->r.rdi = c->edi; g->r.rbp = c->ebp;
        g->r.rax      = (uint64_t)(int64_t)(int32_t)c->eax;
        g->r.orig_rax = (uint64_t)(int64_t)(int32_t)c->orig_eax;
        g->r.rip = c->eip; g->r.rsp = c->esp;
        g->r.eflags = c->eflags; g->r.cs = c->xcs;
    } else {
        g->is32 = 0;
        g->r = u.r64;
    }
    return 0;
}
static int set_regs(pid_t pid, regs_t *g) {
    struct iovec iov;
    if (g->is32) {
        struct user_regs32 *c = &g->raw32;
        c->ebx = (uint32_t)g->r.rbx; c->ecx = (uint32_t)g->r.rcx;
        c->edx = (uint32_t)g->r.rdx; c->esi = (uint32_t)g->r.rsi;
        c->edi = (uint32_t)g->r.rdi; c->ebp = (uint32_t)g->r.rbp;
        c->eax = (uint32_t)g->r.rax; c->orig_eax = (uint32_t)g->r.orig_rax;
        c->eip = (uint32_t)g->r.rip; c->esp = (uint32_t)g->r.rsp;
        c->eflags = (uint32_t)g->r.eflags;
        iov.iov_base = c; iov.iov_len = sizeof *c;
    } else {
        iov.iov_base = &g->r; iov.iov_len = sizeof g->r;
    }
    return ptrace(PTRACE_SETREGSET, pid, (void *)NT_PRSTATUS, &iov) == -1 ? -1 : 0;
}

/* syscall argument i (0..5) in the tracee's ABI */
static unsigned long long *argp(regs_t *g, int i) {
    struct user_regs_struct *r = &g->r;
    if (g->is32) {
        switch (i) { case 0: return &r->rbx; case 1: return &r->rcx; case 2: return &r->rdx;
                     case 3: return &r->rsi; case 4: return &r->rdi; default: return &r->rbp; }
    }
    switch (i) { case 0: return &r->rdi; case 1: return &r->rsi; case 2: return &r->rdx;
                 case 3: return &r->r10; case 4: return &r->r8;  default: return &r->r9; }
}
#define ARG(g, i) (*argp((g), (i)))

/* ---- tracee memory ---- */
static int tracee_read(pid_t pid, uint64_t remote, void *local, size_t len) {
    struct iovec l = { local, len }, r = { (void *)remote, len };
    if (process_vm_readv(pid, &l, 1, &r, 1, 0) == (ssize_t)len) return 0;
    size_t words = (len + 7) / 8;
    for (size_t i = 0; i < words; i++) {
        errno = 0;
        long w = ptrace(PTRACE_PEEKDATA, pid, (void *)(remote + i * 8), NULL);
        if (w == -1 && errno) return -1;
        size_t chunk = (i == words - 1) ? (len - i * 8) : 8;
        memcpy((char *)local + i * 8, &w, chunk);
    }
    return 0;
}
static int tracee_write(pid_t pid, uint64_t remote, const void *local, size_t len) {
    struct iovec l = { (void *)local, len }, r = { (void *)remote, len };
    if (process_vm_writev(pid, &l, 1, &r, 1, 0) == (ssize_t)len) return 0;
    size_t words = (len + 7) / 8;
    for (size_t i = 0; i < words; i++) {
        long w = 0;
        size_t chunk = (i == words - 1) ? (len - i * 8) : 8;
        if (chunk < 8) {
            errno = 0;
            w = ptrace(PTRACE_PEEKDATA, pid, (void *)(remote + i * 8), NULL);
            if (w == -1 && errno) return -1;
        }
        memcpy(&w, (const char *)local + i * 8, chunk);
        if (ptrace(PTRACE_POKEDATA, pid, (void *)(remote + i * 8), (void *)w) == -1)
            return -1;
    }
    return 0;
}
/* read a w-byte (4 or 8) little-endian word */
static int rd_word(pid_t pid, uint64_t addr, int w, uint64_t *out) {
    uint64_t v = 0;
    if (tracee_read(pid, addr, &v, (size_t)w)) return -1;
    *out = v;
    return 0;
}

/* ---- syscall table: shared by the seccomp filter and the dispatcher ---- */
enum { OP_CLOCK_GETTIME = 1, OP_GETTIMEOFDAY, OP_TIME, OP_NANOSLEEP,
       OP_CLOCK_NANOSLEEP, OP_PPOLL, OP_PSELECT6, OP_SELECT, OP_POLL,
       OP_EPOLL_WAIT, OP_EPOLL_PWAIT2, OP_FUTEX, OP_FUTEX_WAITV, OP_IOCTL,
       OP_TIMERFD_SETTIME, OP_TIMER_SETTIME, OP_SETITIMER };
enum { G_CLOCK, G_TIMEOUT, G_FUTEX, G_EPOLL };

typedef struct {
    int op, grp, is32;
    long nr;
    int wide;   /* time objects use 64-bit fields (native x86_64, *_time64) */
    int tv;     /* struct timeval (usec) instead of timespec (nsec) */
} sc_ent;

/* i386 syscall numbers (asm/unistd_32.h) */
#define I_time            13
#define I_ioctl           54
#define I_gettimeofday    78
#define I_setitimer       104
#define I_newselect       142
#define I_nanosleep       162
#define I_poll            168
#define I_futex           240
#define I_epoll_wait      256
#define I_timer_settime   260
#define I_clock_gettime   265
#define I_clock_nanosleep 267
#define I_pselect6        308
#define I_ppoll           309
#define I_epoll_pwait     319
#define I_timerfd_settime 325
#define I_clock_gettime64 403
#define I_clock_nanosleep64 407
#define I_timer_settime64 409
#define I_timerfd_settime64 411
#define I_pselect6_time64 413
#define I_ppoll_time64    414
#define I_futex_time64    422
#define I_epoll_pwait2    441
#define I_futex_waitv     449

static const sc_ent g_tab[] = {
    /* ---- x86_64 ---- */
    { OP_CLOCK_GETTIME,   G_CLOCK,   0, SYS_clock_gettime,   1, 0 },
    { OP_GETTIMEOFDAY,    G_CLOCK,   0, SYS_gettimeofday,    1, 1 },
    { OP_TIME,            G_CLOCK,   0, SYS_time,            1, 0 },
    { OP_NANOSLEEP,       G_TIMEOUT, 0, SYS_nanosleep,       1, 0 },
    { OP_CLOCK_NANOSLEEP, G_TIMEOUT, 0, SYS_clock_nanosleep, 1, 0 },
    { OP_PPOLL,           G_TIMEOUT, 0, SYS_ppoll,           1, 0 },
    { OP_POLL,            G_TIMEOUT, 0, SYS_poll,            1, 0 },
    { OP_SELECT,          G_TIMEOUT, 0, SYS_select,          1, 1 },
    { OP_PSELECT6,        G_TIMEOUT, 0, SYS_pselect6,        1, 0 },
    { OP_TIMERFD_SETTIME, G_TIMEOUT, 0, SYS_timerfd_settime, 1, 0 },
    { OP_TIMER_SETTIME,   G_TIMEOUT, 0, SYS_timer_settime,   1, 0 },
    { OP_SETITIMER,       G_TIMEOUT, 0, SYS_setitimer,       1, 1 },
    { OP_FUTEX_WAITV,     G_FUTEX,   0, 449,                 1, 0 },
    { OP_EPOLL_WAIT,      G_EPOLL,   0, SYS_epoll_wait,      1, 0 },
    { OP_EPOLL_WAIT,      G_EPOLL,   0, SYS_epoll_pwait,     1, 0 },
    { OP_EPOLL_PWAIT2,    G_EPOLL,   0, 441,                 1, 0 },
    { OP_FUTEX,           G_FUTEX,   0, SYS_futex,           1, 0 },
    { OP_IOCTL,           G_FUTEX,   0, SYS_ioctl,           1, 0 },
    /* ---- i386 (compat) ---- */
    { OP_CLOCK_GETTIME,   G_CLOCK,   1, I_clock_gettime,     0, 0 },
    { OP_CLOCK_GETTIME,   G_CLOCK,   1, I_clock_gettime64,   1, 0 },
    { OP_GETTIMEOFDAY,    G_CLOCK,   1, I_gettimeofday,      0, 1 },
    { OP_TIME,            G_CLOCK,   1, I_time,              0, 0 },
    { OP_NANOSLEEP,       G_TIMEOUT, 1, I_nanosleep,         0, 0 },
    { OP_CLOCK_NANOSLEEP, G_TIMEOUT, 1, I_clock_nanosleep,   0, 0 },
    { OP_CLOCK_NANOSLEEP, G_TIMEOUT, 1, I_clock_nanosleep64, 1, 0 },
    { OP_PPOLL,           G_TIMEOUT, 1, I_ppoll,             0, 0 },
    { OP_PPOLL,           G_TIMEOUT, 1, I_ppoll_time64,      1, 0 },
    { OP_POLL,            G_TIMEOUT, 1, I_poll,              0, 0 },
    { OP_SELECT,          G_TIMEOUT, 1, I_newselect,         0, 1 },
    { OP_PSELECT6,        G_TIMEOUT, 1, I_pselect6,          0, 0 },
    { OP_PSELECT6,        G_TIMEOUT, 1, I_pselect6_time64,   1, 0 },
    { OP_TIMERFD_SETTIME, G_TIMEOUT, 1, I_timerfd_settime,   0, 0 },
    { OP_TIMERFD_SETTIME, G_TIMEOUT, 1, I_timerfd_settime64, 1, 0 },
    { OP_TIMER_SETTIME,   G_TIMEOUT, 1, I_timer_settime,     0, 0 },
    { OP_TIMER_SETTIME,   G_TIMEOUT, 1, I_timer_settime64,   1, 0 },
    { OP_SETITIMER,       G_TIMEOUT, 1, I_setitimer,         0, 1 },
    { OP_FUTEX_WAITV,     G_FUTEX,   1, I_futex_waitv,       1, 0 },
    { OP_EPOLL_WAIT,      G_EPOLL,   1, I_epoll_wait,        0, 0 },
    { OP_EPOLL_WAIT,      G_EPOLL,   1, I_epoll_pwait,       0, 0 },
    { OP_EPOLL_PWAIT2,    G_EPOLL,   1, I_epoll_pwait2,      1, 0 },
    { OP_FUTEX,           G_FUTEX,   1, I_futex,             0, 0 },
    { OP_FUTEX,           G_FUTEX,   1, I_futex_time64,      1, 0 },
    { OP_IOCTL,           G_FUTEX,   1, I_ioctl,             0, 0 },
};
#define N_TAB ((int)(sizeof g_tab / sizeof g_tab[0]))

static int group_enabled(int grp) {
    switch (grp) {
    case G_CLOCK:   return !g_off_clock;
    case G_TIMEOUT: return !g_off_timeouts;
    case G_FUTEX:   return !g_off_futex;
    case G_EPOLL:   return g_trap_epoll && !g_off_timeouts;
    }
    return 0;
}
static const sc_ent *lookup(int is32, long nr) {
    for (int i = 0; i < N_TAB; i++)
        if (g_tab[i].is32 == is32 && g_tab[i].nr == nr) return &g_tab[i];
    return NULL;
}

/* ---- seccomp filter (tiny label-based assembler) ---- */
#define FILTER_MAX 512
static struct sock_filter g_filter[FILTER_MAX];
static int g_filter_len;
enum { L_64, L_32, L_FUTEX, L_FUTEX_TMO, L_IOCTL, L_TRACE, L_COUNT };
static int g_lbl[L_COUNT];
static struct { int at, label; } g_fix[256];
static int g_nfix;

static void emit_stmt(unsigned short code, unsigned int k) {
    if (g_filter_len >= FILTER_MAX) { fprintf(stderr, "dilate: filter overflow\n"); exit(1); }
    g_filter[g_filter_len++] = (struct sock_filter){ code, 0, 0, k };
}
static void emit_jump(unsigned short code, unsigned int k,
                      unsigned char jt, unsigned char jf) {
    if (g_filter_len >= FILTER_MAX) { fprintf(stderr, "dilate: filter overflow\n"); exit(1); }
    g_filter[g_filter_len++] = (struct sock_filter){ code, jt, jf, k };
}
static void emit_goto(int label) {
    if (g_nfix >= 256) { fprintf(stderr, "dilate: too many fixups\n"); exit(1); }
    g_fix[g_nfix].at = g_filter_len; g_fix[g_nfix].label = label; g_nfix++;
    emit_stmt(BPF_JMP | BPF_JA, 0);
}
/* if (A == k) goto label; (A is preserved on fall-through) */
static void emit_if_eq_goto(unsigned int k, int label) {
    emit_jump(BPF_JMP | BPF_JEQ | BPF_K, k, 0, 1);
    emit_goto(label);
}
static void emit_ld(unsigned int off) { emit_stmt(BPF_LD | BPF_W | BPF_ABS, off); }
#define ALLOW() emit_stmt(BPF_RET | BPF_K, SECCOMP_RET_ALLOW)

static void build_filter(void) {
    g_filter_len = 0; g_nfix = 0;

    emit_ld(offsetof(struct seccomp_data, arch));
    emit_if_eq_goto(AUDIT_ARCH_X86_64, L_64);
    emit_if_eq_goto(AUDIT_ARCH_I386,   L_32);
    ALLOW();                                        /* unknown ABI */

    for (int is32 = 0; is32 <= 1; is32++) {
        g_lbl[is32 ? L_32 : L_64] = g_filter_len;
        emit_ld(offsetof(struct seccomp_data, nr));
        for (int i = 0; i < N_TAB; i++) {
            const sc_ent *e = &g_tab[i];
            if (e->is32 != is32 || !group_enabled(e->grp)) continue;
            int target = e->op == OP_FUTEX ? L_FUTEX :
                         e->op == OP_IOCTL ? L_IOCTL : L_TRACE;
            emit_if_eq_goto((unsigned int)e->nr, target);
        }
        ALLOW();
    }

    /* futex: only the blocking ops that carry a timeout, and only when the
     * timeout pointer is non-NULL (args[3]); everything else runs untouched */
    g_lbl[L_FUTEX] = g_filter_len;
    emit_ld(offsetof(struct seccomp_data, args[1]));
    emit_stmt(BPF_ALU | BPF_AND | BPF_K, (unsigned int)FUTEX_CMD_MASK);
    emit_if_eq_goto(FUTEX_WAIT,             L_FUTEX_TMO);
    emit_if_eq_goto(FUTEX_WAIT_BITSET,      L_FUTEX_TMO);
    emit_if_eq_goto(FUTEX_WAIT_REQUEUE_PI,  L_FUTEX_TMO);
    emit_if_eq_goto(FUTEX_LOCK_PI,          L_FUTEX_TMO);
    emit_if_eq_goto(FUTEX_LOCK_PI2,         L_FUTEX_TMO);
    ALLOW();

    g_lbl[L_FUTEX_TMO] = g_filter_len;
    emit_ld(offsetof(struct seccomp_data, args[3]));
    emit_jump(BPF_JMP | BPF_JEQ | BPF_K, 0, 1, 0);   /* lo == 0 ? skip goto */
    emit_goto(L_TRACE);
    emit_ld(offsetof(struct seccomp_data, args[3]) + 4);
    emit_jump(BPF_JMP | BPF_JEQ | BPF_K, 0, 1, 0);   /* hi == 0 ? skip goto */
    emit_goto(L_TRACE);
    ALLOW();                                          /* NULL timeout */

    /* ntsync waits: same "blocking with a deadline" class */
    g_lbl[L_IOCTL] = g_filter_len;
    emit_ld(offsetof(struct seccomp_data, args[1]));
    emit_if_eq_goto(NTSYNC_IOC_WAIT_ANY, L_TRACE);
    emit_if_eq_goto(NTSYNC_IOC_WAIT_ALL, L_TRACE);
    ALLOW();

    g_lbl[L_TRACE] = g_filter_len;
    emit_stmt(BPF_RET | BPF_K, SECCOMP_RET_TRACE);

    for (int i = 0; i < g_nfix; i++)
        g_filter[g_fix[i].at].k = (unsigned int)(g_lbl[g_fix[i].label] - (g_fix[i].at + 1));
}

static void install_seccomp_filter(void) {
    build_filter();
    struct sock_fprog prog = { .len = (unsigned short)g_filter_len, .filter = g_filter };
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) == -1) die("prctl(NO_NEW_PRIVS)");
    if (prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &prog) == -1) die("prctl(SECCOMP_MODE_FILTER)");
}

/* ---- deadlines patched in place; restored at syscall exit ----
 * ntsync's wait struct is also an *output* (index), so we cannot redirect it
 * to scratch. Instead we patch the timeout in the caller's struct and put the
 * original back at syscall-exit, so EINTR retries don't convert twice. */
static struct { pid_t pid; uint64_t addr, orig; } g_pend[MAX_TRACEES];
static int pend_find(pid_t pid) {
    for (int i = 0; i < MAX_TRACEES; i++) if (g_pend[i].pid == pid) return i;
    return -1;
}
static int pend_add(pid_t pid, uint64_t addr, uint64_t orig) {
    int i = pend_find(0);
    if (i < 0) return 0;
    g_pend[i].pid = pid; g_pend[i].addr = addr; g_pend[i].orig = orig;
    return 1;
}
static void resume(pid_t pid, int sig) {
    ptrace(pend_find(pid) >= 0 ? PTRACE_SYSCALL : PTRACE_CONT, pid, 0, sig);
}
static int handle_sysgood_stop(pid_t pid) {
    int i = pend_find(pid);
    if (i < 0) { ptrace(PTRACE_CONT, pid, 0, 0); return 1; }
    regs_t g;
    if (get_regs(pid, &g) == 0 && (long long)g.r.rax == -ENOSYS) {   /* entry stop */
        ptrace(PTRACE_SYSCALL, pid, 0, 0);
        return 1;
    }
    tracee_write(pid, g_pend[i].addr, &g_pend[i].orig, 8);
    g_pend[i].pid = 0;
    ptrace(PTRACE_CONT, pid, 0, 0);
    return 1;
}

/* ---- syscall handlers ---- */
static void cont(pid_t pid) { ptrace(PTRACE_CONT, pid, 0, 0); }

/* Skip the real syscall and return `ret` to the tracee. */
static void skip_with(pid_t pid, regs_t *g, long ret) {
    g->r.orig_rax = (unsigned long long)-1;
    g->r.rax = (unsigned long long)ret;
    set_regs(pid, g);
    cont(pid);
}

/* Scratch area below the stack pointer. Only used for syscall args the
 * kernel copies at entry, so later signal frames can't corrupt it. */
static uint64_t scratch(const regs_t *g, int slot) {
    return ((g->r.rsp - 1024) & ~15ULL) + (uint64_t)slot * 64;
}

/* A time object in the tracee's memory: {sec,frac} with 32- or 64-bit
 * fields; frac is nanoseconds (timespec) or microseconds (timeval). */
static size_t tobj_size(int wide) { return wide ? 16 : 8; }
static int tobj_read(pid_t pid, uint64_t addr, int wide, int tv, int64_t *ns) {
    int64_t s, f;
    if (wide) { int64_t v[2]; if (tracee_read(pid, addr, v, sizeof v)) return -1; s = v[0]; f = v[1]; }
    else      { int32_t v[2]; if (tracee_read(pid, addr, v, sizeof v)) return -1; s = v[0]; f = v[1]; }
    *ns = s * 1000000000LL + f * (tv ? 1000 : 1);
    return 0;
}
static int tobj_write(pid_t pid, uint64_t addr, int wide, int tv, int64_t ns) {
    int64_t unit = tv ? 1000 : 1;
    int64_t s = ns / 1000000000LL, f = (ns % 1000000000LL) / unit;
    if (wide) { int64_t v[2] = { s, f }; return tracee_write(pid, addr, v, sizeof v); }
    if (s > INT32_MAX) { s = INT32_MAX; f = 0; }
    if (s < INT32_MIN) { s = INT32_MIN; f = 0; }
    int32_t v[2] = { (int32_t)s, (int32_t)f };
    return tracee_write(pid, addr, v, sizeof v);
}

/* Rewrite the time object pointed to by syscall arg `ai`. rel: scale;
 * else absolute on clock `clk`. Returns 1 if the argument was redirected. */
static int retarget(pid_t pid, regs_t *g, int ai, int wide, int tv,
                    int absolute, int clk, int slot) {
    uint64_t p = ARG(g, ai);
    if (!p) return 0;
    int64_t ns, out;
    if (tobj_read(pid, p, wide, tv, &ns) != 0) return 0;
    if (absolute) {
        if (!clock_is_dilated(clk)) return 0;
        out = abs_fake_to_real(clk, ns);
    } else {
        out = scale_rel_ns(ns);
    }
    uint64_t sp = scratch(g, slot);
    if (tobj_write(pid, sp, wide, tv, out) != 0) return 0;
    ARG(g, ai) = sp;
    return 1;
}
/* itimerspec / itimerval: two consecutive objects (interval, value), relative */
static int retarget_pair(pid_t pid, regs_t *g, int ai, int wide, int tv, int slot) {
    uint64_t p = ARG(g, ai);
    if (!p) return 0;
    size_t sz = tobj_size(wide);
    int64_t ns[2];
    for (int i = 0; i < 2; i++)
        if (tobj_read(pid, p + i * sz, wide, tv, &ns[i]) != 0) return 0;
    uint64_t sp = scratch(g, slot);
    for (int i = 0; i < 2; i++)
        if (tobj_write(pid, sp + i * sz, wide, tv, scale_rel_ns(ns[i])) != 0) return 0;
    ARG(g, ai) = sp;
    return 1;
}

static void reply_time(pid_t pid, regs_t *g, const sc_ent *e, int clk, uint64_t out) {
    int64_t ns = fake_ns(clk);
    if (e->op == OP_GETTIMEOFDAY) {
        if (out && tobj_write(pid, out, e->wide, 1, ns) != 0) { skip_with(pid, g, -EFAULT); return; }
        uint64_t tz = ARG(g, 1);            /* struct timezone: zero it */
        if (tz) { int32_t z[2] = {0, 0}; tracee_write(pid, tz, z, sizeof z); }
    } else {
        if (tobj_write(pid, out, e->wide, 0, ns) != 0) { skip_with(pid, g, -EFAULT); return; }
    }
    skip_with(pid, g, 0);
}

static void dispatch_seccomp_stop(pid_t pid) {
    regs_t g;
    if (get_regs(pid, &g) == -1) { cont(pid); return; }
    long nr = (long)(int64_t)g.r.orig_rax;
    const sc_ent *e = lookup(g.is32, nr);
    if (g_debug >= 2)
        DBG("pid %d trap%s nr=%ld a0=%llx a1=%llx a2=%llx a3=%llx a4=%llx", pid,
            g.is32 ? "(32)" : "", nr, ARG(&g, 0), ARG(&g, 1), ARG(&g, 2), ARG(&g, 3), ARG(&g, 4));
    if (!e) { cont(pid); return; }

    int changed = 0, w = e->wide, tv = e->tv;

    switch (e->op) {
    case OP_CLOCK_GETTIME: {
        int clk = (int)ARG(&g, 0);
        if (!WANT_CLOCK(clk)) { cont(pid); return; }
        reply_time(pid, &g, e, clk, ARG(&g, 1));
        return;
    }
    case OP_GETTIMEOFDAY:
        reply_time(pid, &g, e, CLOCK_REALTIME, ARG(&g, 0));
        return;
    case OP_TIME: {
        int64_t s = fake_ns(CLOCK_REALTIME) / 1000000000LL;
        uint64_t out = ARG(&g, 0);
        if (out) {
            int rc = g.is32 ? ({ int32_t s32 = (int32_t)s; tracee_write(pid, out, &s32, 4); })
                            : tracee_write(pid, out, &s, 8);
            if (rc != 0) { skip_with(pid, &g, -EFAULT); return; }
        }
        skip_with(pid, &g, (long)s);
        return;
    }
    case OP_NANOSLEEP:
        changed = retarget(pid, &g, 0, w, 0, 0, 0, 0);
        break;
    case OP_CLOCK_NANOSLEEP: {
        int abs = (int)(ARG(&g, 1) & 1);                   /* TIMER_ABSTIME */
        changed = retarget(pid, &g, 2, w, 0, abs, (int)ARG(&g, 0), 0);
        break;
    }
    case OP_PPOLL:
        changed = retarget(pid, &g, 2, w, 0, 0, 0, 0);
        break;
    case OP_PSELECT6:
        changed = retarget(pid, &g, 4, w, 0, 0, 0, 0);
        break;
    case OP_SELECT:
        changed = retarget(pid, &g, 4, w, 1, 0, 0, 0);
        break;
    case OP_POLL: {
        int t = (int)ARG(&g, 2), s = scale_ms(t);
        if (s != t) { ARG(&g, 2) = (unsigned long long)(long long)s; changed = 1; }
        break;
    }
    case OP_EPOLL_WAIT: {
        int t = (int)ARG(&g, 3), s = scale_ms(t);
        if (s != t) { ARG(&g, 3) = (unsigned long long)(long long)s; changed = 1; }
        break;
    }
    case OP_EPOLL_PWAIT2:
        changed = retarget(pid, &g, 3, w, 0, 0, 0, 0);
        break;
    case OP_FUTEX: {
        int op = (int)ARG(&g, 1), cmd = op & FUTEX_CMD_MASK;
        int clk = (op & FUTEX_CLOCK_REALTIME) ? CLOCK_REALTIME : CLOCK_MONOTONIC;
        if (cmd == FUTEX_LOCK_PI) clk = CLOCK_REALTIME;      /* always realtime */
        int abs = (cmd != FUTEX_WAIT);                       /* WAIT is relative */
        changed = retarget(pid, &g, 3, w, 0, abs, clk, 0);
        break;
    }
    case OP_IOCTL: {                                         /* ntsync wait */
        struct { uint64_t timeout, objs; uint32_t count, index, flags, owner, alert, pad; } wa;   /* uapi order */
        uint64_t addr = ARG(&g, 2);
        if (tracee_read(pid, addr, &wa, sizeof wa) != 0) break;
        if (wa.timeout == 0 || wa.timeout >= (1ULL << 63)) break;   /* poll / infinite */
        int clk = (wa.flags & NTSYNC_WAIT_REALTIME) ? CLOCK_REALTIME : CLOCK_MONOTONIC;
        uint64_t nt = (uint64_t)abs_fake_to_real(clk, (int64_t)wa.timeout);
        if (g_debug >= 2) DBG("pid %d ntsync wait deadline %llu -> %llu", pid,
                              (unsigned long long)wa.timeout, (unsigned long long)nt);
        if (tracee_write(pid, addr, &nt, 8) == 0 && pend_add(pid, addr, wa.timeout)) {
            ptrace(PTRACE_SYSCALL, pid, 0, 0);               /* stop at exit to restore */
            return;
        }
        break;
    }
    case OP_FUTEX_WAITV:                                     /* Wine fsync */
        changed = retarget(pid, &g, 3, 1, 0, 1, (int)ARG(&g, 4), 0);
        break;
    case OP_TIMERFD_SETTIME:
    case OP_TIMER_SETTIME:
        if (ARG(&g, 1) & 1) break;                           /* absolute: leave */
        changed = retarget_pair(pid, &g, 2, w, 0, 0);
        break;
    case OP_SETITIMER:
        changed = retarget_pair(pid, &g, 1, w, tv, 0);
        break;
    default: break;
    }

    if (changed) set_regs(pid, &g);
    cont(pid);
}

/* ---- vDSO removal ----
 * At PTRACE_EVENT_EXEC the new image's initial stack is
 *   argc, argv[]..., NULL, envp[]..., NULL, auxv pairs..., AT_NULL
 * (words are 4 bytes for i386 images, 8 for x86_64).  Turning
 * AT_SYSINFO_EHDR into AT_IGNORE makes libc skip the vDSO, so clock_gettime
 * & co become real (trappable) syscalls. */
static void strip_vdso(pid_t pid) {
    regs_t g;
    if (get_regs(pid, &g) == -1) return;
    int w = g.is32 ? 4 : 8;
    uint64_t p = g.r.rsp, v;
    if (rd_word(pid, p, w, &v)) return;
    uint64_t argc = v;
    p += (uint64_t)w * (argc + 2);            /* past argc, argv, NULL */
    for (int i = 0; i < 1000000; i++) {       /* envp until NULL */
        if (rd_word(pid, p, w, &v)) return;
        p += (uint64_t)w;
        if (v == 0) break;
    }
    for (int i = 0; i < 512; i++) {           /* auxv */
        uint64_t key, val;
        if (rd_word(pid, p, w, &key) || rd_word(pid, p + (uint64_t)w, w, &val)) return;
        if (key == 0) break;
        if (key == AT_SYSINFO_EHDR) {
            uint64_t ign = 1;                 /* AT_IGNORE */
            tracee_write(pid, p, &ign, (size_t)w);
            DBG("pid %d: vDSO stripped (%s)", pid, g.is32 ? "i386" : "x86_64");
            return;
        }
        p += 2 * (uint64_t)w;
    }
}

/* ---- rdtsc / rdtscp emulation (PR_TSC_SIGSEGV) ---- */
static int try_emulate_tsc(pid_t pid) {
    siginfo_t si;
    if (ptrace(PTRACE_GETSIGINFO, pid, 0, &si) == -1) return 0;
    if (si.si_signo != SIGSEGV || si.si_code != 0x80 /* SI_KERNEL */) return 0;
    regs_t g;
    if (get_regs(pid, &g) == -1) return 0;
    unsigned char ins[3] = {0};
    if (tracee_read(pid, g.r.rip, ins, 3) != 0) return 0;
    int len, is_p = 0;
    if (ins[0] == 0x0f && ins[1] == 0x31)                       len = 2;
    else if (ins[0] == 0x0f && ins[1] == 0x01 && ins[2] == 0xf9) { len = 3; is_p = 1; }
    else return 0;
    uint64_t t = fake_tsc();
    g.r.rax = t & 0xffffffffULL;
    g.r.rdx = t >> 32;
    if (is_p) g.r.rcx = 0;
    g.r.rip += (uint64_t)len;
    return set_regs(pid, &g) == 0;
}

/* ---- diagnostics ---- */
static const char *sig_name(int sig) {
    const char *n = sig > 0 && sig < 65 ? strsignal(sig) : NULL;
    return n ? n : "?";
}
static void exe_of(pid_t pid, char *buf, size_t n) {
    char p[64];
    snprintf(p, sizeof p, "/proc/%d/exe", pid);
    ssize_t r = readlink(p, buf, n - 1);
    if (r < 0) { snprintf(buf, n, "?"); return; }
    buf[r] = 0;
}

/* ---- stall report (DILATE_DEBUG>=1): where is every tracee blocked? ---- */
static void on_alarm(int sig) { (void)sig; g_stall_flag = 1; }
static void dump_stall(void) {
    fprintf(stderr, "[dilate] --- stall report: %d tracees ---\n", g_npids);
    for (int i = 0; i < g_npids && i < 64; i++) {
        char path[64], comm[64] = "?", sc[160] = "?";
        FILE *f;
        snprintf(path, sizeof path, "/proc/%d/comm", g_pids[i]);
        if ((f = fopen(path, "r"))) { if (fgets(comm, sizeof comm, f)) comm[strcspn(comm, "\n")] = 0; fclose(f); }
        snprintf(path, sizeof path, "/proc/%d/syscall", g_pids[i]);
        if ((f = fopen(path, "r"))) { if (fgets(sc, sizeof sc, f)) sc[strcspn(sc, "\n")] = 0; fclose(f); }
        fprintf(stderr, "[dilate]   pid %d (%s): %s\n", g_pids[i], comm, sc);
    }
}

/* ---- setup ---- */
static void seed_clocks(void) {
    g_ref_mono_ns = real_ns(CLOCK_MONOTONIC);
    int cl[] = { CLOCK_REALTIME, CLOCK_MONOTONIC, CLOCK_MONOTONIC_RAW,
                 CLOCK_BOOTTIME, CLOCK_REALTIME_COARSE, CLOCK_MONOTONIC_COARSE };
    for (size_t i = 0; i < sizeof cl / sizeof cl[0]; i++) {
        g_base_ns[cl[i]] = real_ns((clockid_t)cl[i]);
        g_base_valid[cl[i]] = 1;
    }
    g_tsc0 = __rdtsc();
}

int main(int argc, char **argv) {
    if (argc < 4 || strcmp(argv[2], "--") != 0) {
        fprintf(stderr, "usage: %s <factor> -- <program> [args...]\n", argv[0]);
        return 2;
    }
    g_factor = atof(argv[1]);
    if (g_factor <= 0.0) { fprintf(stderr, "factor must be > 0\n"); return 2; }
    if (getenv("DILATE_DEBUG"))     g_debug = atoi(getenv("DILATE_DEBUG")) ? atoi(getenv("DILATE_DEBUG")) : 1;
    const char *off = getenv("DILATE_OFF");
    if (off && *off) {
        if (strstr(off, "vdso"))     g_keep_vdso = 1;
        if (strstr(off, "tsc"))      g_no_tsc = 1;
        if (strstr(off, "epoll"))    g_trap_epoll = 0;
        if (strstr(off, "futex"))    g_off_futex = 1;
        if (strstr(off, "timeouts")) g_off_timeouts = 1;
        if (strstr(off, "clock"))    g_off_clock = 1;
        fprintf(stderr, "[dilate] DILATE_OFF=%s\n", off);
    }
    if (getenv("DILATE_KEEP_VDSO")) g_keep_vdso = 1;
    if (getenv("DILATE_NO_TSC"))    g_no_tsc = 1;
    if (getenv("DILATE_NO_EPOLL"))  g_trap_epoll = 0;

    pid_t pid = fork();
    if (pid == -1) die("fork");
    if (pid == 0) {
        if (ptrace(PTRACE_TRACEME, 0, 0, 0) == -1) die("PTRACE_TRACEME");
        if (!g_no_tsc && prctl(PR_SET_TSC, PR_TSC_SIGSEGV, 0, 0, 0) == -1)
            fprintf(stderr, "[dilate] WARN: PR_SET_TSC failed (%s); rdtsc not scaled\n",
                    strerror(errno));
        install_seccomp_filter();
        raise(SIGSTOP);
        execvp(argv[3], &argv[3]);
        die("execvp");
    }

    int status;
    waitpid(pid, &status, 0);
    long opts = PTRACE_O_TRACESECCOMP | PTRACE_O_EXITKILL |
                PTRACE_O_TRACECLONE | PTRACE_O_TRACEFORK |
                PTRACE_O_TRACEVFORK | PTRACE_O_TRACEEXEC |
                PTRACE_O_TRACESYSGOOD;
    if (ptrace(PTRACE_SETOPTIONS, pid, 0, opts) == -1) die("PTRACE_SETOPTIONS");

    seed_clocks();
    track_pid(pid);
    ptrace(PTRACE_CONT, pid, 0, 0);
    fprintf(stderr, "[dilate] tracing pid %d at factor %.4g\n", pid, g_factor);
    if (g_debug) {
        struct sigaction sa = { .sa_handler = on_alarm };   /* no SA_RESTART */
        sigaction(SIGALRM, &sa, NULL);
        struct itimerval it = { {5, 0}, {5, 0} };
        setitimer(ITIMER_REAL, &it, NULL);
    }

    int main_done = 0, main_code = 0;
    while (g_npids > 0) {
        int ws;
        pid_t w = waitpid(-1, &ws, __WALL);
        if (w == -1) {
            if (errno == ECHILD) break;
            if (errno == EINTR) {
                if (g_stall_flag) { g_stall_flag = 0; dump_stall(); }
                continue;
            }
            die("waitpid");
        }
        if (WIFEXITED(ws) || WIFSIGNALED(ws)) {
            DBG("pid %d exited (status=0x%x)", w, ws);
            untrack_pid(w);
            { int pi = pend_find(w); if (pi >= 0) g_pend[pi].pid = 0; }
            if (w == pid) {
                main_done = 1;
                main_code = WIFEXITED(ws) ? WEXITSTATUS(ws) : 128 + WTERMSIG(ws);
                if (WIFSIGNALED(ws))
                    fprintf(stderr, "[dilate] target (pid %d) killed by signal %d (%s)\n",
                            w, WTERMSIG(ws), sig_name(WTERMSIG(ws)));
                else if (main_code != 0)
                    fprintf(stderr, "[dilate] target (pid %d) exited with status %d\n", w, main_code);
                else
                    DBG("target (pid %d) exited normally", w);
            } else if (WIFSIGNALED(ws) && g_debug) {
                DBG("pid %d killed by signal %d (%s)", w, WTERMSIG(ws), sig_name(WTERMSIG(ws)));
            }
            continue;
        }
        if (!WIFSTOPPED(ws)) continue;
        int sig = WSTOPSIG(ws), event = ws >> 16;

        if (event == PTRACE_EVENT_SECCOMP) {
            dispatch_seccomp_stop(w);
        } else if (event == PTRACE_EVENT_CLONE || event == PTRACE_EVENT_FORK ||
                   event == PTRACE_EVENT_VFORK) {
            unsigned long np = 0;
            if (ptrace(PTRACE_GETEVENTMSG, w, 0, &np) == 0) track_pid((pid_t)np);
            DBG("pid %d fork/clone -> %lu", w, np);
            cont(w);
        } else if (event == PTRACE_EVENT_EXEC) {
            if (!g_keep_vdso) strip_vdso(w);
            if (g_debug) { char exe[256]; exe_of(w, exe, sizeof exe); DBG("pid %d exec %s", w, exe); }
            cont(w);
        } else if (sig == (SIGTRAP | 0x80)) {
            handle_sysgood_stop(w);
        } else {
            track_pid(w);
            if (g_debug && (g_debug >= 2 ? (sig != SIGCHLD && sig != SIGALRM)
                            : (sig == SIGSEGV || sig == SIGILL || sig == SIGABRT ||
                               sig == SIGBUS || sig == SIGFPE || sig == SIGSYS))) {
                regs_t rg; unsigned long long ip = 0;
                if (get_regs(w, &rg) == 0) ip = rg.r.rip;
                DBG("pid %d signal %d (%s) rip=%llx", w, sig, sig_name(sig), ip);
            }
            if (sig == SIGSEGV && !g_no_tsc && try_emulate_tsc(w)) { resume(w, 0); continue; }
            /* Everything else is a real signal for the tracee (including
             * SIGTRAP from int3 / single-step, which Wine and many games
             * rely on) and is passed through.  With PTRACE_O_TRACEEXEC set
             * the kernel sends no legacy post-exec SIGTRAP, so there are no
             * spurious traps to filter. */
            int deliver = sig;
            /* First stop of an auto-attached child is SIGSTOP; job-control
             * stops must not be re-injected or the tracee re-stops forever. */
            if (sig == SIGSTOP || sig == SIGTSTP || sig == SIGTTIN || sig == SIGTTOU)
                deliver = 0;
            resume(w, deliver);
        }
    }
    return main_done ? main_code : 0;
}
