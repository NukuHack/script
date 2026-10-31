# first loaded stuff

# --- Functions (defined here so .bashrc can use them once sourced below) ---

# Add a path to PATH if it exists and isn't already there
add_to_path_if_exists() {
    local dir="$1"
    if [ -d "$dir" ]; then
        case ":$PATH:" in
            *":$dir:"*) ;; # already in PATH, do nothing
            *) export PATH="$dir:$PATH" ;;
        esac
    fi
}

# Source a file if it exists
source_if_exists() {
    local file="$1"
    if [ -f "$file" ]; then
        . "$file"
        return 0
    else
        return 1
    fi
}

# Create and cd into a directory
mkcd() {
    mkdir -p "$1" && cd "$1"
}

md() {
    patine "$1" --ascii | less -R
}
# Extract any archive, whatever its type
extract() {
    if [ -f "$1" ]; then
        case "$1" in
        *.tar.bz2) tar xjf "$1" ;;
        *.tar.gz) tar xzf "$1" ;;
        *.bz2) bunzip2 "$1" ;;
        *.rar) unrar x "$1" ;;
        *.gz) gunzip "$1" ;;
        *.tar) tar xf "$1" ;;
        *.tbz2) tar xjf "$1" ;;
        *.tgz) tar xzf "$1" ;;
        *.zip) unzip "$1" ;;
        *.pak) unzip "$1" ;;
        *.Z) uncompress "$1" ;;
        *.7z) 7z x "$1" ;;
        *) echo "'$1' cannot be extracted via extract()" ;;
        esac
    else
        echo "'$1' is not a valid file"
    fi
}

unzip_one() {
    [ "$#" -eq 3 ] || { echo "usage: unzip_one <archive.zip> <dest_dir> <path>" >&2; return 1; }
    local archive="$1" dest="$2" path="$3"

    [ -f "$archive" ] || { echo "unzip_one: archive not found: $archive" >&2; return 1; }
    mkdir -p -- "$dest" || return 1

    # Normalize: strip trailing slash, then build both patterns.
    local base="${path%/}"
    unzip -o -d "$dest" -- "$archive" "$base" "$base/*"
}

# Create a backup copy of a file
backup() {
    cp "$1" "$1.bak"
}

# Zip up the last commit's files
git-zip() {
    if [ -z "$1" ]; then
        echo "Usage: git-zip <output_filename.zip>"
        return 1
    fi

    local OUTPUT_ZIP="$1"
    git archive --format=zip -o "$OUTPUT_ZIP" HEAD
    echo "Created $OUTPUT_ZIP with files from last commit"
}


mid() {
    local skip=0 count= arg
    while [ $# -gt 0 ]; do
        case "$1" in
            -s) skip=$2; shift 2 ;;
            -n) count=$2; shift 2 ;;
            -*) echo "mid: unknown option $1" >&2; return 1 ;;
            *)  break ;;
        esac
    done

    case "$skip" in ''|*[!0-9]*) echo "mid: -s must be a non-negative integer" >&2; return 1 ;; esac
    if [ -n "$count" ]; then
        case "$count" in ''|*[!0-9]*) echo "mid: -n must be a non-negative integer" >&2; return 1 ;; esac
    fi

    local src tmp=
    if [ $# -eq 0 ] || [ "$1" = "-" ]; then
        tmp=$(mktemp) || return 1
        cat > "$tmp"
        src=$tmp
    else
        src=$1
    fi

    local total
    total=$(wc -l < "$src")
    total=${total// /}

    if [ -n "$count" ] && [ $(( skip + count )) -gt "$total" ]; then
        skip=$(( total - count ))
        [ "$skip" -lt 0 ] && skip=0
    fi

    if [ -n "$count" ]; then
        tail -n +"$(( skip + 1 ))" "$src" | head -n "$count"
    else
        tail -n +"$(( skip + 1 ))" "$src"
    fi

    [ -n "$tmp" ] && rm -f "$tmp"
}


# User specific environment
export SDKMAN_DIR="$HOME/.sdkman"
# binaries, native compiled stuff
add_to_path_if_exists "$HOME/.bun/bin"
add_to_path_if_exists "$HOME/Desktop"
add_to_path_if_exists "$HOME/.local/bin"
add_to_path_if_exists "$HOME/.local/script"
add_to_path_if_exists "$HOME/Desktop/script/shell"
add_to_path_if_exists "$HOME/Desktop/script/python"
add_to_path_if_exists "$HOME/bin"
add_to_path_if_exists "/usr/bin"
add_to_path_if_exists "$HOME/.local/share/flatpak/exports/bin"
add_to_path_if_exists "/var/lib/flatpak/exports/bin"

# And startup programs
source_if_exists "$HOME/.cargo/env"
source_if_exists "$SDKMAN_DIR/bin/sdkman-init.sh"
source_if_exists "/home/nukuhack/.ghcup/env"
source_if_exists "/etc/bashrc"

export PATH


