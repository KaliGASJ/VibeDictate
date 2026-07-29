#!/usr/bin/env bash
# ==============================================================================
# VibeDictate installer
# ==============================================================================
# Installs entirely inside the user's home directory. Nothing is written outside
# $PREFIX, ~/.local/bin, ~/.local/share/applications and ~/.config/systemd/user.
# System packages are never installed silently: the exact command is printed and
# only runs after you confirm it.
#
# Usage: ./install.sh [--yes] [--prefix DIR] [--skip-system-deps] [--skip-cuda]

set -euo pipefail

# Work from the repository directory regardless of where the script was called.
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -t 1 ]; then
    GREEN=$'\033[0;32m'; CYAN=$'\033[0;36m'; YELLOW=$'\033[1;33m'
    RED=$'\033[0;31m'; BOLD=$'\033[1m'; NC=$'\033[0m'
else
    GREEN=""; CYAN=""; YELLOW=""; RED=""; BOLD=""; NC=""
fi

PREFIX="${VD_PREFIX:-$HOME/.local/share/VibeDictate}"
BIN_DIR="${VD_BIN_DIR:-$HOME/.local/bin}"
UNIT_DIR="${VD_UNIT_DIR:-$HOME/.config/systemd/user}"
DESKTOP_DIR="${VD_DESKTOP_DIR:-$HOME/.local/share/applications}"
ASSUME_YES=0
SKIP_SYSTEM_DEPS=0
SKIP_CUDA=0

while [ $# -gt 0 ]; do
    case "$1" in
        -y|--yes) ASSUME_YES=1 ;;
        --prefix) PREFIX="${2:?--prefix needs a directory}"; shift ;;
        --skip-system-deps) SKIP_SYSTEM_DEPS=1 ;;
        --skip-cuda) SKIP_CUDA=1 ;;
        -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $1 (try --help)" >&2; exit 2 ;;
    esac
    shift
done

step() { printf '%s\n' "${YELLOW}==>${NC} ${BOLD}$*${NC}"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '%s\n' "${YELLOW}!! ${NC}$*"; }
fail() { printf '%s\n' "${RED}xx ${NC}$*" >&2; exit 1; }

confirm() {
    [ "$ASSUME_YES" -eq 1 ] && return 0
    [ -t 0 ] || return 1
    local answer
    read -r -p "    $1 [y/N] " answer
    [[ "$answer" =~ ^[Yy]$ ]]
}

printf '%s\n' "${CYAN}"
echo "======================================================================"
echo "            VibeDictate - local voice dictation for Linux"
echo "======================================================================"
printf '%s\n' "${NC}"
info "Install prefix: $PREFIX"

# ------------------------------------------------------------------------------
step "[1/6] Checking system dependencies"
# ------------------------------------------------------------------------------
IS_WAYLAND=0
if [ -n "${WAYLAND_DISPLAY:-}" ] || [ "${XDG_SESSION_TYPE:-}" = "wayland" ]; then
    IS_WAYLAND=1
fi
IS_GNOME=0
case "${XDG_CURRENT_DESKTOP:-}" in *[Gg][Nn][Oo][Mm][Ee]*) IS_GNOME=1 ;; esac

# Map required binaries to the package providing them, per distribution family.
declare -A PKG_OF
missing_bins=()

detect_manager() {
    for m in pacman apt-get dnf zypper apk; do
        command -v "$m" >/dev/null 2>&1 && { echo "$m"; return; }
    done
    echo "unknown"
}
MANAGER="$(detect_manager)"

case "$MANAGER" in
    pacman)
        info "Detected Arch Linux family (pacman)"
        PKG_OF=( [parec]=libpulse [notify-send]=libnotify [wl-copy]=wl-clipboard
                 [wtype]=wtype [xdotool]=xdotool [xclip]=xclip [ydotool]=ydotool
                 [python3]=python )
        GUI_PKGS="python-gobject gtk4"
        INSTALL_CMD="sudo pacman -S --needed"
        ;;
    apt-get)
        info "Detected Debian/Ubuntu family (apt)"
        PKG_OF=( [parec]=pulseaudio-utils [notify-send]=libnotify-bin [wl-copy]=wl-clipboard
                 [wtype]=wtype [xdotool]=xdotool [xclip]=xclip [ydotool]=ydotool
                 [python3]="python3 python3-venv python3-pip" )
        GUI_PKGS="python3-gi gir1.2-gtk-4.0"
        INSTALL_CMD="sudo apt-get install -y"
        ;;
    dnf)
        info "Detected Fedora/RHEL family (dnf)"
        PKG_OF=( [parec]=pulseaudio-utils [notify-send]=libnotify [wl-copy]=wl-clipboard
                 [wtype]=wtype [xdotool]=xdotool [xclip]=xclip [ydotool]=ydotool
                 [python3]="python3 python3-pip" )
        GUI_PKGS="python3-gobject gtk4"
        INSTALL_CMD="sudo dnf install -y"
        ;;
    zypper)
        info "Detected openSUSE (zypper)"
        PKG_OF=( [parec]=pulseaudio-utils [notify-send]=libnotify-tools [wl-copy]=wl-clipboard
                 [wtype]=wtype [xdotool]=xdotool [xclip]=xclip [ydotool]=ydotool
                 [python3]="python3 python3-pip" )
        GUI_PKGS="python3-gobject gtk4"
        INSTALL_CMD="sudo zypper install -y"
        ;;
    *)
        info "Unrecognised distribution: dependency installation is left to you."
        PKG_OF=()
        GUI_PKGS=""
        INSTALL_CMD=""
        ;;
esac

need() {
    command -v "$1" >/dev/null 2>&1 && return 0
    missing_bins+=("$1")
    return 1
}

need python3 || true
command -v parec >/dev/null 2>&1 || command -v ffmpeg >/dev/null 2>&1 || \
    command -v arecord >/dev/null 2>&1 || need parec || true
need notify-send || true

if [ "$IS_WAYLAND" -eq 1 ]; then
    need wl-copy || true
    # Mutter does not implement the virtual keyboard protocol, so wtype cannot
    # send keystrokes on GNOME; ydotool goes through the kernel instead.
    if [ "$IS_GNOME" -eq 1 ]; then
        need ydotool || true
    else
        need wtype || true
    fi
else
    need xclip || true
    need xdotool || true
fi

if [ ${#missing_bins[@]} -eq 0 ]; then
    info "${GREEN}All system dependencies are present.${NC}"
elif [ "$SKIP_SYSTEM_DEPS" -eq 1 ]; then
    warn "Missing: ${missing_bins[*]} (skipped by request)"
elif [ -z "$INSTALL_CMD" ]; then
    warn "Missing: ${missing_bins[*]}"
    warn "Install them with your package manager before starting the service."
else
    pkgs=""
    for bin in "${missing_bins[@]}"; do
        pkgs="$pkgs ${PKG_OF[$bin]:-$bin}"
    done
    [ -n "$GUI_PKGS" ] && pkgs="$pkgs $GUI_PKGS"
    # shellcheck disable=SC2086
    pkgs="$(printf '%s\n' $pkgs | sort -u | tr '\n' ' ')"
    info "Missing: ${missing_bins[*]}"
    info "Command: ${CYAN}$INSTALL_CMD $pkgs${NC}"
    if confirm "Run it now (asks for your sudo password)?"; then
        # shellcheck disable=SC2086
        $INSTALL_CMD $pkgs || warn "Package installation reported an error; continuing."
    else
        warn "Skipped. Run the command above before starting the service."
    fi
fi

# ------------------------------------------------------------------------------
step "[2/6] Creating directories"
# ------------------------------------------------------------------------------
mkdir -p "$PREFIX" "$BIN_DIR" "$UNIT_DIR" "$DESKTOP_DIR"
info "$PREFIX"

# ------------------------------------------------------------------------------
step "[3/6] Installing program files"
# ------------------------------------------------------------------------------
install -m 0755 daemon.py vibedictate-daemon vibedictate-toggle vibedictate-gui.py "$PREFIX/"
install -m 0644 requirements.txt requirements-cuda.txt "$PREFIX/"

FRESH_CONFIG=0
if [ -f "$PREFIX/env.sh" ]; then
    info "Keeping your existing env.sh"
else
    install -m 0644 env.sh.example "$PREFIX/env.sh"
    FRESH_CONFIG=1
    info "Created env.sh from the template"
fi

ln -sf "$PREFIX/vibedictate-toggle" "$BIN_DIR/vibedictate-toggle"
ln -sf "$PREFIX/vibedictate-gui.py" "$BIN_DIR/vibedictate-gui"
info "Linked vibedictate-toggle and vibedictate-gui into $BIN_DIR"

sed "s|@PREFIX@|$PREFIX|g" vibedictate.desktop > "$DESKTOP_DIR/vibedictate.desktop"
chmod 0644 "$DESKTOP_DIR/vibedictate.desktop"

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "$BIN_DIR is not in your PATH. Global hotkeys must use the full path:"
       warn "  $PREFIX/vibedictate-toggle" ;;
esac

# ------------------------------------------------------------------------------
step "[4/6] Setting up the Python environment"
# ------------------------------------------------------------------------------
command -v python3 >/dev/null 2>&1 || fail "python3 not found. Install Python 3.9 or newer."
PY_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
info "Using Python $PY_VERSION"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || fail "Python 3.9 or newer is required (found $PY_VERSION)."

if [ ! -x "$PREFIX/.venv/bin/python" ]; then
    python3 -m venv "$PREFIX/.venv" \
        || fail "Could not create the virtualenv. On Debian/Ubuntu install python3-venv."
fi
VENV_PY="$PREFIX/.venv/bin/python"

"$VENV_PY" -m pip install --upgrade --quiet pip wheel
info "Installing the speech engine (this downloads a few hundred MB)…"
"$VENV_PY" -m pip install --quiet -r requirements.txt \
    || fail "Dependency installation failed. Check your network connection and retry."

HAS_NVIDIA=0
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
    HAS_NVIDIA=1
elif [ -e /dev/nvidia0 ] || [ -e /dev/nvidiactl ]; then
    HAS_NVIDIA=1
fi

if [ "$SKIP_CUDA" -eq 1 ]; then
    info "Skipping GPU libraries by request; the daemon will run on the CPU."
elif [ "$HAS_NVIDIA" -eq 1 ]; then
    info "NVIDIA GPU detected: installing CUDA 12 / cuDNN 9 libraries…"
    if ! "$VENV_PY" -m pip install --quiet -r requirements-cuda.txt; then
        warn "CUDA libraries failed to install; the daemon will fall back to the CPU."
    fi
else
    info "No NVIDIA GPU detected: the daemon will run on the CPU."
    # Only ever rewrite a config file we just generated, never the user's own.
    if [ "$FRESH_CONFIG" -eq 1 ]; then
        # SC2016: the ${VAR:-default} text is written into env.sh literally and
        # must not be expanded here.
        # shellcheck disable=SC2016
        sed -i 's|^export VD_DEVICE=.*|export VD_DEVICE="${VD_DEVICE:-cpu}"|
                s|^export VD_COMPUTE=.*|export VD_COMPUTE="${VD_COMPUTE:-int8}"|
                s|^export VD_MODEL=.*|export VD_MODEL="${VD_MODEL:-small}"|' "$PREFIX/env.sh"
        info "env.sh preset to CPU inference with the 'small' model."
    fi
fi

# ------------------------------------------------------------------------------
step "[5/6] Installing the systemd user service"
# ------------------------------------------------------------------------------
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    sed "s|%h/.local/share/VibeDictate|$PREFIX|g" vibedictate.service > "$UNIT_DIR/vibedictate.service"
    systemctl --user daemon-reload
    systemctl --user enable vibedictate.service >/dev/null 2>&1 \
        || warn "Could not enable the service automatically."

    if ! systemctl --user list-units --type=target --all 2>/dev/null | grep -q graphical-session.target; then
        warn "graphical-session.target is not known to your session manager."
        warn "If the daemon does not start at login, run:"
        warn "  systemctl --user add-wants default.target vibedictate.service"
    fi
    SERVICE_READY=1
else
    warn "No systemd user session detected. Start the daemon manually with:"
    warn "  $PREFIX/vibedictate-daemon"
    SERVICE_READY=0
fi

# ------------------------------------------------------------------------------
step "[6/6] Verifying the installation"
# ------------------------------------------------------------------------------
if ! "$PREFIX/vibedictate-daemon" --check; then
    warn "Some components are missing (see above). Install them and re-run:"
    warn "  $PREFIX/vibedictate-daemon --check"
fi

printf '%s\n' "${GREEN}"
echo "======================================================================"
echo "                    Installation complete"
echo "======================================================================"
printf '%s\n' "${NC}"
echo "Next steps:"
if [ "$SERVICE_READY" -eq 1 ]; then
    echo "  1. Start it:      ${CYAN}systemctl --user start vibedictate.service${NC}"
else
    echo "  1. Start it:      ${CYAN}$PREFIX/vibedictate-daemon${NC}"
fi
echo "     The model downloads on first start; give it a minute."
echo "  2. Bind a hotkey: command ${CYAN}vibedictate-toggle${NC} to ${CYAN}Ctrl+Space${NC}"
echo "                    in your desktop's keyboard settings."
echo "  3. Settings:      ${CYAN}\$EDITOR $PREFIX/env.sh${NC}"
echo "  4. Control panel: ${CYAN}vibedictate-gui${NC}"
if [ "$IS_GNOME" -eq 1 ] && [ "$IS_WAYLAND" -eq 1 ]; then
    echo ""
    printf '%s\n' "${YELLOW}GNOME on Wayland:${NC} keystroke injection needs ydotool, which requires a"
    echo "background daemon with access to /dev/uinput:"
    echo "  ${CYAN}sudo systemctl enable --now ydotoold${NC}"
    echo "See the README section 'GNOME on Wayland' for the full setup."
fi
echo ""
echo "Uninstall at any time with: ${CYAN}./uninstall.sh${NC}"
