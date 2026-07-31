#!/usr/bin/env python3
"""
VibeDictate control panel.

Small GTK window to start and stop the daemon, which is also how you free the
VRAM it holds. Runs on GTK4 and falls back to GTK3.

This uses the system Python on purpose: it needs the distribution's PyGObject,
which is not part of the daemon's virtualenv.
"""

from __future__ import annotations

import fcntl
import os
import socket
import subprocess
import sys
import tempfile
import threading

SERVICE = "vibedictate.service"

try:
    import gi
except ImportError:
    sys.exit(
        "VibeDictate: PyGObject is missing.\n"
        "  Arch/CachyOS  sudo pacman -S python-gobject gtk4\n"
        "  Debian/Ubuntu sudo apt install python3-gi gir1.2-gtk-4.0\n"
        "  Fedora        sudo dnf install python3-gobject gtk4"
    )

GTK_VERSION = 0
for candidate in ("4.0", "3.0"):
    try:
        gi.require_version("Gtk", candidate)
        GTK_VERSION = int(candidate[0])
        break
    except ValueError:
        continue

if not GTK_VERSION:
    sys.exit("VibeDictate: neither GTK4 nor GTK3 typelibs are available.")

from gi.repository import GLib, Gtk  # noqa: E402


def socket_path() -> str:
    override = os.environ.get("VD_SOCKET", "").strip()
    if override:
        return override
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if not runtime or not os.path.isdir(runtime):
        runtime = os.path.join(tempfile.gettempdir(), f"vibedictate-{os.getuid()}")
    return os.path.join(runtime, "vibedictate.sock")


def ask_daemon(command: str) -> str | None:
    """Send a command to the daemon. None means it is not reachable."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(1.5)
    try:
        client.connect(socket_path())
        client.sendall(command.encode())
        return client.recv(32).decode("utf-8", "ignore").strip()
    except OSError:
        return None
    finally:
        client.close()


def systemctl(*args: str) -> int:
    try:
        return subprocess.run(
            ["systemctl", "--user", *args], check=False, capture_output=True, timeout=30
        ).returncode
    except (OSError, subprocess.SubprocessError):
        return 1


class ControlPanel(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id="io.github.kaligasj.VibeDictate")
        self.busy = False

    # -- window ---------------------------------------------------------------

    def do_activate(self) -> None:
        # Relaunching the app re-runs do_activate on this same instance instead
        # of spawning a process. Without this guard every relaunch stacked one
        # more refresh timeout that is never cancelled (refresh returns True),
        # and each tick queries the daemon and may fork a systemctl call.
        if getattr(self, "window", None) is not None:
            self.window.present()
            return

        title = "VibeDictate"
        self.status = Gtk.Label(label="Checking…")
        self.status.set_use_markup(True)
        self.hint = Gtk.Label(label="")
        self.hint.set_use_markup(True)

        self.start_button = Gtk.Button(label="Start (load into memory)")
        self.start_button.connect("clicked", self.on_start)
        self.stop_button = Gtk.Button(label="Stop (free memory)")
        self.stop_button.connect("clicked", self.on_stop)

        if GTK_VERSION == 4:
            self.window = Gtk.ApplicationWindow(application=self, title=title)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            for setter in ("set_margin_top", "set_margin_bottom", "set_margin_start", "set_margin_end"):
                getattr(box, setter)(22)
            buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            buttons.set_homogeneous(True)
            buttons.append(self.start_button)
            buttons.append(self.stop_button)
            box.append(self.status)
            box.append(buttons)
            box.append(self.hint)
            self.window.set_child(box)
            self.window.set_default_size(400, 190)
            self.window.set_resizable(False)
            self.window.present()
        else:
            self.window = Gtk.Window(title=title)
            # Without this the application would exit as soon as do_activate
            # returns, because GTK3 does not adopt plain windows automatically.
            self.add_window(self.window)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            box.set_border_width(22)
            buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            buttons.set_homogeneous(True)
            buttons.add(self.start_button)
            buttons.add(self.stop_button)
            box.add(self.status)
            box.add(buttons)
            box.add(self.hint)
            self.window.add(box)
            self.window.set_default_size(400, 190)
            self.window.set_resizable(False)
            self.window.set_position(Gtk.WindowPosition.CENTER)
            self.window.show_all()

        self.window.connect(
            "close-request" if GTK_VERSION == 4 else "delete-event", self.on_close
        )
        self.timeout_id = GLib.timeout_add_seconds(2, self.refresh)
        self.refresh()

    def on_close(self, *_args) -> bool:
        # Drop the polling timeout and forget the window, so a later activation
        # builds a fresh one instead of calling present() on a dead widget.
        if getattr(self, "timeout_id", None) is not None:
            GLib.source_remove(self.timeout_id)
            self.timeout_id = None
        self.window = None
        return False

    # -- state ----------------------------------------------------------------

    def refresh(self) -> bool:
        if self.busy:
            return True

        daemon_state = ask_daemon("status")
        if daemon_state == "recording":
            self.set_state("#e5a50a", "Recording", "Listening to the microphone…")
        elif daemon_state == "idle":
            self.set_state("#2ec27e", "Ready", "Model loaded · press your hotkey to dictate")
        elif systemctl("is-active", "--quiet", SERVICE) == 0:
            self.set_state("#e5a50a", "Starting", "Loading the model into memory…")
        else:
            self.set_state("#e01b24", "Stopped", "Memory is free · nothing is running")
        return True

    def set_state(self, colour: str, state: str, hint: str) -> None:
        self.status.set_markup(
            f"<span size='large' weight='bold'>Status:</span> "
            f"<span foreground='{colour}' weight='bold' size='large'>{state}</span>"
        )
        self.hint.set_markup(f"<span size='small' alpha='65%'>{GLib.markup_escape_text(hint)}</span>")
        running = state != "Stopped"
        self.start_button.set_sensitive(not running)
        self.stop_button.set_sensitive(running)

    # -- actions --------------------------------------------------------------

    def on_start(self, _button: Gtk.Button) -> None:
        self.run_action("start", "#e5a50a", "Starting", "Loading the model into memory…")

    def on_stop(self, _button: Gtk.Button) -> None:
        self.run_action("stop", "#e5a50a", "Stopping", "Releasing memory…")

    def run_action(self, action: str, colour: str, state: str, hint: str) -> None:
        self.busy = True
        self.set_state(colour, state, hint)
        self.start_button.set_sensitive(False)
        self.stop_button.set_sensitive(False)

        def worker() -> None:
            code = systemctl(action, SERVICE)
            GLib.idle_add(self.finish_action, code, action)

        threading.Thread(target=worker, daemon=True).start()

    def finish_action(self, code: int, action: str) -> bool:
        self.busy = False
        if code != 0:
            self.set_state(
                "#e01b24",
                "Error",
                f"systemctl --user {action} {SERVICE} failed. Check: journalctl --user -u {SERVICE}",
            )
        else:
            self.refresh()
        return False


if __name__ == "__main__":
    # Safety net for when Gtk.Application fails to deduplicate over D-Bus: each
    # extra copy polls the daemon on its own timer, and a second window adds
    # load without showing the user anything new.
    _lock = open(
        os.path.join(
            os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(),
            f"vibedictate-gui-{os.getuid()}.lock",
        ),
        "w",
    )
    try:
        fcntl.flock(_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(0)

    sys.exit(ControlPanel().run(sys.argv))
