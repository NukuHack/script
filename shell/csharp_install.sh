#!/usr/bin/env bash
# simple script
# Installs the .NET SDK into scratch space (/tmp) to dodge AFS quota limits.
set -euo pipefail

USER_TMP="/tmp/$USER"
INSTALL_DIR="$USER_TMP/dotnet"
NUGET_CACHE="$USER_TMP/nuget-cache"

mkdir -p "$INSTALL_DIR" "$NUGET_CACHE"

if [ -x "$INSTALL_DIR/dotnet" ]; then
    echo "==> .NET SDK already installed at $INSTALL_DIR, skipping download."
else
    echo "==> Installing .NET SDK into $INSTALL_DIR ..."
    curl -fsSL https://dot.net/v1/dotnet-install.sh -o $USER_TMP/dotnet-install-$USER.sh
    chmod +x $USER_TMP/dotnet-install-$USER.sh
    $USER_TMP/dotnet-install-$USER.sh --channel STS --install-dir "$INSTALL_DIR"
    rm -f $USER_TMP/dotnet-install-$USER.sh
fi

echo "==> Verifying..."
"$INSTALL_DIR/dotnet" --version

echo "==> Size (not counted against AFS quota):"
du -sh "$INSTALL_DIR"

echo ""
echo "Done. This script did not touch .bashrc or .bash_profile."
echo "Make sure .bash_profile defines setup_dotnet() and .bashrc calls it."

# Try to refresh the current shell's environment, if this script was sourced.
# If it was run as ./csharp_install.sh (a subshell), this can't reach the
# parent shell - you'll need to run: source ~/.bashrc  yourself.
if [ -n "${BASH_SOURCE:-}" ] && [ "${BASH_SOURCE[0]}" != "$0" ]; then
    echo "==> Sourcing ~/.bashrc into this shell..."
    source ~/.bashrc
else
    echo ""
    echo "NOTE: run this as 'source csharp_install.sh' (or '. csharp_install.sh')"
    echo "      instead of './csharp_install.sh' if you want it to auto-source"
    echo "      ~/.bashrc for you. Otherwise just run: source ~/.bashrc"
fi
