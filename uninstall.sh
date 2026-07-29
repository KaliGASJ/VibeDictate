#!/usr/bin/env bash
# ==============================================================================
# VibeDictate uninstaller
# ==============================================================================
# Removes everything install.sh created. Downloaded models are large, so they
# are only deleted after an explicit confirmation.
#
# Usage: ./uninstall.sh [--yes] [--prefix DIR] [--keep-models]

set -euo pipefail

if [ -t 1 ]; then
    GREEN=$'\033[0;32m'; CYAN=$'\033[0;36m'; YELLOW=$'\033[1;33m'; BOLD=$'\033[1m'; NC=$'\033[0m'
else
    GREEN=""; CYAN=""; YELLOW=""; BOLD=""; NC=""
fi

PREFIX="${VD_PREFIX:-$HOME/.local/share/VibeDictate}"
BIN_DIR="${VD_BIN_DIR:-$HOME/.local/bin}"
UNIT_DIR="${VD_UNIT_DIR:-$HOME/.config/systemd/user}"
DESKTOP_DIR="${VD_DESKTOP_DIR:-$HOME/.local/share/applications}"
ASSUME_YES=0
KEEP_MODELS=0

while [ $# -gt 0 ]; do
    case "$1" in
        -y|--yes) ASSUME_YES=1 ;;
        --prefix) PREFIX="${2:?--prefix needs a directory}"; shift ;;
        --keep-models) KEEP_MODELS=1 ;;
        -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $1 (try --help)" >&2; exit 2 ;;
    esac
    shift
done

step() { printf '%s\n' "${YELLOW}==>${NC} ${BOLD}$*${NC}"; }
info() { printf '    %s\n' "$*"; }

confirm() {
    [ "$ASSUME_YES" -eq 1 ] && return 0
    [ -t 0 ] || return 1
    local answer
    read -r -p "    $1 [y/N] " answer
    [[ "$answer" =~ ^[Yy]$ ]]
}

step "Stopping the service"
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    systemctl --user stop vibedictate.service 2>/dev/null || true
    systemctl --user disable vibedictate.service 2>/dev/null || true
    info "Stopped and disabled"
else
    info "No systemd user session; nothing to stop"
fi

step "Removing launchers and unit files"
rm -f "$BIN_DIR/vibedictate-toggle" "$BIN_DIR/vibedictate-gui"
rm -f "$UNIT_DIR/vibedictate.service"
rm -f "$DESKTOP_DIR/vibedictate.desktop"
if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload 2>/dev/null || true
fi
info "Done"

step "Removing the installation directory"
if [ ! -d "$PREFIX" ]; then
    info "$PREFIX does not exist"
else
    MODEL_DIR="$PREFIX/models"
    MODEL_SIZE=""
    [ -d "$MODEL_DIR" ] && MODEL_SIZE="$(du -sh "$MODEL_DIR" 2>/dev/null | cut -f1)"

    if [ "$KEEP_MODELS" -eq 1 ] && [ -d "$MODEL_DIR" ]; then
        info "Keeping downloaded models (${MODEL_SIZE:-unknown size}) in $MODEL_DIR"
        find "$PREFIX" -mindepth 1 -maxdepth 1 ! -name models -exec rm -rf {} +
    else
        if [ -n "$MODEL_SIZE" ]; then
            info "This also deletes ${MODEL_SIZE} of downloaded models."
        fi
        if confirm "Delete $PREFIX entirely?"; then
            rm -rf "$PREFIX"
            info "Removed $PREFIX"
        else
            info "Left $PREFIX in place"
        fi
    fi
fi

# Models land here only when HF_HOME was overridden; mention it, never delete it.
if [ -d "$HOME/.cache/huggingface" ]; then
    printf '%s\n' "${YELLOW}Note:${NC} $HOME/.cache/huggingface may hold models from other tools; left untouched."
fi

printf '%s\n' "${GREEN}VibeDictate has been uninstalled.${NC}"
printf '%s\n' "Remember to remove the ${CYAN}vibedictate-toggle${NC} hotkey from your desktop settings."
