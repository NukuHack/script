#!/bin/bash
# ELTE environment setup for shell sessions.

neptun_code="i7zbe7"
pandora="pandora"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Return 0 if we are inside an SSH session.
is_ssh() {
    # Fast path: sshd exports these into the login shell's environment.
    if [ -n "$SSH_CONNECTION" ] || [ -n "$SSH_CLIENT" ] || [ -n "$SSH_TTY" ]; then
        return 0
    fi
    # Fallback (e.g. after sudo, or if env was scrubbed): inspect the
    # process tree for an sshd ancestor.
    if command -v pstree >/dev/null 2>&1; then
        pstree -s "$$" 2>/dev/null | grep -q sshd && return 0
    fi
    return 1
}

# Return 0 if the current user matches the ELTE neptun code.
# (Checking both id -un and whoami is redundant but kept from the original.)
is_elte_user() {
    [ "$(id -un)" = "$neptun_code" ] && [ "$(whoami)" = "$neptun_code" ]
}

# Environment variables and aliases shared by SSH and local ELTE sessions.
_setup_elte_common() {
    export DOTNET_ROOT="/tmp/$neptun_code/dotnet"
    export LD_LIBRARY_PATH="$HOME/.local/lib"
    add_to_path_if_exists "$DOTNET_ROOT"
    
    local target="/tmp/$neptun_code/tmp"
	local link="$HOME/tmp"
	# remove ~/tmp if it exists (as folder, file, or symlink).
	# then re-add it back correctly
	rm -rf -- "$link"
	mkdir -p -- "$target"
	ln -s -- "$target" "$link"

    alias fs-q="fs listquota"

    # tmux moved from a global path to "personal temp".
    alias tmux="tmux -S $target/tmux-$neptun_code.sock"
}

# ---------------------------------------------------------------------------
# Case 1: SSH session + ELTE user
# ---------------------------------------------------------------------------
setup_elte_ssh() {
    if [[ $- == *i* ]]; then
        echo ""

        echo "Hello you Elte student! (over SSH)"

        if [ "$pandora" == "$(hostname)" ] || [ "$(uname -m)" == "i686" ]; then
            add_to_path_if_exists "$HOME/.local/bin/i686:$PATH"
            echo "You are on 32bit cpu !Awful!"
        else
            add_to_path_if_exists "$HOME/.local/bin/x86_64:$PATH"
        fi

        # Enable mesg in ELTE session(s).
        mesg y
    fi

    # Get Kerberos tickets for the remote session.
    # if command -v klog >/dev/null 2>&1; then
    #     klog "$neptun_code"
    # else
    #     kinit -V "$neptun_code@INF.ELTE.HU" && aklog
    # fi

    _setup_elte_common
}

# ---------------------------------------------------------------------------
# Case 2: Local (non-SSH) session + ELTE user
# ---------------------------------------------------------------------------
setup_elte_local() {
    if [[ $- == *i* ]]; then
        echo ""
        echo "Hello you Elte student! (local)"
    fi

    _setup_elte_common
}

# ---------------------------------------------------------------------------
# Case 3: Any other user
# ---------------------------------------------------------------------------
setup_other_user() {
    echo "You are not $neptun_code, some bash paths are left alone"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if ! is_elte_user; then
    setup_other_user
elif is_ssh; then
    setup_elte_ssh
else
    setup_elte_local
fi


alias sftp-elte="sftp $neptun_code@szamrend.inf.elte.hu"
alias ssh-elte="ssh $neptun_code@szamrend.inf.elte.hu"

