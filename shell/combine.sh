#!/bin/bash
#
# combine.sh - Combine files with a filterable expression.
#
# Usage: combine.sh <folder> [options] [filter...]
#

usage() {
    cat <<'EOF'
Usage: combine.sh <folder> [options] [filter...]

Options:
  -o, --output FILE   Output file (default: combined.txt)
  -h, --help          Show this help

Filter expression:
  Combine globs with 'and', 'or', 'not' and parentheses.
  '--git' (or '-g') restricts to git-tracked files.

Operators:  and  &  &&  |  or  ||  !  not  ( )

Examples:
  combine.sh ./src '*.rs'
  combine.sh ./src '*.rs' or '*.toml'
  combine.sh ./src '*.rs' and not '*test*'
  combine.sh ./src --git and not '*.txt'
  combine.sh ./src '(' '*.rs' or '*.toml' ')' and not '*build*'
  combine.sh ./src "--git & !*.txt"
EOF
}

if [ $# -eq 0 ]; then usage; exit 1; fi

FOLDER=""
OUTPUT="combined.txt"
FILTER_ARGS=()

while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        -o|--output)
            if [ $# -lt 2 ]; then echo "Error: -o requires an argument" >&2; exit 1; fi
            OUTPUT="$2"; shift 2 ;;
        *)
            if [ -z "$FOLDER" ]; then FOLDER="$1"
            else FILTER_ARGS+=("$1"); fi
            shift ;;
    esac
done

if [ -z "$FOLDER" ]; then echo "Error: folder is required" >&2; usage; exit 1; fi
if [ ! -d "$FOLDER" ]; then echo "Error: $FOLDER is not a valid directory" >&2; exit 1; fi

case "$OUTPUT" in
    /*) ;;
    *) OUTPUT="$(pwd)/$OUTPUT" ;;
esac

# ---------- Tokenize filter expression ----------
# Each arg is split on whitespace and on operators, then normalised to
# the canonical keywords: and / or / not / ( / )
TOKENS=()
for arg in ${FILTER_ARGS[@]+"${FILTER_ARGS[@]}"}; do
    [ -z "$arg" ] && continue
    s="${arg//&&/ and }"
    s="${s//||/ or }"
    s=$(printf '%s' "$s" | sed -E 's/([()&|!])/ \1 /g')
    for tok in $s; do
        case "$tok" in
            and|AND|\&)  TOKENS+=("and") ;;
            or|OR|\|)    TOKENS+=("or") ;;
            not|NOT|\!)  TOKENS+=("not") ;;
            *)           TOKENS+=("$tok") ;;
        esac
    done
done

GIT_ONLY=0
for t in ${TOKENS[@]+"${TOKENS[@]}"}; do
    case "$t" in --git|-g) GIT_ONLY=1 ;; esac
done

# ---------- Expression parser / evaluator ----------
# Grammar (with implicit-AND between adjacent primaries):
#   or_expr  := and_expr ('or' and_expr)*
#   and_expr := not_expr (('and')? not_expr)*
#   not_expr := 'not' not_expr | primary
#   primary  := '(' or_expr ')' | FILTER
# FILTER is a glob pattern matched against the relative file path,
# or the special token '--git' / '-g' which always matches (file list
# has already been restricted to git-tracked files when it's present).

POS=0
CURRENT_FILE=""

eval_or() {
    eval_and
    local result=$?
    local r
    while [ "${TOKENS[$POS]:-}" = "or" ]; do
        POS=$((POS+1))
        eval_and; r=$?
        [ $r -eq 0 ] && result=0
    done
    return $result
}

eval_and() {
    eval_not
    local result=$?
    local r tok
    while :; do
        tok="${TOKENS[$POS]:-}"
        if [ "$tok" = "and" ]; then
            POS=$((POS+1))
            eval_not; r=$?
            [ $r -ne 0 ] && result=1
        elif [ -z "$tok" ] || [ "$tok" = "or" ] || [ "$tok" = ")" ]; then
            break
        else
            # implicit AND between adjacent primaries
            eval_not; r=$?
            [ $r -ne 0 ] && result=1
        fi
    done
    return $result
}

eval_not() {
    if [ "${TOKENS[$POS]:-}" = "not" ]; then
        POS=$((POS+1))
        eval_not
        local r=$?
        if [ $r -eq 0 ]; then return 1; else return 0; fi
    fi
    eval_primary
}

eval_primary() {
    local tok="${TOKENS[$POS]:-}"
    if [ "$tok" = "(" ]; then
        POS=$((POS+1))
        eval_or
        local r=$?
        if [ "${TOKENS[$POS]:-}" != ")" ]; then
            echo "Error: expected ')' in filter expression" >&2
            exit 1
        fi
        POS=$((POS+1))
        return $r
    fi
    if [ -z "$tok" ]; then
        echo "Error: unexpected end of filter expression" >&2
        exit 1
    fi
    POS=$((POS+1))
    case "$tok" in
        --git|-g) return 0 ;;   # source already filtered to git files
    esac
    [[ "$CURRENT_FILE" == $tok ]]
}

# Validate expression once, before touching any files.
if [ ${#TOKENS[@]} -gt 0 ]; then
    CURRENT_FILE="__validate__"
    POS=0
    eval_or
    if [ "$POS" -ne "${#TOKENS[@]}" ]; then
        echo "Error: unexpected token '${TOKENS[$POS]}' in filter expression" >&2
        exit 1
    fi
fi

# ---------- Gather the candidate file list ----------
cd "$FOLDER" || exit 1
FOLDER_ABS="$(pwd)"

FILES=()
if [ "$GIT_ONLY" = "1" ]; then
    if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        echo "Error: $FOLDER is not a git repository (required by --git)" >&2
        exit 1
    fi
    while IFS= read -r f; do
        [ -n "$f" ] && FILES+=("$f")
    done < <(git ls-files)
else
    while IFS= read -r -d '' f; do
        FILES+=("${f#./}")
    done < <(find . -type f -print0)
fi

# ---------- Combine ----------
> "$OUTPUT"
COUNT=0
for CURRENT_FILE in "${FILES[@]}"; do
    [ -f "$CURRENT_FILE" ] || continue
    # Don't eat our own output if it lives inside the folder.
    [ "$FOLDER_ABS/$CURRENT_FILE" = "$OUTPUT" ] && continue

    include=0
    if [ ${#TOKENS[@]} -eq 0 ]; then
        include=1
    else
        POS=0
        if eval_or; then include=1; fi
    fi

    if [ $include -eq 1 ]; then
        {
            echo "=== $CURRENT_FILE ==="
            cat "$CURRENT_FILE"
            echo ""
        } >> "$OUTPUT"
        COUNT=$((COUNT+1))
    fi
done

echo "Combined $COUNT file(s) into $OUTPUT"