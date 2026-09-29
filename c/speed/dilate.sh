#!/usr/bin/env bash
#
# dilate.sh — one-stop runner for the ptrace/seccomp time dilator.
#
# Expected layout (next to this script):
#   src/dilate.c          (required UNLESS bin/dilate already exists)
#   src/testtarget.c      (optional; auto-generated if missing)
#
# Usage:
#   ./dilate.sh                       # show usage
#   ./dilate.sh build                 # build bin/dilate (+ testtarget)
#   ./dilate.sh test                  # 3-way sanity check (1x, 4x, 0.25x)
#   ./dilate.sh run <factor> <prog> [args...]      # native program under dilate
#   ./dilate.sh wine <factor> <exe> [args...]      # Windows exe under Wine+dilate
#   ./dilate.sh <factor> <prog> [args...]          # shorthand for `run`
#
# Env:
#   CC             compiler (default: gcc)
#   CFLAGS         extra cflags
#   WINE_BIN       wine binary to invoke (default: wine)
#   WINEPREFIX_DILATE  prefix used by `wine` mode (default: ~/.wine-dilate)
#   WINEDEBUG_DILATE   WINEDEBUG value for the run (default: fixme-all, i.e.
#                      Wine's err: lines stay visible; use -all to silence
#                      everything, or err+all,warn+module to dig deeper)
#   DILATE_DEBUG / DILATE_NO_TSC / DILATE_NO_EPOLL / DILATE_KEEP_VDSO
#                      passed through to bin/dilate (see src/dilate.c)

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

SRC_DIR="src"
BIN_DIR="bin"
DILATE_SRC="$SRC_DIR/dilate.c"
TEST_SRC="$SRC_DIR/testtarget.c"
DILATE_BIN="$BIN_DIR/dilate"
TEST_BIN="$BIN_DIR/testtarget"

CC="${CC:-gcc}"
CFLAGS="${CFLAGS:--O2 -Wall -Wextra -std=gnu11}"

# ---- helpers ----------------------------------------------------------

info() { printf '[dilate.sh] %s\n' "$*" >&2; }
warn() { printf '[dilate.sh] WARN: %s\n' "$*" >&2; }
die()  { printf '[dilate.sh] ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
dilate.sh — ptrace/seccomp time dilation for Linux x86_64

USAGE
  dilate.sh <command> [args...]
  dilate.sh <factor> <program> [args...]      # shorthand for `run`

COMMANDS
  build                          Build bin/dilate (and bin/testtarget)
  test                           Build, then run the 3-way sanity check
  run <factor> <prog> [args...]  Run a native program under dilate
  wine <factor> <exe>  [args...] Run a Windows program under Wine+dilate
  doctor [exe]                   Env report; with an exe, A/B test plain wine vs dilate 1.0
  help                           Show this message (also shown with no args)

FACTOR
  > 1.0    speed the target up (its clock runs faster)
  < 1.0    slow it down (bullet time)
  = 1.0    pass-through (correctness baseline)

EXAMPLES
  dilate.sh build
  dilate.sh test
  dilate.sh run 4.0 ./bin/testtarget
  dilate.sh run 0.25 ./mygame -- --fullscreen
  dilate.sh wine 8.0 ./game.exe --windowed

NOTES
  * dilate must LAUNCH the target — it can't attach to a running PID.
  * The vDSO is disabled in the target so clock_gettime & co. become real,
    trappable syscalls (this is what makes dilation actually take effect).
    rdtsc/rdtscp are emulated with a scaled TSC, so Wine's
    QueryPerformanceCounter and engines that read the cycle counter are
    dilated too.
  * Only x86_64 processes are dilated (32-bit compat syscalls are ignored).
  * No root needed for `run` mode (unprivileged process ptracing its own
    child is allowed by default). `wine` mode just needs a writable
    WINEPREFIX.
  * Wine's err: lines are shown by default. If Wine still exits silently,
    run `dilate.sh doctor <exe>`, or DILATE_DEBUG=1 for dilate's own log.
  * Debug knobs: DILATE_DEBUG=1, DILATE_NO_TSC=1, DILATE_NO_EPOLL=1,
    DILATE_KEEP_VDSO=1 (all read by bin/dilate).
EOF
}

# Write a minimal sanity-check test target if the user doesn't have one.
ensure_test_src() {
    [ -f "$TEST_SRC" ] && return 0
    mkdir -p "$SRC_DIR"
    info "src/testtarget.c not found — writing a default one"
    cat > "$TEST_SRC" <<'EOF'
/* testtarget.c — sanity-check target for dilate.
 *
 * Prints CLOCK_REALTIME, CLOCK_MONOTONIC and gettimeofday() readings
 * once a "second" (via nanosleep) for 8 iterations. Under dilate 4.0
 * all three should advance ~4x faster than real wall time.
 */
#include <stdio.h>
#include <time.h>
#include <sys/time.h>
#include <unistd.h>

int main(void) {
    struct timespec real_start, mono_start;
    clock_gettime(CLOCK_REALTIME, &real_start);
    clock_gettime(CLOCK_MONOTONIC, &mono_start);

    for (int i = 0; i < 8; i++) {
        struct timespec rt, mt;
        struct timeval tv;
        clock_gettime(CLOCK_REALTIME, &rt);
        clock_gettime(CLOCK_MONOTONIC, &mt);
        gettimeofday(&tv, NULL);

        double rt_elapsed = (rt.tv_sec - real_start.tv_sec)
                          + (rt.tv_nsec - real_start.tv_nsec) / 1e9;
        double mt_elapsed = (mt.tv_sec - mono_start.tv_sec)
                          + (mt.tv_nsec - mono_start.tv_nsec) / 1e9;

        printf("iter=%d  CLOCK_REALTIME elapsed=%.3fs  "
               "CLOCK_MONOTONIC elapsed=%.3fs  gettimeofday=%ld.%06ld\n",
               i, rt_elapsed, mt_elapsed,
               (long)tv.tv_sec, (long)tv.tv_usec);
        fflush(stdout);

        struct timespec req = { 1, 0 };
        nanosleep(&req, NULL);
    }
    return 0;
}
EOF
}

build_dilate() {
    mkdir -p "$BIN_DIR"
    if [ ! -f "$DILATE_SRC" ]; then
        if [ -x "$DILATE_BIN" ]; then
            warn "$DILATE_SRC not found — using existing $DILATE_BIN"
            return 0
        fi
        die "missing $DILATE_SRC and no prebuilt $DILATE_BIN — put dilate.c under src/"
    fi
    if [ -x "$DILATE_BIN" ] && [ "$DILATE_BIN" -nt "$DILATE_SRC" ]; then
        return 0
    fi
    info "building $DILATE_BIN"
    # shellcheck disable=SC2086
    "$CC" $CFLAGS -o "$DILATE_BIN" "$DILATE_SRC" || die "build of dilate failed"
}

build_test() {
    ensure_test_src
    mkdir -p "$BIN_DIR"
    if [ -x "$TEST_BIN" ] && [ "$TEST_BIN" -nt "$TEST_SRC" ]; then
        return 0
    fi
    info "building $TEST_BIN (static)"
    # shellcheck disable=SC2086
    "$CC" $CFLAGS -static -o "$TEST_BIN" "$TEST_SRC" \
        || die "build of testtarget failed (try installing glibc-static / musl-gcc)"
}

# ---- commands ---------------------------------------------------------

cmd_run() {
    [ $# -ge 2 ] || die "usage: dilate.sh run <factor> <program> [args...]"
    build_dilate
    local factor="$1"; shift
    exec "$DILATE_BIN" "$factor" -- "$@"
}

cmd_wine() {
    [ $# -ge 2 ] || die "usage: dilate.sh wine <factor> <exe> [args...]"
    build_dilate

    local factor="$1"; shift
    local exe="$1"; shift

    [ -f "$exe" ] || die "exe not found: $exe"

    local exe_abs exe_dir
    exe_dir="$(cd -- "$(dirname -- "$exe")" && pwd)"
    exe_abs="$exe_dir/$(basename -- "$exe")"

    local wine_bin="${WINE_BIN:-wine}"
    command -v "$wine_bin" >/dev/null 2>&1 \
        || die "cannot find '$wine_bin' in PATH (override with WINE_BIN=/path/to/wine)"

    local prefix="${WINEPREFIX_DILATE:-$HOME/.wine-dilate}"

    if [ ! -d "$prefix" ]; then
        info "creating fresh WINEPREFIX at $prefix"
        WINEPREFIX="$prefix" WINEDEBUG=-all "$wine_bin" wineboot --init >/dev/null 2>&1 || true
        warn "fresh prefix has no game dependencies (vcredist, d3dx9, ...). If the game exits at once,"
        warn "reuse a working prefix:  WINEPREFIX_DILATE=~/.wine ./dilate.sh wine ...  (or run winetricks on $prefix)"
    fi

    info "killing any wineserver for prefix $prefix (so it respawns under ptrace)"
    WINEPREFIX="$prefix" wineserver -k 2>/dev/null || true
    sleep 0.4

    info "launching: WINEPREFIX=$prefix $wine_bin $exe_abs  under dilate ${factor}x (cwd: $exe_dir)"

    # Run from the game's own directory: most games (and launchers such as
    # portal2.exe) load bin\, steam_api.dll, gameinfo.txt relative to the CWD.
    # WINEDEBUG defaults to fixme-all so Wine's own "err:" lines (missing DLL,
    # failed import, crash reports) are visible instead of silently swallowed.
    # If a game misbehaves try WINEESYNC=0 WINEFSYNC=0 (server-side sync is the
    # most predictable).
    local rc=0
    ( cd "$exe_dir" && \
      env WINEPREFIX="$prefix" WINEDEBUG="${WINEDEBUG_DILATE-fixme-all}" \
        "$SCRIPT_DIR/$DILATE_BIN" "$factor" -- "$wine_bin" "$exe_abs" "$@" ) || rc=$?
    info "target finished with exit code $rc"
    if [ "$rc" -ne 0 ]; then
        warn "non-zero exit. Compare without dilation:  WINEPREFIX=$prefix WINEDEBUG=fixme-all $wine_bin '$exe_abs'"
        warn "then re-run with DILATE_DEBUG=1 (execs/signals/exits are logged)."
    fi
    return "$rc"
}

# Environment report + A/B run so you can tell "dilate broke it" from
# "wine/prefix/game problem".
cmd_doctor() {
    build_dilate
    echo "kernel:        $(uname -r)"
    echo "wine:          $(${WINE_BIN:-wine} --version 2>&1 | head -1)"
    echo "ptrace_scope:  $(cat /proc/sys/kernel/yama/ptrace_scope 2>/dev/null || echo n/a)"
    echo "/dev/ntsync:   $([ -e /dev/ntsync ] && echo present || echo absent)"
    echo "prefix:        ${WINEPREFIX_DILATE:-$HOME/.wine-dilate}"
    if [ $# -ge 1 ]; then
        local exe="$1"; shift
        local exe_dir; exe_dir="$(cd -- "$(dirname -- "$exe")" && pwd)"
        local exe_abs="$exe_dir/$(basename -- "$exe")"
        local prefix="${WINEPREFIX_DILATE:-$HOME/.wine-dilate}"
        echo
        echo "=== A: plain wine, no dilate (10s max) ==="
        ( cd "$exe_dir" && WINEPREFIX="$prefix" WINEDEBUG=fixme-all \
            timeout 10 "${WINE_BIN:-wine}" "$exe_abs" "$@" ) ; echo "exit code: $?"
        WINEPREFIX="$prefix" wineserver -k 2>/dev/null || true; sleep 0.4
        echo
        echo "=== B: dilate 1.0 with DILATE_DEBUG=1 (10s max) ==="
        ( cd "$exe_dir" && WINEPREFIX="$prefix" WINEDEBUG=fixme-all DILATE_DEBUG=1 \
            timeout 10 "$SCRIPT_DIR/$DILATE_BIN" 1.0 -- "${WINE_BIN:-wine}" "$exe_abs" "$@" ) ; echo "exit code: $?"
        WINEPREFIX="$prefix" wineserver -k 2>/dev/null || true
        echo
        echo "If A also exits at once, it's Wine/prefix/game (read the err: lines), not dilate."
    else
        echo
        echo "usage: dilate.sh doctor <exe> [args...]   (adds the A/B test)"
    fi
}

cmd_test() {
    build_dilate
    build_test
    [ -x "$TEST_BIN" ] || die "bin/testtarget missing — cannot run test"

    echo "=== baseline (factor 1.0) ==="
    "$DILATE_BIN" 1.0 -- "$TEST_BIN"

    echo
    echo "=== 4x (should finish ~4x faster in wall time) ==="
    local t0 t1
    t0=$(date +%s.%N)
    "$DILATE_BIN" 4.0 -- "$TEST_BIN"
    t1=$(date +%s.%N)
    awk -v a="$t0" -v b="$t1" 'BEGIN{printf "real wall: %.2fs\n", b-a}'

    # 32-bit check (i386 tracees: wine32 hosts, WoW64 rdtsc, 32-bit games)
    if "$CC" -m32 -static -o "$TEST_BIN"32 "$TEST_SRC" 2>/dev/null; then
        echo
        echo "=== 32-bit binary at 4x (i386 compat path) ==="
        t0=$(date +%s.%N)
        "$DILATE_BIN" 4.0 -- "${TEST_BIN}32"
        t1=$(date +%s.%N)
        awk -v a="$t0" -v b="$t1" 'BEGIN{printf "real wall: %.2fs\n", b-a}'
    else
        warn "skipping 32-bit test (install gcc-multilib / libc6-dev-i386 to enable)"
    fi

    echo
    echo "=== 0.25x (should take ~4x longer in wall time) ==="
    t0=$(date +%s.%N)
    "$DILATE_BIN" 0.25 -- "$TEST_BIN"
    t1=$(date +%s.%N)
    awk -v a="$t0" -v b="$t1" 'BEGIN{printf "real wall: %.2fs\n", b-a}'
}

# ---- main -------------------------------------------------------------

main() {
    if [ $# -eq 0 ]; then
        usage
        exit 0
    fi

    local cmd="$1"

    if [[ "$cmd" =~ ^[0-9]*\.?[0-9]+$ ]]; then
        cmd_run "$@"
        return
    fi

    shift
    case "$cmd" in
        -h|--help|help) usage ;;
        build)          build_dilate; build_test ;;
        test)           cmd_test ;;
        run)            cmd_run "$@" ;;
        wine)           cmd_wine "$@" ;;
        doctor)         cmd_doctor "$@" ;;
        *)              die "unknown command: $cmd (try: $(basename "$0") help)" ;;
    esac
}

main "$@"
