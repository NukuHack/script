#!/bin/bash
#
# setup-sasshimi.sh — one-shot:
#   1. Reuses ~/.local/bin/SaSSHimi if present (prompts to rebuild), else builds it
#   2. Installs/updates the static binary in ~/.local/bin
#   3. Uploads it to <server>:/tmp (atomic, one SSH round-trip)
#   4. Creates/refreshes ~/Desktop/sasshimi-proxy.sh
#
# Usage: ./setup-sasshimi.sh [user@host] [local_port]
#        FORCE_REBUILD=1 ./setup-sasshimi.sh   # skip the prompt, rebuild
#
set -euo pipefail

SERVER="${1:-i7zbe7@szamrend.inf.elte.hu}"
PORT="${2:-1080}"

BIN_DIR="$HOME/.local/bin"
BIN="$BIN_DIR/SaSSHimi"
WRAPPER="$BIN_DIR/sasshimi-remote-wrapper.sh"
LAUNCHER="$HOME/Desktop/sasshimi-proxy.sh"

GO_VERSION="1.21.6"
GO_TARBALL="go${GO_VERSION}.linux-amd64.tar.gz"
GO_URL="https://go.dev/dl/${GO_TARBALL}"

# ---------------------------------------------------------------------------
# Static build (only called when needed). Everything under /tmp, self-cleaning.
# ---------------------------------------------------------------------------
build_static_sasshimi() {
    local work
    work=$(mktemp -d /tmp/sasshimi-build.XXXXXX)

    cleanup_build() {
        [ -d "$work" ] || return 0
        # Go's module cache creates 0555 dirs — rm can't descend without w.
        chmod -R u+w "$work" 2>/dev/null || true
        rm -rf "$work" 2>/dev/null || true
    }
    trap cleanup_build EXIT

    local goroot="$work/go" gopath="$work/gopath" gocache="$work/gocache"
    mkdir -p "$goroot" "$gopath" "$gocache"

    echo "==> Downloading Go ${GO_VERSION} ..."
    if command -v curl >/dev/null; then
        curl -fsSL "$GO_URL" -o "$work/$GO_TARBALL"
    else
        wget -q -O "$work/$GO_TARBALL" "$GO_URL"
    fi

    echo "==> Extracting Go ..."
    tar -C "$goroot" -xzf "$work/$GO_TARBALL"
    rm -f "$work/$GO_TARBALL"

    export GOROOT="$goroot/go"
    export GOPATH="$gopath"
    export GOCACHE="$gocache"
    export PATH="$GOROOT/bin:$PATH"
    export CGO_ENABLED=0          # <-- static binary, any glibc

    echo "==> Building SaSSHimi (static, stripped) ..."
    go install -trimpath -ldflags="-s -w" \
        github.com/rsrdesarrollo/SaSSHimi@latest

    local built
    built=$(find "$GOPATH/bin" -maxdepth 1 -type f -executable | head -n1)
    [ -n "${built:-}" ] && [ -x "$built" ] \
        || { echo "!! build produced no executable" >&2; exit 1; }

    mkdir -p "$BIN_DIR"
    install -m 755 "$built" "$BIN"
    echo "==> Installed $BIN ($(du -h "$BIN" | cut -f1))"

    cleanup_build
    trap - EXIT
}

# ---------------------------------------------------------------------------
# Remote wrapper (uploaded by SaSSHimi on every launch; tiny).
# ---------------------------------------------------------------------------
write_wrapper() {
    cat > "$WRAPPER" <<'EOSH'
#!/bin/bash
cd /tmp || { echo "SaSSHimi: cannot cd /tmp" >&2; exit 1; }
[ -x /tmp/SaSSHimi ] || {
    echo "SaSSHimi: /tmp/SaSSHimi missing on server — run setup-sasshimi.sh" >&2
    exit 1
}
exec /tmp/SaSSHimi "$@"
EOSH
    chmod +x "$WRAPPER"
}

# ---------------------------------------------------------------------------
# Stage to remote. Atomic; one SSH command handles chmod+mv, one prompt on
# password auth.
# ---------------------------------------------------------------------------
stage_remote() {
    echo "==> Uploading $(du -h "$BIN" | cut -f1) to $SERVER:/tmp/SaSSHimi ..."
    ssh "$SERVER" \
        'cat > /tmp/SaSSHimi.tmp && chmod 755 /tmp/SaSSHimi.tmp && mv /tmp/SaSSHimi.tmp /tmp/SaSSHimi && test -x /tmp/SaSSHimi' \
        < "$BIN" \
        || { echo "!! remote staging failed" >&2; exit 1; }
}

# ---------------------------------------------------------------------------
# Desktop launcher (idempotent — regenerated on every setup run).
# ---------------------------------------------------------------------------
write_launcher() {
    mkdir -p "$(dirname "$LAUNCHER")"
    cat > "$LAUNCHER" <<EOF
#!/bin/bash
# SaSSHimi SOCKS5 proxy 127.0.0.1:$PORT -> $SERVER
# Re-stages the binary if /tmp was wiped; otherwise silent.
set -euo pipefail

BIN="\$HOME/bin/SaSSHimi"
WRAPPER="\$HOME/bin/sasshimi-remote-wrapper.sh"
[ -x "\$BIN" ]     || { echo "!! \$BIN missing — run setup-sasshimi.sh" >&2; exit 1; }
[ -x "\$WRAPPER" ] || { echo "!! \$WRAPPER missing — run setup-sasshimi.sh" >&2; exit 1; }

if ! ssh -o BatchMode=yes -o ConnectTimeout=3 "$SERVER" "test -x /tmp/SaSSHimi" 2>/dev/null; then
    echo ">>> Re-staging /tmp/SaSSHimi on $SERVER ..."
    ssh "$SERVER" \\
        'cat > /tmp/SaSSHimi.tmp && chmod 755 /tmp/SaSSHimi.tmp && mv /tmp/SaSSHimi.tmp /tmp/SaSSHimi' \\
        < "\$BIN"
fi

exec "\$BIN" server "$SERVER" \\
    --bind "127.0.0.1:$PORT" \\
    --remote_executable "\$WRAPPER"
EOF
    chmod +x "$LAUNCHER"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    local rebuild=0

    if [ -x "$BIN" ]; then
        echo "==> Found existing $BIN ($(du -h "$BIN" | cut -f1))"
        echo "    $(file -b "$BIN" | cut -d, -f1-2)"
        if [ "${FORCE_REBUILD:-0}" = "1" ]; then
            echo "    FORCE_REBUILD=1 — rebuilding."
            rebuild=1
        elif [ -t 0 ]; then
            read -rp "    Rebuild from source? [y/N] " ans
            case "${ans:-N}" in [yY]*) rebuild=1 ;; esac
        else
            echo "    (non-interactive stdin — reusing. Set FORCE_REBUILD=1 to rebuild.)"
        fi
    else
        echo "==> $BIN not found — building from source."
        rebuild=1
    fi

    if [ "$rebuild" = 1 ]; then
        build_static_sasshimi
    elif ! file "$BIN" 2>/dev/null | grep -q "statically linked"; then
        echo "!! WARNING: $BIN does not look statically linked." >&2
        echo "!!          It may fail on the server with GLIBC errors." >&2
        echo "!!          Re-run with FORCE_REBUILD=1 to fix that." >&2
    fi

    write_wrapper
    stage_remote
    write_launcher

    echo
    echo "All set."
    echo "  Local  : $BIN ($(du -h "$BIN" | cut -f1))"
    echo "  Remote : $SERVER:/tmp/SaSSHimi"
    echo "  Launch : $LAUNCHER"
    echo "  Proxy  : socks5://127.0.0.1:$PORT"
}

main "$@"
