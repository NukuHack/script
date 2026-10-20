#!/bin/bash
#
# system-backup.sh  v3
#
# Safer backup / restore for Fedora-family (dnf/rpm) systems.
#
# Design principles:
#   - Backup writes only to NEW files in the output dir (creations only).
#     A failed backup cannot lose data. Errors there are [WARN]/[ERR].
#   - Restore modifications are TRANSACTIONAL. Every file we touch that
#     already existed is copied aside first, hashed, and journaled.
#   - A failed modification triggers AUTO-REVERT of that file. If that
#     also fails, we print the exact recovery command and keep the
#     journal on disk (the "recoverable stage").
#   - Creations during restore are safe: if they fail, no data was lost.
#   - Full rollback is offered on every failure, on Ctrl-C, and on exit
#     while a transaction is still active.
#   - Rollback refuses to clobber files that changed since our write
#     (each MODIFY journal line records the new SHA-256 we wrote).
#
# Everything optional is strictly OPT-IN via [1..9]. Nothing is enabled
# by default. Encryption in particular is only [5], never suggested.
#
# Usage:
#   sudo ./system-backup.sh                 # menu: backup or restore
#   sudo ./system-backup.sh backup          # jump to backup
#   sudo ./system-backup.sh restore         # jump to restore
#
# Logging tip (printed at startup as well):
#   sudo ./system-backup.sh 2>&1 | tee ~/sysbackup-$(date +%F).log
#   sudo ./system-backup.sh 2> ~/sysbackup.log
# Interactive prompts read from /dev/tty, so they still work under a pipe.

set -uo pipefail

# ============================================================================
# Constants
# ============================================================================

VERSION="3.0"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"

# Anything here that exists in the archive is restored as <path>.restored-NEW
# (never overwritten), regardless of any policy. Bricking-class files.
CRITICAL_PATHS=(
  "etc/passwd" "etc/shadow" "etc/group" "etc/gshadow"
  "etc/subuid" "etc/subgid"
  "etc/fstab" "etc/crypttab"
  "etc/sudoers" "etc/sudoers.d"
  "etc/pam.d"
  "etc/nsswitch.conf" "etc/login.defs"
  "etc/ssh/sshd_config" "etc/ssh/ssh_config"
  "etc/default/grub" "etc/grub.d"
)

ETC_CURATED=(
  "etc/yum.repos.d" "etc/dnf"
  "etc/NetworkManager/system-connections" "etc/NetworkManager/conf.d"
  "etc/NetworkManager/NetworkManager.conf"
  "etc/hosts" "etc/hostname" "etc/wireguard" "etc/openvpn"
  "etc/default/grub" "etc/grub.d"
  "etc/modprobe.d" "etc/dracut.conf.d" "etc/modules-load.d" "etc/sysctl.d"
  "etc/X11/xorg.conf.d"
  "etc/sysconfig"
  "etc/ssh"
  "etc/firewalld" "etc/cups" "etc/samba" "etc/pki"
  "etc/locale.conf" "etc/vconsole.conf" "etc/environment" "etc/profile.d"
  "etc/security/limits.d" "etc/polkit-1" "etc/selinux/config"
  "etc/subuid" "etc/subgid"
  "etc/systemd/system" "etc/systemd/user"
  "etc/systemd/journald.conf" "etc/systemd/logind.conf"
  "etc/systemd/resolved.conf" "etc/systemd/timesyncd.conf"
  "etc/systemd/coredump.conf" "etc/systemd/system.conf"
  "etc/systemd/user.conf"
  "etc/cron.d" "etc/crontab" "etc/anacrontab"
  "etc/ld.so.conf.d" "etc/chrony.conf"
  "etc/bluetooth/main.conf" "etc/dconf" "etc/gdm"
  "etc/udev/rules.d"
  "var/lib/bluetooth" "var/lib/AccountsService"
)

DOTFILES_LIST=(
  ".bashrc" ".bash_profile" ".bash_aliases" ".profile" ".zshrc" ".inputrc"
  ".gitconfig" ".gitignore_global"
  ".vimrc" ".vim" ".emacs" ".emacs.d" ".tmux.conf" ".tmux"
  ".Xresources" ".Xdefaults" ".npmrc" ".gemrc" ".cargo/config.toml"
  ".config/nvim" ".config/fish" ".config/starship.toml"
  ".config/alacritty" ".config/kitty" ".config/wezterm"
  ".config/i3" ".config/sway" ".config/hypr"
  ".config/polybar" ".config/waybar" ".config/fontconfig"
  ".config/gtk-3.0" ".config/gtk-4.0" ".config/systemd/user"
)

# ============================================================================
# Logging — everything to stderr, so `2>log` captures all of it cleanly.
# Colors are disabled when stderr is not a TTY (pipes, files, CI).
# ============================================================================

if [[ -t 2 ]]; then
  C_RST=$'\033[0m'; C_BLD=$'\033[1m'
  C_RED=$'\033[1;31m'; C_YEL=$'\033[1;33m'; C_GRN=$'\033[1;32m'; C_DIM=$'\033[2m'
else
  C_RST= C_BLD= C_RED= C_YEL= C_GRN= C_DIM=
fi

log_info()  { printf '%s[INFO ]%s %s\n'  "$C_DIM" "$C_RST" "$*" >&2; }
log_ok()    { printf '%s[ OK  ]%s %s\n'  "$C_GRN" "$C_RST" "$*" >&2; }
log_warn()  { printf '%s[WARN ]%s %s\n'  "$C_YEL" "$C_RST" "$*" >&2; }
log_err()   { printf '%s[ERR  ]%s %s\n'  "$C_RED" "$C_RST" "$*" >&2; }
log_fatal() { printf '\n%s[FATAL]%s %s\n' "$C_RED$C_BLD" "$C_RST" "$*" >&2; }
log_crit()  { printf '\n%s[CRIT ]%s %s\n' "$C_RED$C_BLD" "$C_RST" "$*" >&2; }
log_step()  { printf '\n%s=== %s ===%s\n' "$C_BLD" "$*" "$C_RST" >&2; }

banner_logging_tip() {
  cat >&2 <<EOF
${C_DIM}------------------------------------------------------------------------
 ${C_RST}${C_BLD}Tip:${C_RST}${C_DIM} to keep a full log of this run,
${C_RST}
     sudo ./system-backup.sh 2>&1 | tee ~/sysbackup-\$(date +%F).log

 ${C_RST}${C_DIM}or
${C_RST}
     sudo ./system-backup.sh 2> ~/sysbackup.log

 ${C_RST}${C_DIM}Interactive prompts read from /dev/tty, so they still work under a pipe.
${C_RST}${C_DIM}------------------------------------------------------------------------${C_RST}
EOF
}

# ============================================================================
# Prompts (all read from /dev/tty, so pipes/redirection don't break them)
# ============================================================================

ask_yn() {
  local prompt="$1" default="${2:-n}" answer suffix
  [[ "$default" == "y" ]] && suffix="[Y/n]" || suffix="[y/N]"
  while true; do
    read -r -p "$prompt $suffix " answer </dev/tty || return 1
    answer="${answer:-$default}"
    case "${answer,,}" in
      y|yes) return 0 ;;
      n|no)  return 1 ;;
      *) printf 'Please answer y or n.\n' >&2 ;;
    esac
  done
}

ask_str() {
  local prompt="$1" default="${2:-}" answer
  if [[ -n "$default" ]]; then
    read -r -p "$prompt [$default] " answer </dev/tty || return 1
    printf '%s' "${answer:-$default}"
  else
    read -r -p "$prompt " answer </dev/tty || return 1
    printf '%s' "$answer"
  fi
}

# Destructive-confirm: literal uppercase YES.
ask_confirm_yes() {
  local prompt="$1" answer
  printf '%s\n' "$prompt" >&2
  printf 'Type YES (uppercase) to proceed, anything else aborts: ' >&2
  read -r answer </dev/tty || return 1
  [[ "$answer" == "YES" ]]
}

# ============================================================================
# Root re-exec (preserve TERM so colors survive sudo)
# ============================================================================

if [[ $EUID -ne 0 ]]; then
  echo "Re-executing with sudo (needs /etc/shadow, /etc writes)..." >&2
  exec sudo --preserve-env=TERM "$0" "$@"
fi

REAL_USER="${SUDO_USER:-}"
[[ -z "$REAL_USER" || "$REAL_USER" == "root" ]] && REAL_USER="$(logname 2>/dev/null || true)"
[[ -z "$REAL_USER" ]] && REAL_USER="root"
REAL_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"
REAL_UID="$(id -u "$REAL_USER")"
REAL_GID="$(id -g "$REAL_USER")"
[[ -z "$REAL_HOME" ]] && REAL_HOME="/root"

# ============================================================================
# Transaction system
#
# Layout under $TXN_DIR:
#   journal.tsv            — TSV log of every operation, appended before
#                             the write (so a crash mid-write is journaled)
#   files/<abs-path>       — pre-modification copy of each modified file
#
# Journal format (tab-separated):
#   MODIFY <path> <orig_sha256> <new_sha256>
#   CREATE <path> <written_sha256>
# Comment lines begin with '#'.
# ============================================================================

TXN_DIR=""
TXN_JOURNAL=""
TXN_ACTIVE=0

txn_begin() {
  local base="${1:-/var/lib/system-backup-restore}"
  TXN_DIR="$base/rollback-$TIMESTAMP"
  if ! mkdir -p "$TXN_DIR/files" 2>/dev/null; then
    log_fatal "Cannot create transaction directory: $TXN_DIR"
    log_fatal "Refusing to proceed — no safe rollback point."
    return 1
  fi
  chmod 700 "$base" "$TXN_DIR" 2>/dev/null || true
  TXN_JOURNAL="$TXN_DIR/journal.tsv"
  printf '# system-backup.sh v%s transaction started %s\n' \
    "$VERSION" "$(date -Iseconds)" > "$TXN_JOURNAL"
  TXN_ACTIVE=1
  log_info "Transaction: $TXN_DIR"
  log_info "Journal:     $TXN_JOURNAL"
  return 0
}

# Journal the intent BEFORE any write.
_txn_journal() {
  printf '%s\n' "$*" >> "$TXN_JOURNAL"
}

# Path under $TXN_DIR/files/ mirroring an absolute target path.
_txn_bak_path() {
  printf '%s/files/%s' "$TXN_DIR" "${1#/}"
}

# Back up an existing file before modifying it. If this fails, the caller
# MUST refuse to modify. Never modify a file without a successful backup.
txn_backup_before_modify() {
  local target="$1" bak
  bak="$(_txn_bak_path "$target")"
  mkdir -p "$(dirname "$bak")" 2>/dev/null || return 1
  cp -a "$target" "$bak" 2>/dev/null      || return 1
  return 0
}

# Roll back everything in the journal. Refuses to restore a file that has
# changed since our write (the recorded new_sha no longer matches current).
txn_rollback() {
  [[ $TXN_ACTIVE -ne 1 || ! -f "$TXN_JOURNAL" ]] && {
    log_info "Nothing to roll back."
    return 0
  }

  log_warn "=== ROLLBACK: reversing all operations in $TXN_JOURNAL ==="
  local op path orig new current bak rc=0
  local -i done_ok=0 done_skip=0 done_fail=0

  while IFS=$'\t' read -r op path orig new; do
    [[ -z "${op:-}" || "${op:0:1}" == "#" ]] && continue

    case "$op" in
      MODIFY)
        bak="$(_txn_bak_path "$path")"
        if [[ ! -e "$bak" ]]; then
          log_err "No backup for $path — cannot revert"
          (( done_fail++ ))
          continue
        fi
        current=$(sha256sum "$path" 2>/dev/null | cut -d' ' -f1)
        if [[ -n "$new" && "$current" != "$new" ]]; then
          log_warn "External change detected in $path since our write"
          log_warn "  we wrote:  $new"
          log_warn "  now:       ${current:-<missing>}"
          if ! ask_yn "  Restore original over this external change?" n; then
            log_info "  Skipped: $path (kept current content)"
            (( done_skip++ ))
            continue
          fi
        fi
        if cp -a "$bak" "$path" 2>/dev/null; then
          log_ok "Reverted: $path"
          (( done_ok++ ))
        else
          log_err "FAILED to revert $path — backup at $bak"
          (( done_fail++ )); rc=1
        fi
        ;;
      CREATE)
        if [[ ! -e "$path" ]]; then
          continue
        fi
        current=$(sha256sum "$path" 2>/dev/null | cut -d' ' -f1)
        if [[ -n "$new" && "$current" != "$new" ]]; then
          log_warn "$path was created by us, but its content changed since"
          if ! ask_yn "  Remove anyway?" n; then
            log_info "  Kept: $path"
            (( done_skip++ ))
            continue
          fi
        fi
        if rm -f "$path" 2>/dev/null; then
          log_ok "Removed: $path"
          (( done_ok++ ))
        else
          log_err "FAILED to remove $path"
          (( done_fail++ )); rc=1
        fi
        ;;
      *)
        log_warn "Unknown journal op '$op' — skipping"
        ;;
    esac
  done < <(tac "$TXN_JOURNAL")

  printf '\n' >&2
  log_warn "Rollback summary: %d reverted, %d skipped, %d failed" \
    "$done_ok" "$done_skip" "$done_fail"
  TXN_ACTIVE=0
  [[ $rc -eq 0 ]] && log_ok "Rollback complete." || log_err "Rollback finished WITH ERRORS — see above."
  return $rc
}

txn_commit() {
  [[ $TXN_ACTIVE -ne 1 ]] && return 0
  printf '# committed %s\n' "$(date -Iseconds)" >> "$TXN_JOURNAL"
  TXN_ACTIVE=0
  log_ok "Transaction committed."
  log_info "Rollback data kept at: $TXN_DIR"
  log_info "  (Safe to delete once satisfied:  sudo rm -rf '$TXN_DIR')"
}

# ============================================================================
# Safe write primitives
# ============================================================================

# Show a detailed modification warning + diff preview, ask to proceed.
prompt_modification() {
  local src="$1" dst="$2"
  local ssz dsz sh dh
  ssz=$(stat -c%s "$src" 2>/dev/null || echo "?")
  dsz=$(stat -c%s "$dst" 2>/dev/null || echo "?")
  sh=$(sha256sum "$src" | cut -d' ' -f1)
  dh=$(sha256sum "$dst" | cut -d' ' -f1)

  if [[ "$sh" == "$dh" ]]; then
    log_info "  [same]  $dst (identical to archive, skipping)"
    return 1
  fi

  log_warn ""
  log_warn "MODIFICATION of existing file (data-loss risk)"
  log_warn "  Path:     $dst"
  log_warn "  Current:  ${dsz} bytes   sha256 ${dh:0:16}..."
  log_warn "  Incoming: ${ssz} bytes   sha256 ${sh:0:16}..."
  log_warn "  The original will be COPIED to $TXN_DIR BEFORE the write,"
  log_warn "  and can be reverted at any time (per-file or full rollback)."

  local diffout
  diffout=$(diff -u "$dst" "$src" 2>/dev/null | head -40)
  if [[ -n "$diffout" ]]; then
    log_warn "  Diff (first 40 lines):"
    printf '%s\n' "$diffout" | sed 's/^/    /' >&2
  else
    log_warn "  (binary or unreadable — no diff preview)"
  fi

  if ask_yn "  Apply this modification?" n; then
    return 0
  fi
  log_info "  Declined: $dst (original kept)"
  return 1
}

# Safe write. Handles modification-vs-creation distinction. Auto-reverts on
# modification failure. Returns:
#   0 = wrote or safely skipped
#   1 = failed but state is recoverable (auto-reverted or never existed)
#   2 = CRITICAL: file state unknown, manual recovery required
safe_write() {
  local src="$1" dst="$2" policy="$3"
  local sh dh bak

  # ---- CASE A: creation (target does not exist) ----
  if [[ ! -e "$dst" ]]; then
    if cp -a "$src" "$dst" 2>/dev/null; then
      sh=$(sha256sum "$dst" | cut -d' ' -f1)
      _txn_journal "CREATE"$'\t'"$dst"$'\t'"$sh"
      log_ok "  [CREATED]  $dst"
      return 0
    fi
    log_warn "  [CREATE FAILED] $dst"
    log_warn "    Target did not exist before — NO DATA WAS LOST."
    log_warn "    Skipping. The system remains in its pre-restore state for"
    log_warn "    this path. Investigate permissions/space and retry."
    return 1
  fi

  # ---- CASE B: modification (target exists) ----
  case "$policy" in
    skip)   log_info "  [skip]  $dst (exists, policy=skip)"; return 0 ;;
    ask)    prompt_modification "$src" "$dst" || return 0 ;;
    backup) : ;;                       # fall through, always back up
    force)  : ;;                       # fall through
    *)      prompt_modification "$src" "$dst" || return 0 ;;
  esac

  bak="$(_txn_bak_path "$dst")"
  if ! txn_backup_before_modify "$dst"; then
    log_err "REFUSING to modify $dst"
    log_err "  Could not create rollback copy at: $bak"
    log_err "  (Safety invariant: we never modify a file we cannot back up.)"
    return 1
  fi

  local orig_sha new_sha
  orig_sha=$(sha256sum "$dst" | cut -d' ' -f1)
  new_sha=$(sha256sum  "$src" | cut -d' ' -f1)

  # Journal intent BEFORE writing.
  _txn_journal "MODIFY"$'\t'"$dst"$'\t'"$orig_sha"$'\t'"$new_sha"

  if cp -af "$src" "$dst" 2>/dev/null; then
    log_ok "  [MODIFIED] $dst"
    return 0
  fi

  # ---- WRITE FAILED — attempt auto-revert ----
  log_fatal "Modification FAILED: $dst"
  log_fatal "  Source:  $src"
  log_fatal "  Backup:  $bak"
  log_fatal "  This file may be in an INCONSISTENT state."
  log_fatal "Attempting automatic revert from backup..."

  if cp -a "$bak" "$dst" 2>/dev/null; then
    log_warn "  AUTO-REVERT SUCCEEDED — $dst is back to its pre-modification state."
    log_warn "  Backup still available at: $bak"
    return 1
  fi

  log_crit "!!! AUTO-REVERT FAILED — MANUAL RECOVERY REQUIRED !!!"
  log_crit "  File:    $dst"
  log_crit "  Backup:  $bak"
  log_crit "  Journal: $TXN_JOURNAL"
  log_crit ""
  log_crit "  To recover manually:"
  log_crit "      sudo cp -a '$bak' '$dst'"
  log_crit ""
  log_crit "  If the file is unreadable or the system is unstable:"
  log_crit "      reboot into a rescue environment and run the cp above."
  return 2
}

# ============================================================================
# Extras parsing (backup only)
# ============================================================================

parse_extras() {
  local input="$1" tok
  EX_SSH_HOST=0; EX_SSH_USER=0; EX_DOTFILES=0; EX_HOME=0
  EX_ENCRYPT=0;  EX_CHECKSUM=0; EX_FULL_ETC=0; EX_CONTAINER=0; EX_LUKS=0
  [[ "$input" == "none" || -z "$input" ]] && return 0
  for tok in $(printf '%s' "$input" | tr ',' ' '); do
    case "$tok" in
      1) EX_SSH_HOST=1 ;; 2) EX_SSH_USER=1 ;; 3) EX_DOTFILES=1 ;;
      4) EX_HOME=1     ;; 5) EX_ENCRYPT=1  ;; 6) EX_CHECKSUM=1 ;;
      7) EX_FULL_ETC=1 ;; 8) EX_CONTAINER=1;; 9) EX_LUKS=1 ;;
      *) log_warn "Ignoring unknown option '$tok'" ;;
    esac
  done
}

show_extras_menu() {
  cat >&2 <<'EOF'

Optional extras — nothing is enabled unless you pick it:

  [1] SSH host keys        (WARNING: reusing host keys defeats their purpose)
  [2] ~/.ssh               (personal SSH keys & config — sensitive)
  [3] Dotfiles             (small shell/editor/git/app config files)
  [4] Home directory       (full archive — can be very large)
  [5] Encrypt output       (age preferred, gpg fallback — OPT-IN ONLY)
  [6] SHA-256 checksums    (verified on restore before anything is written)
  [7] Full /etc            (not curated — includes everything)
  [8] Container/VM data    (/var/lib/docker, /var/lib/libvirt)
  [9] LUKS header backup   (only useful if written to a SEPARATE disk)

Enter numbers separated by commas/spaces, or 'none'.
EOF
}

# ============================================================================
# Backup
# ============================================================================

do_backup() {
  log_step "BACKUP"

  show_extras_menu
  local extras
  extras="$(ask_str 'Selection (or none)')" || return 1
  parse_extras "$extras"

  local default_out="$REAL_HOME/system-backup-$TIMESTAMP"
  local outdir
  outdir="$(ask_str 'Output directory' "$default_out")" || return 1
  if [[ -e "$outdir" ]]; then
    log_warn "Output directory already exists: $outdir"
    ask_yn "Continue and write into it?" n || { log_info "Aborted."; return 1; }
  fi
  mkdir -p "$outdir" || { log_err "Cannot create $outdir"; return 1; }

  # Stage inside the output dir (same filesystem; not /tmp tmpfs).
  local stage="$outdir/.staging"
  mkdir -p "$stage/lists" || { log_err "Cannot create $stage"; return 1; }

  log_info "Output:  $outdir"
  log_info "Staging: $stage"

  # --- [A] etc ---------------------------------------------------------------
  log_info "Building etc archive..."
  local paths=() missing=() p
  if [[ $EX_FULL_ETC -eq 1 ]]; then
    paths=("etc")
    log_warn "Full /etc selected — includes caches, machine-id, etc."
  else
    for p in "${ETC_CURATED[@]}"; do
      [[ -e "/$p" ]] && paths+=("$p") || missing+=("$p")
    done
  fi

  local excl=("--exclude=etc/ssh/ssh_host_*")
  [[ $EX_SSH_HOST -eq 1 ]] && excl=()

  tar --create --gzip --ignore-failed-read \
      --acls --xattrs --xattrs-include='*' \
      "${excl[@]}" -f "$stage/etc-minimal.tgz" -C / "${paths[@]}"
  local rc=$?
  if [[ $rc -ne 0 ]]; then
    log_warn "tar exited $rc for etc-minimal.tgz (usually benign — sockets, live files)"
  fi
  if [[ ! -s "$stage/etc-minimal.tgz" ]]; then
    log_err "etc-minimal.tgz missing or empty — aborting."
    return 1
  fi
  log_ok "etc-minimal.tgz: $(du -h "$stage/etc-minimal.tgz" | cut -f1)"
  [[ ${#missing[@]} -gt 0 ]] && log_info "Skipped (absent): ${missing[*]}"

  # --- [B] lists -------------------------------------------------------------
  log_info "Collecting inventories..."
  capture() {
    local out="$1"; shift
    local label="$1"; shift
    if command -v "$1" >/dev/null 2>&1; then
      "$@" >"$stage/lists/$out" 2>&1 \
        && log_info "  [ok]   $label" \
        || log_warn "  [warn] $label (nonzero exit)"
    else
      echo "# $1: command not found" >"$stage/lists/$out"
      log_info "  [skip] $label (not installed)"
    fi
  }
  capture_as_user() {
    local out="$1"; shift
    local label="$1"; shift
    if command -v "$1" >/dev/null 2>&1; then
      runuser -u "$REAL_USER" -- "$@" >"$stage/lists/$out" 2>&1 \
        && log_info "  [ok]   $label" \
        || log_warn "  [warn] $label (nonzero exit)"
    else
      echo "# $1: command not found" >"$stage/lists/$out"
      log_info "  [skip] $label (not installed)"
    fi
  }

  if command -v dnf >/dev/null 2>&1; then
    capture dnf-installed.txt     "dnf installed"     dnf repoquery --installed --qf '%{name}\n'
    capture dnf-userinstalled.txt "dnf userinstalled" dnf repoquery --userinstalled --qf '%{name}\n'
    capture dnf-groups.txt        "dnf groups"        dnf group list --installed
    capture dnf-enabled-repos.txt "dnf repos enabled" dnf repolist --enabled
    capture dnf-repos-all.txt     "dnf repos all"     dnf repolist --all
  fi
  capture rpm-all.txt "rpm -qa" rpm -qa --qf '%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\n'

  if command -v flatpak >/dev/null 2>&1; then
    capture flatpak-apps.txt     "flatpak apps"     flatpak list --app --columns=application
    capture flatpak-refs.txt     "flatpak refs"     flatpak list --app --columns=ref
    capture flatpak-apps.tsv     "flatpak detail"   flatpak list --app --columns=application,name,version,branch,origin
    capture flatpak-runtimes.tsv "flatpak runtimes" flatpak list --runtime --columns=application,version,branch,origin
    capture flatpak-remotes.txt  "flatpak remotes"  flatpak remotes
  fi

  capture_as_user vscode-extensions.txt "VS Code extensions" code --list-extensions
  capture_as_user dconf.ini             "dconf dump"        dconf dump /

  capture lsblk.txt               "block devices" lsblk -f -o NAME,FSTYPE,LABEL,UUID,MOUNTPOINT,SIZE
  capture fdisk.txt               "fdisk -l"      fdisk -l
  capture df.txt                  "df -hT"        df -hT
  capture mount.txt               "mounts"        mount
  capture swapon.txt              "swap"          swapon --show
  capture lspci.txt               "pci+drivers"   lspci -k
  capture lsusb.txt               "usb"           lsusb
  capture lscpu.txt               "cpu"           lscpu
  capture dmidecode.txt           "dmi"           dmidecode
  capture nvidia-smi.txt          "nvidia-smi"    nvidia-smi
  capture mok-enrolled.txt        "mok enrolled"  mokutil --list-enrolled
  capture sestatus.txt            "selinux"       sestatus
  capture timedatectl.txt         "timedatectl"   timedatectl
  capture localectl.txt           "localectl"     localectl
  capture nmcli-connections.txt   "nmcli"         nmcli connection show
  capture systemctl-unitfiles.txt "unit files"    systemctl list-unit-files
  capture systemctl-enabled.txt   "enabled units" systemctl list-unit-files --state=enabled

  {
    find /usr/share/applications -maxdepth 1 -name '*.desktop' 2>/dev/null
    find "$REAL_HOME/.local/share/applications" -maxdepth 1 -name '*.desktop' 2>/dev/null
  } | sort > "$stage/lists/desktop-files.txt"

  tar --create --xz -f "$stage/lists_tar.xz" -C "$stage" lists
  if [[ $? -ne 0 || ! -s "$stage/lists_tar.xz" ]]; then
    log_err "lists_tar.xz failed"
    return 1
  fi
  log_ok "lists_tar.xz: $(du -h "$stage/lists_tar.xz" | cut -f1)"

  # --- [C] Optional home / dotfiles / ssh / container / luks -----------------
  if [[ $EX_HOME -eq 1 && -d "$REAL_HOME" ]]; then
    log_info "Archiving home ($(du -sh "$REAL_HOME" 2>/dev/null | cut -f1)) ..."
    tar --create --xz --one-file-system \
        --acls --xattrs --xattrs-include='*' \
        -f "$stage/home.tar.xz" \
        -C "$(dirname "$REAL_HOME")" "$(basename "$REAL_HOME")"
    [[ $? -ne 0 ]] && log_warn "home.tar.xz may be incomplete (tar nonzero)"
    [[ -s "$stage/home.tar.xz" ]] && log_ok "home.tar.xz: $(du -h "$stage/home.tar.xz" | cut -f1)"
  fi

  if [[ $EX_DOTFILES -eq 1 ]]; then
    local df_args=() d
    for d in "${DOTFILES_LIST[@]}"; do
      [[ -e "$REAL_HOME/$d" ]] && df_args+=("$d")
    done
    if [[ ${#df_args[@]} -gt 0 ]]; then
      tar --create --gzip -f "$stage/dotfiles.tar.gz" -C "$REAL_HOME" "${df_args[@]}"
      [[ $? -eq 0 ]] && log_ok "dotfiles.tar.gz: $(du -h "$stage/dotfiles.tar.gz" | cut -f1)"
    else
      log_warn "No dotfiles found."
    fi
  fi

  if [[ $EX_SSH_USER -eq 1 && -d "$REAL_HOME/.ssh" ]]; then
    tar --create --gzip -f "$stage/user-ssh.tar.gz" -C "$REAL_HOME" .ssh
    [[ $? -eq 0 ]] && log_ok "user-ssh.tar.gz: $(du -h "$stage/user-ssh.tar.gz" | cut -f1)"
  fi

  if [[ $EX_SSH_HOST -eq 1 ]]; then
    local hk=(/etc/ssh/ssh_host_*)
    if [[ -e "${hk[0]:-}" ]]; then
      tar --create --gzip -f "$stage/ssh-host-keys.tar.gz" -C / etc/ssh/ssh_host_*
      log_warn "SSH host keys archived — do NOT restore onto another machine."
    fi
  fi

  if [[ $EX_CONTAINER -eq 1 ]]; then
    for d in /var/lib/docker /var/lib/libvirt /var/lib/containers; do
      [[ -d "$d" ]] || continue
      local name="container-$(basename "$d").tar.xz"
      log_info "Archiving $d ..."
      tar --create --xz --one-file-system -f "$stage/$name" -C / "${d#/}"
      [[ $? -ne 0 ]] && log_warn "$name may be incomplete"
    done
  fi

  if [[ $EX_LUKS -eq 1 ]]; then
    mkdir -p "$stage/luks-headers"
    local dev
    for dev in $(lsblk -rno NAME,FSTYPE 2>/dev/null | awk '$2=="crypto_LUKS"{print $1}'); do
      log_info "LUKS header for /dev/$dev ..."
      cryptsetup luksHeaderBackup "/dev/$dev" \
        --header-backup-file "$stage/luks-headers/luks-header-$dev.bin" 2>/dev/null \
        || log_warn "  could not back up /dev/$dev"
    done
    log_warn "LUKS headers are ONLY useful copied to a DIFFERENT physical disk."
  fi

  # --- [D] checksums (opt-in only via [6]) -----------------------------------
  if [[ $EX_CHECKSUM -eq 1 ]]; then
    ( cd "$stage" && \
      find . -maxdepth 1 -type f -printf '%P\n' | sort | \
      while read -r f; do sha256sum "$f"; done > SHA256SUMS )
    [[ -s "$stage/SHA256SUMS" ]] && log_ok "SHA256SUMS written."
  fi

  # --- [E] manifest ----------------------------------------------------------
  cat > "$stage/MANIFEST.txt" <<EOF
System backup manifest
======================
Created:  $(date -Iseconds)
Version:  $VERSION
Host:     $(hostname)
User:     $REAL_USER  ($REAL_HOME)
OS:       $(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME")
Extras:   ssh-host=$EX_SSH_HOST ssh-user=$EX_SSH_USER dotfiles=$EX_DOTFILES
          home=$EX_HOME encrypt=$EX_ENCRYPT checksum=$EX_CHECKSUM
          full-etc=$EX_FULL_ETC container=$EX_CONTAINER luks=$EX_LUKS

Contents:
$(cd "$stage" && ls -la | tail -n +2)

This is a config + inventory backup, NOT a full system image.
EOF

  # --- [F] move staging up ---------------------------------------------------
  local f
  for f in "$stage"/*; do
    [[ -e "$f" ]] || continue
    mv "$f" "$outdir/"
  done
  rm -rf "$stage"

  # --- [G] encryption — OPT-IN ONLY via [5] ----------------------------------
  if [[ $EX_ENCRYPT -eq 1 ]]; then
    local tool="" plain kept=0 encrypted=0
    if command -v age >/dev/null 2>&1; then tool=age
    elif command -v gpg >/dev/null 2>&1; then tool=gpg
    fi
    if [[ -z "$tool" ]]; then
      log_err "Encryption requested but neither 'age' nor 'gpg' is installed."
      log_err "  Install one and re-run, or omit [5]. Archives left UNENCRYPTED."
    else
      log_info "Encrypting with $tool (plaintext kept until you confirm)."
      for f in "$outdir"/*.tgz "$outdir"/*.xz "$outdir"/*.gz; do
        [[ -f "$f" ]] || continue
        log_info "  $tool: $(basename "$f")"
        if [[ "$tool" == age ]]; then
          age -p -o "${f}.age" "$f" 2>/dev/null && encrypted=$((encrypted+1)) \
            || { log_err "  encryption failed: $f"; kept=$((kept+1)); }
        else
          gpg --symmetric --cipher-algo AES256 --yes -o "${f}.gpg" "$f" 2>/dev/null \
            && encrypted=$((encrypted+1)) \
            || { log_err "  encryption failed: $f"; kept=$((kept+1)); }
        fi
      done
      log_ok "Encrypted $encrypted archive(s)."
      if [[ $kept -gt 0 ]]; then
        log_warn "$kept archive(s) could NOT be encrypted — plaintext kept."
      fi
      if [[ $encrypted -gt 0 ]]; then
        log_warn ""
        log_warn "SAFE-DELETE is intentionally NOT automatic."
        log_warn "  Make sure you've saved your passphrase, then delete plaintext:"
        log_warn "    for f in $outdir/*.{tgz,xz,gz}; do"
        log_warn "      [ -f \"\$f\" ] && ${tool} -d -o /dev/null \"\$f.\"* && shred -u \"\$f\""
        log_warn "    done"
        log_warn "  (Or leave plaintext in place for now — nothing is deleted automatically.)"
      fi
    fi
  fi

  # --- [H] ownership & permissions ------------------------------------------
  chown -R "$REAL_UID:$REAL_GID" "$outdir" 2>/dev/null || true
  chmod 700 "$outdir"
  find "$outdir" -maxdepth 1 -type f -exec chmod 600 {} \; 2>/dev/null || true
  [[ -f "$outdir/SHA256SUMS" ]] && chmod 600 "$outdir/SHA256SUMS"

  log_step "Backup complete"
  log_ok "$outdir"
  echo >&2
  echo "  Archives are mode 600, owned by $REAL_USER." >&2
  echo "  etc-minimal.tgz contains /etc/shadow, Wi-Fi PSKs, akmods private key." >&2
  echo "  Copy to an encrypted disk BEFORE reinstalling." >&2
  echo >&2
  echo "  ─── NOT INCLUDED IN THIS BACKUP ───────────────────────────" >&2
  [[ $EX_HOME      -eq 0 ]] && echo "  /home — documents, projects, pictures. Back up separately (rsync/restic/borg)." >&2
  [[ $EX_DOTFILES  -eq 0 ]] && echo "  Dotfiles (~/.bashrc, ~/.config/…)  — re-run with [3] if wanted." >&2
  [[ $EX_SSH_USER  -eq 0 ]] && echo "  ~/.ssh personal keys                — re-run with [2] if wanted." >&2
  [[ $EX_CONTAINER -eq 0 ]] && echo "  Container/VM data (/var/lib/docker, /var/lib/libvirt) — re-run with [8]." >&2
  [[ $EX_LUKS      -eq 0 ]] && echo "  LUKS headers — re-run with [9] and write to a DIFFERENT disk." >&2
  echo "  Installed software binaries — only package NAME lists are saved; reinstall via dnf/flatpak." >&2
  echo >&2
  echo "  Designed to pair with a FRESH REINSTALL of the SAME Fedora" >&2
  echo "  version. This is a config+inventory backup, NOT a system image." >&2
  return 0
}

# ============================================================================
# Restore
# ============================================================================

is_critical_path() {
  local rel="$1" c
  for c in "${CRITICAL_PATHS[@]}"; do
    [[ "$rel" == "$c" || "$rel" == "$c"/* ]] && return 0
  done
  return 1
}

# Restore a critical path as <path>.restored-NEW. Never overwrite.
restore_critical_file() {
  local tmp="$1" rel="$2"
  local src="$tmp/$rel" dst="/$rel" new="/${rel}.restored-NEW"
  if [[ ! -e "$src" ]]; then
    log_info "  [skip] $rel (not in archive)"
    return 0
  fi
  if [[ ! -e "$dst" ]]; then
    if cp -a "$src" "$dst" 2>/dev/null; then
      log_ok "  [NEW]  $dst"
    else
      log_warn "  [CREATE FAILED] $dst — no data lost"
    fi
    return 0
  fi
  if cmp -s "$src" "$dst"; then
    log_info "  [same] $dst"
    return 0
  fi
  if cp -a "$src" "$new" 2>/dev/null; then
    log_warn "  [REVIEW] $dst"
    log_warn "           original kept, incoming copy at: $new"
    log_warn "           diff '$dst' '$new'      # inspect"
    log_warn "           (for sudoers:  sudo visudo -c -f '$new')"
  else
    log_err "  [FAIL] could not write $new"
  fi
}

# Walk the extracted /etc tree and dispatch.
restore_etc_tree() {
  local tmp="$1" policy="$2" f rel rc worst=0

  # First pass: critical files (never overwritten).
  log_info "Critical paths -> .restored-NEW (never overwritten):"
  for f in "${CRITICAL_PATHS[@]}"; do
    restore_critical_file "$tmp" "$f"
  done

  # Second pass: everything else, using safe_write.
  log_info "Non-critical paths (policy=$policy):"
  local -i mods=0 creates=0 fails=0
  while IFS= read -r f; do
    rel="${f#./}"
    is_critical_path "$rel" && continue
    if safe_write "$tmp/$rel" "/$rel" "$policy"; then
      :
    else
      rc=$?
      (( fails++ ))
      [[ $rc -gt $worst ]] && worst=$rc
      if [[ $rc -eq 2 ]]; then
        log_crit "Critical failure — aborting restore loop."
        break
      fi
    fi
  done < <(cd "$tmp" && find . -type f)

  log_info "Non-critical summary: $mods modified, $creates created, $fails failed/skipped"
  return $worst
}

do_restore() {
  log_step "RESTORE"

  local dir
  dir="$(ask_str 'Backup directory to restore from')" || return 1
  [[ -d "$dir" ]] || { log_err "Not a directory: $dir"; return 1; }

  log_info "Contents of $dir:"
  ls -la "$dir" >&2
  
  echo >&2
  echo "  Restoring onto a fresh install of the SAME Fedora version." >&2
  echo "  /home is NOT in etc-minimal.tgz — restore it from your own backup." >&2
  echo >&2
  echo "  Quick reference — where to find things:" >&2
  echo "    Packages to reinstall:   lists/dnf-userinstalled.txt        (menu [2])" >&2
  echo "    Installed dnf groups:    lists/dnf-groups.txt               (dnf group install …)" >&2
  echo "    Flatpak apps:            lists/flatpak-apps.txt             (see menu [2])" >&2
  echo "    Repos:                   etc/yum.repos.d/, dnf-enabled-repos.txt  (menu [1])" >&2
  echo "    Wi-Fi/VPN:               etc/NetworkManager/system-connections/    (menu [3])" >&2
  echo "    GNOME/dconf:             lists/dconf.ini                    (menu [7])" >&2
  echo "    Previously-enabled svc:  etc/systemd/system/*.target.wants/ (menu [6])" >&2
  echo "    Hardware / drivers:      lists/lspci.txt, nvidia-smi.txt    (menu [9])" >&2
  echo >&2

  # --- Decryption -----------------------------------------------------------
  local enc
  enc=$(find "$dir" -maxdepth 1 \( -name '*.age' -o -name '*.gpg' \) | head -1)
  if [[ -n "$enc" ]]; then
    if ask_yn "Encrypted archives present — decrypt now?" y; then
      local f
      for f in "$dir"/*.age; do
        [[ -f "$f" ]] || continue
        command -v age >/dev/null || { log_err "age not installed"; return 1; }
        age -d -o "${f%.age}" "$f" || { log_err "decrypt failed: $f"; return 1; }
      done
      for f in "$dir"/*.gpg; do
        [[ -f "$f" ]] || continue
        gpg --yes -o "${f%.gpg}" --decrypt "$f" || { log_err "decrypt failed: $f"; return 1; }
      done
      log_ok "Decryption complete."
    else
      log_warn "Continuing without decryption — restore options will mostly no-op."
    fi
  fi

  # --- Checksum verification (before ANY write) ----------------------------
  if [[ -f "$dir/SHA256SUMS" ]]; then
    if ask_yn "Verify SHA-256 checksums before restoring?" y; then
      ( cd "$dir" && sha256sum -c SHA256SUMS ) >&2 || {
        log_err "Checksum verification FAILED."
        ask_yn "Continue anyway (DANGEROUS)?" n || return 1
      }
      log_ok "Checksums verified."
    fi
  else
    log_info "No SHA256SUMS — skipping verification."
  fi

  # --- Extract etc to temp (creations only — safe even if it fails) --------
  local tmp=""
  if [[ -f "$dir/etc-minimal.tgz" ]]; then
    tmp=$(mktemp -d /tmp/restore.XXXXXX)
    tar -xzf "$dir/etc-minimal.tgz" -C "$tmp" \
      || { log_err "Extraction of etc-minimal.tgz failed (no data lost)"; return 1; }
    log_ok "Extracted etc-minimal.tgz -> $tmp"
  else
    log_warn "No etc-minimal.tgz — repo/config restore options will no-op."
  fi

  # --- Begin transaction (refuse to proceed if we cannot) ------------------
  if ! txn_begin; then
    [[ -n "$tmp" ]] && rm -rf "$tmp"
    return 1
  fi

  # ============ Interactive menu ===========================================
  local rc
  while true; do
    cat >&2 <<EOF

${C_BLD}Restore menu${C_RST} — source: $dir
${C_DIM}Transaction journal: $TXN_JOURNAL${C_RST}

  [1] Re-add package repos
  [2] Show package-reinstall command (does NOT run it)
  [3] Restore Wi-Fi / VPN connections
  [4] Restore /etc non-critical configs (with overwrite policy)
  [5] Extract critical /etc files as .restored-NEW (never overwrites)
  [6] Show previously enabled services
  [7] Restore dconf + VS Code extensions
  [8] Restore home directory (merge / replace / alternate)
  [9] Show hardware + driver checklist
  [R] Review transaction journal (dry-run listing)
  [U] Undo — roll back EVERYTHING done so far
  [0] Finish (commit transaction)

EOF
    local choice
    choice="$(ask_str 'Choose')" || break

    case "$choice" in
      1)
        [[ -z "$tmp" ]] && { log_warn "No etc archive."; continue; }
        if [[ -d "$tmp/etc/yum.repos.d" ]]; then
          log_info "Adding repo files..."
          mkdir -p /etc/yum.repos.d
          for r in "$tmp/etc/yum.repos.d"/*.repo; do
            [[ -f "$r" ]] || continue
            local bn; bn=$(basename "$r")
            safe_write "$r" "/etc/yum.repos.d/$bn" ask
          done
          dnf clean all >/dev/null 2>&1 && dnf makecache >/dev/null 2>&1 \
            && log_ok "dnf cache refreshed."
        fi
        ;;

      2)
        if [[ -f "$dir/lists_tar.xz" ]]; then
          local ltmp; ltmp=$(mktemp -d /tmp/lists.XXXXXX)
          tar -xJf "$dir/lists_tar.xz" -C "$ltmp"

          if [[ -f "$ltmp/lists/dnf-userinstalled.txt" ]]; then
            echo >&2
            echo "── dnf packages ── (review the list first)" >&2
            echo "  sudo dnf install --skip-unavailable \\" >&2
            echo "    \$(cat $ltmp/lists/dnf-userinstalled.txt)" >&2
            echo "  ($(wc -l < "$ltmp/lists/dnf-userinstalled.txt") packages)" >&2
          fi

          if [[ -f "$ltmp/lists/dnf-groups.txt" ]]; then
            echo >&2
            echo "── dnf groups ── (if you had any, listed in $ltmp/lists/dnf-groups.txt)" >&2
            echo "  sudo dnf group install --skip-unavailable '<group-name>'" >&2
          fi

          if [[ -f "$ltmp/lists/flatpak-apps.txt" ]]; then
            echo >&2
            echo "── flatpaks ── (add Flathub first if needed)" >&2
            echo "  flatpak remote-add --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo" >&2
            echo "  while read -r app; do flatpak install -y flathub \"\$app\"; done \\" >&2
            echo "    < $ltmp/lists/flatpak-apps.txt" >&2
          fi

          echo >&2
          echo "  Lists are still available under: $ltmp/lists/" >&2
          echo "  (They will be deleted when you exit this restore session.)" >&2
          # rm -rf "$ltmp"     <-- do NOT delete yet; see note below
        else
          log_warn "No lists archive."
        fi
        ;;

      3)
        if [[ -z "$tmp" || ! -d "$tmp/etc/NetworkManager/system-connections" ]]; then
          log_warn "No NM connections in archive."; continue
        fi
        mkdir -p /etc/NetworkManager/system-connections
        for c in "$tmp/etc/NetworkManager/system-connections"/*.nmconnection; do
          [[ -f "$c" ]] || continue
          local bn; bn=$(basename "$c")
          safe_write "$c" "/etc/NetworkManager/system-connections/$bn" ask \
            && { chmod 600 "/etc/NetworkManager/system-connections/$bn"
                 chown root:root "/etc/NetworkManager/system-connections/$bn"; }
        done
        systemctl restart NetworkManager 2>/dev/null \
          && log_ok "NetworkManager restarted."
        ;;

      4)
        [[ -z "$tmp" ]] && { log_warn "No etc archive."; continue; }
        echo >&2
        echo "Overwrite policy for existing files in /etc:" >&2
        echo "  [1] Ask per file   (safest, slow)" >&2
        echo "  [2] Skip existing  (safe, fast)" >&2
        echo "  [3] Backup + overwrite" >&2
        echo "  [4] Overwrite silently (DANGEROUS)" >&2
        local pmode mode
        pmode="$(ask_str 'Policy [1-4]' 2)" || continue
        case "$pmode" in
          1) mode=ask ;;
          2) mode=skip ;;
          3) mode=backup ;;
          4) ask_confirm_yes "Confirm SILENT OVERWRITE of /etc?" \
               && mode=force || { log_info "Aborted."; continue; } ;;
          *) mode=skip ;;
        esac
        log_warn "Critical files go to .restored-NEW regardless of policy."
        if ask_yn "Proceed?" n; then
          restore_etc_tree "$tmp" "$mode" || rc=$?
          if [[ ${rc:-0} -eq 2 ]]; then
            log_crit "Restore loop hit a critical failure."
            if ask_yn "Roll back ALL modifications now?" y; then
              txn_rollback
            fi
          fi
        fi
        ;;

      5)
        [[ -z "$tmp" ]] && { log_warn "No etc archive."; continue; }
        log_warn "These will be written as <path>.restored-NEW, NOT over originals:"
        printf '  %s\n' "${CRITICAL_PATHS[@]}" >&2
        if ask_yn "Proceed?" y; then
          local c
          for c in "${CRITICAL_PATHS[@]}"; do
            restore_critical_file "$tmp" "$c"
          done
          echo >&2
          echo "Review, then merge manually. Examples:" >&2
          echo "  diff /etc/fstab      /etc/fstab.restored-NEW" >&2
          echo "  diff /etc/passwd     /etc/passwd.restored-NEW" >&2
          echo "  sudo visudo -c -f /etc/sudoers.restored-NEW  # validate first" >&2
        fi
        ;;

      6)
        if [[ -z "$tmp" || ! -d "$tmp/etc/systemd/system" ]]; then
          log_warn "No systemd units in archive."; continue
        fi
        echo "Previously enabled units (from *.wants/ symlinks):" >&2
        ls "$tmp/etc/systemd/system"/*.wants/ 2>/dev/null \
          | sed 's|.*/||;s|\.service$||' | sort -u >&2
        echo >&2
        echo "Current:  systemctl list-unit-files --state=enabled" >&2
        ;;

      7)
        if [[ -f "$dir/lists_tar.xz" ]]; then
          local ltmp; ltmp=$(mktemp -d /tmp/lists.XXXXXX)
          tar -xJf "$dir/lists_tar.xz" -C "$ltmp"
          if [[ -f "$ltmp/lists/dconf.ini" ]] && ask_yn "Load dconf?" n; then
            runuser -u "$REAL_USER" -- dconf load / < "$ltmp/lists/dconf.ini" \
              && log_ok "dconf loaded."
          fi
          if [[ -f "$ltmp/lists/vscode-extensions.txt" ]] && \
             ask_yn "Install VS Code extensions?" n; then
            while IFS= read -r ext; do
              [[ -z "$ext" ]] && continue
              runuser -u "$REAL_USER" -- code --install-extension "$ext" >&2 \
                && log_ok "  $ext" || log_warn "  $ext failed"
            done < "$ltmp/lists/vscode-extensions.txt"
          fi
          rm -rf "$ltmp"
        fi
        ;;

      8)
        [[ -f "$dir/home.tar.xz" ]] || { log_warn "No home.tar.xz."; continue; }
        restore_home "$dir"
        ;;

      9)
        if [[ -f "$dir/lists_tar.xz" ]]; then
          local ltmp; ltmp=$(mktemp -d /tmp/lists.XXXXXX)
          tar -xJf "$dir/lists_tar.xz" -C "$ltmp"
          for f in lspci.txt lsusb.txt nvidia-smi.txt mok-enrolled.txt; do
            [[ -f "$ltmp/lists/$f" ]] && { echo "--- $f ---" >&2
              head -20 "$ltmp/lists/$f" >&2; echo >&2; }
          done
          echo "Current:  lspci -k / lsusb / nvidia-smi / mokutil --list-enrolled" >&2
          rm -rf "$ltmp"
        fi
        ;;

      R|r)
        if [[ -f "$TXN_JOURNAL" ]]; then
          echo "--- $TXN_JOURNAL ---" >&2
          column -t -s $'\t' "$TXN_JOURNAL" >&2 2>/dev/null || cat "$TXN_JOURNAL" >&2
        fi
        ;;

      U|u)
        ask_yn "Roll back EVERY modification made in this session?" n && txn_rollback
        ;;

      0)
        break
        ;;

      *) log_warn "Unknown choice '$choice'." ;;
    esac
  done

  # --- On exit from menu: commit or ask ------------------------------------
  if [[ $TXN_ACTIVE -eq 1 ]]; then
    if ask_yn "Commit all modifications? (otherwise offers rollback)" y; then
      txn_commit
    else
      ask_yn "Roll back all modifications?" y && txn_rollback
    fi
  fi

  [[ -n "$tmp" ]] && rm -rf "$tmp"
  log_step "Restore session finished"
}

# Home restore — deliberately outside the /etc transaction because it's huge,
# and because "modify vs create" on user data deserves its own UX.
restore_home() {
  local dir="$1" archive="$dir/home.tar.xz"
  local home_dir="$REAL_HOME"
  local tmp; tmp=$(mktemp -d /tmp/homerestore.XXXXXX)

  log_info "Extracting home archive to temp..."
  tar -xJf "$archive" -C "$tmp" || { log_err "Extraction failed."; rm -rf "$tmp"; return 1; }
  local top
  top=$(find "$tmp" -mindepth 1 -maxdepth 1 -type d | head -1)
  [[ -z "$top" ]] && { log_err "Home archive has unexpected structure."; rm -rf "$tmp"; return 1; }

  log_info "Home archive user: $(basename "$top") -> target: $home_dir"

  if [[ ! -e "$home_dir" || -z "$(ls -A "$home_dir" 2>/dev/null)" ]]; then
    mkdir -p "$home_dir"
    cp -a "$top"/. "$home_dir"/ \
      && { chown -R "$REAL_UID:$REAL_GID" "$home_dir"
           log_ok "Home restored to $home_dir"; }
    rm -rf "$tmp"; return 0
  fi

  cat >&2 <<EOF

Target $home_dir is NOT empty. Choose:

  [1] Extract to alternate dir ($home_dir.restored-$TIMESTAMP)   — SAFEST
  [2] Merge: skip files that already exist
  [3] Merge: back up existing home first, then overwrite
  [4] REPLACE ENTIRE HOME (requires typing YES)                   — DANGEROUS
  [5] Skip

EOF
  local choice
  choice="$(ask_str 'Choose [1-5]' 1)" || { rm -rf "$tmp"; return 1; }

  case "$choice" in
    1)
      local alt="$home_dir.restored-$TIMESTAMP"
      cp -a "$top" "$alt" && chown -R "$REAL_UID:$REAL_GID" "$alt"
      log_ok "Extracted to $alt — diff/move at your leisure."
      ;;
    2)
      log_info "Merging (skip existing)..."
      cp -an "$top"/. "$home_dir"/ && chown -R "$REAL_UID:$REAL_GID" "$home_dir"
      log_ok "Merge complete (existing untouched)."
      ;;
    3)
      local bak="$home_dir.bak-$TIMESTAMP.tar.xz"
      log_info "Backing up current home to $bak ..."
      tar -cJf "$bak" -C "$(dirname "$home_dir")" "$(basename "$home_dir")" \
        || { log_err "Backup failed — aborting (no changes made)."; rm -rf "$tmp"; return 1; }
      cp -af "$top"/. "$home_dir"/ && chown -R "$REAL_UID:$REAL_GID" "$home_dir"
      log_ok "Merge complete (backup at $bak)."
      ;;
    4)
      ask_confirm_yes "This REPLACES $home_dir entirely and CANNOT be undone." \
        || { log_info "Aborted."; rm -rf "$tmp"; return 1; }
      local bak="$home_dir.bak-$TIMESTAMP.tar.xz"
      tar -cJf "$bak" -C "$(dirname "$home_dir")" "$(basename "$home_dir")" \
        || { log_err "Backup failed — aborting (no changes made)."; rm -rf "$tmp"; return 1; }
      rm -rf "$home_dir"/* "$home_dir"/.[!.]* "$home_dir"/..?* 2>/dev/null || true
      cp -a "$top"/* "$home_dir"/ 2>/dev/null || true
      cp -a "$top"/.[!.]* "$home_dir"/ 2>/dev/null || true
      chown -R "$REAL_UID:$REAL_GID" "$home_dir"
      log_ok "Home replaced (backup at $bak)."
      ;;
    5) log_info "Skipped." ;;
    *) log_warn "Unknown choice." ;;
  esac
  rm -rf "$tmp"
}

# ============================================================================
# Signal & exit handling — preserve the recoverable stage
# ============================================================================

on_interrupt() {
  log_warn "Interrupted."
  if [[ $TXN_ACTIVE -eq 1 ]]; then
    log_warn "Transaction is ACTIVE. Rollback data at: $TXN_DIR"
    if ask_yn "Roll back all modifications made in this session?" y; then
      txn_rollback
    fi
  fi
  exit 130
}
trap on_interrupt INT TERM

on_exit() {
  local rc=$?
  if [[ $TXN_ACTIVE -eq 1 ]]; then
    log_warn "Script exiting with transaction still active (rc=$rc)."
    log_warn "  Journal:   $TXN_JOURNAL"
    log_warn "  Backups:   $TXN_DIR/files/"
    log_warn "  To revert: re-run and choose [U], or manually:"
    log_warn "             sudo cp -a $TXN_DIR/files/<path> /<path>"
  fi
}
trap on_exit EXIT

# ============================================================================
# Main
# ============================================================================

main() {
  banner_logging_tip

  local mode="${1:-}"
  if [[ "$mode" != "backup" && "$mode" != "restore" ]]; then
    cat >&2 <<EOF

${C_BLD}system-backup.sh v$VERSION${C_RST} — interactive backup / restore

  1) BACKUP
  2) RESTORE
  q) Quit

EOF
    mode="$(ask_str 'Choose')" || exit 1
    case "$mode" in
      1|backup)  mode=backup ;;
      2|restore) mode=restore ;;
      q|quit)    exit 0 ;;
      *) log_err "Invalid choice."; exit 1 ;;
    esac
  fi

  if [[ "$mode" == "backup" ]]; then do_backup; else do_restore; fi
}

main "$@"
