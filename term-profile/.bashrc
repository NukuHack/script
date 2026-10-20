# .bashrc
# This file assumes .bash_profile already loaded in scope.

# Welcome message with system info (only in interactive sessions, so not in sftp)
if [[ $- == *i* ]]; then
    echo "Welcome back, $(whoami)!"
    echo "System: $(uname -srm)"
    echo "Today is $(date)"
    # disable old xoff/xon
    stty -ixon
    
	# Better completion behavior
	bind 'set show-all-if-ambiguous on'
	bind 'set completion-ignore-case on'
	bind 'set colored-stats on'
	bind 'TAB:menu-complete'
fi

# Uncomment the following line if you don't like systemctl's auto-paging feature:
# export SYSTEMD_PAGER=

# User specific aliases and functions
_here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
for rc in "$_here"/*; do
    [ -f "$rc" ] || continue
    [ "$rc" = "$_here/.bashrc" ] && continue
    . "$rc"
done
unset _here rc

# Enable programmable completion features
if ! shopt -oq posix; then
    if [ -f /usr/share/bash-completion/bash_completion ]; then
        . /usr/share/bash-completion/bash_completion
    elif [ -f /etc/bash_completion ]; then
        . /etc/bash_completion
    fi
fi

# Set custom prompt
export PS1="\[\e[32m\]\u\[\e[0m\]:\[\e[34m\]\w\[\e[0m\]$ "

# Safety first
alias rm='rm -i' # Confirm before removing
alias cp='cp -i' # Confirm before overwriting
alias mv='mv -i' # Confirm before overwriting

# Navigation shortcuts
alias ..='cd ..'
alias ...='cd ../..'
alias ....='cd ../../..'
alias .....='cd ../../../..'

alias desktop='cd ~/Desktop'
alias sftp-elte='sftp i7zbe7@szamrend.inf.elte.hu'
alias ssh-elte='ssh i7zbe7@szamrend.inf.elte.hu'
alias idea="/opt/idea/bin/idea"
alias du-l='du -sh -- * .[!.]* 2>/dev/null | sort -h'
alias df-l='df -lha'
alias md='patine README.md --ascii | less -R'
alias re-source="source ~/.bashrc"
alias re-source-f="exec bash -l"

alias webp2='~/.local/share/wp2conv/target/release/wp2conv'

alias dir='ls'
alias open='xdg-open'
alias py='python'
alias cls='clear'
alias sudo='sudo '

alias ls='ls --color=auto'
alias ll='ls -la'
alias la='ls -A'

alias gp='git pull'
alias gps='git push'
alias gf='git fetch'
alias gc='git commit'
alias gch='git checkout'

export HISTCONTROL=erasedups:ignorespace
export HISTIGNORE="clear:ls:ls *:exit:la:la *:cls:..:...:.. *:... *"

