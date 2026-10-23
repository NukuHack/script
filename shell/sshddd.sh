#!/bin/bash
# setup-sasshimi.sh — one-shot setup for SaSSHimi SOCKS5 proxy
# Usage: ./setup-sasshimi.sh [user@host] [local_port]
#        FORCE_REBUILD=1 ./setup-sasshimi.sh
set -euo pipefail

SERVER="${1:-i7zbe7@szamrend.inf.elte.hu}"
PORT="${2:-1080}"

BIN_DIR="$HOME/.local/bin"
BIN="$BIN_DIR/SaSSHimi"
WRAPPER="$BIN_DIR/sasshimi-remote-wrapper.sh"
LAUNCHER="$BIN_DIR/sasshimi-proxy.sh"
REMOTE_BIN=/tmp/SaSSHimi

GO_VERSION=1.21.6
GO_TARBALL="go${GO_VERSION}.linux-amd64.tar.gz"
GO_URL="https://go.dev/dl/${GO_TARBALL}"

build_static_sasshimi() {
    local work; work=$(mktemp -d /tmp/sasshimi-build.XXXXXX)
    cleanup() { [ -d "$work" ] && { chmod -R u+w "$work" 2>/dev/null; rm -rf "$work"; }; return 0; }
    trap cleanup EXIT

    local gopath="$work/gopath" gocache="$work/gocache"
    mkdir -p "$work/go" "$gopath" "$gocache"

    echo "==> Downloading Go ${GO_VERSION} ..."
    if command -v curl >/dev/null; then curl -fsSL "$GO_URL" -o "$work/$GO_TARBALL"
    else wget -q -O "$work/$GO_TARBALL" "$GO_URL"; fi

    echo "==> Extracting Go ..."
    tar -C "$work/go" -xzf "$work/$GO_TARBALL"
    rm -f "$work/$GO_TARBALL"

    export GOROOT="$work/go/go" GOPATH="$gopath" GOCACHE="$gocache"
    export PATH="$GOROOT/bin:$PATH" CGO_ENABLED=0

    echo "==> Building SaSSHimi (static, stripped) ..."
    go install -trimpath -ldflags="-s -w" github.com/rsrdesarrollo/SaSSHimi@latest

    local built; built=$(find "$GOPATH/bin" -maxdepth 1 -type f -executable | head -n1)
    [ -x "${built:-/nonexistent}" ] || { echo "!! build produced no executable" >&2; exit 1; }
    install -m 755 "$built" "$BIN"
    echo "==> Installed $BIN ($(du -h "$BIN" | cut -f1))"
    cleanup; trap - EXIT
}

write_wrapper() {
    cat > "$WRAPPER" <<EOSH
#!/bin/bash
cd /tmp || { echo "SaSSHimi: cannot cd /tmp" >&2; exit 1; }
[ -x $REMOTE_BIN ] || { echo "SaSSHimi: $REMOTE_BIN missing on server — run setup-sasshimi.sh" >&2; exit 1; }
exec $REMOTE_BIN "\$@"
EOSH
    chmod +x "$WRAPPER"
}

stage_remote() {
    echo "==> Uploading $(du -h "$BIN" | cut -f1) to $SERVER:$REMOTE_BIN ..."
    ssh "$SERVER" "cat > $REMOTE_BIN.tmp && chmod 755 $REMOTE_BIN.tmp && mv $REMOTE_BIN.tmp $REMOTE_BIN && test -x $REMOTE_BIN" < "$BIN" \
        || { echo "!! remote staging failed" >&2; exit 1; }
}

write_launcher() {
    cat > "$LAUNCHER" <<EOF
#!/bin/bash
# SaSSHimi SOCKS5 proxy 127.0.0.1:$PORT -> $SERVER
set -euo pipefail
BIN="$BIN"
WRAPPER="$WRAPPER"
[ -x "\$BIN" ]     || { echo "!! \$BIN missing — run setup-sasshimi.sh" >&2; exit 1; }
[ -x "\$WRAPPER" ] || { echo "!! \$WRAPPER missing — run setup-sasshimi.sh" >&2; exit 1; }
if ! ssh -o BatchMode=yes -o ConnectTimeout=3 "$SERVER" "test -x $REMOTE_BIN" 2>/dev/null; then
    echo ">>> Re-staging $REMOTE_BIN on $SERVER ..."
    ssh "$SERVER" "cat > $REMOTE_BIN.tmp && chmod 755 $REMOTE_BIN.tmp && mv $REMOTE_BIN.tmp $REMOTE_BIN" < "\$BIN"
fi
exec "\$BIN" server "$SERVER" --bind "127.0.0.1:$PORT" --remote_executable "\$WRAPPER"
EOF
    chmod +x "$LAUNCHER"
}

main() {
    mkdir -p "$BIN_DIR"
    local rebuild=0
    if [ -x "$BIN" ]; then
        echo "==> Found $BIN ($(du -h "$BIN" | cut -f1)) — $(file -b "$BIN" | cut -d, -f1-2)"
        if [ "${FORCE_REBUILD:-0}" = 1 ]; then
            echo "    FORCE_REBUILD=1 — rebuilding."; rebuild=1
        elif [ -t 0 ]; then
            read -rp "    Rebuild from source? [y/N] " ans
            case "${ans:-N}" in [yY]*) rebuild=1 ;; esac
        else
            echo "    (non-interactive — reusing; FORCE_REBUILD=1 to rebuild.)"
        fi
    else
        echo "==> $BIN not found — building from source."; rebuild=1
    fi

    if [ "$rebuild" = 1 ]; then
        build_static_sasshimi
    elif ! file "$BIN" 2>/dev/null | grep -q "statically linked"; then
        echo "!! WARNING: $BIN does not look statically linked — may fail (GLIBC)." >&2
        echo "!!          Re-run with FORCE_REBUILD=1 to fix." >&2
    fi

    write_wrapper
    stage_remote
    write_launcher

    echo
    echo "All set."
    echo "  Binary : $BIN ($(du -h "$BIN" | cut -f1))"
    echo "  Wrapper: $WRAPPER"
    echo "  Remote : $SERVER:$REMOTE_BIN"
    echo "  Launch : $LAUNCHER"
    echo "  Proxy  : socks5://127.0.0.1:$PORT"
}

main "$@"