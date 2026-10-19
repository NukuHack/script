
neptun_code="i7zbe7"

if [[ $- == *i* ]]; then
#    if command -v klog >/dev/null 2>&1; then
#        klog i7zbe7
#    else
#        kinit -V i7zbe7@INF.ELTE.HU && aklog
#    fi
    echo ""
fi

if [ "$(id -un)" = "$neptun_code" ] && [ "$(whoami)" = "$neptun_code" ]; then
    if [[ $- == *i* ]]; then
        echo "Hello you Elte student!"

        # enable msg in elte session(s)
        mesg y
    fi
    # c# environment
    export DOTNET_ROOT="/tmp/$USER/dotnet"
    export LD_LIBRARY_PATH="$HOME/.local/lib"
    export NUGET_PACKAGES="/tmp/$USER/nuget-cache"
    add_to_path_if_exists "$DOTNET_ROOT"

    alias fs-quota="fs listquota"

    # tmux moved to "personal temp" from global
    alias tmux='tmux -S ~/tmp/tmux-$(id -u).sock'

else
    echo "You are not $neptun_code, some bash paths are left alone"

fi

