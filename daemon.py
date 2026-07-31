#!/usr/bin/env python3
"""
VibeDictate daemon - local, private voice dictation for Linux.

Keeps a speech-recognition model resident in GPU VRAM or system RAM, records the
microphone on demand, transcribes on-device and injects the result into the
focused window.

Control protocol: plain ASCII commands over a 0600 UNIX socket.
    toggle | start | stop | status | ping | quit

This file is normally launched through the `vibedictate-daemon` wrapper, which
sources env.sh and selects the bundled virtualenv interpreter.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from shutil import which

APP = "VibeDictate"
VERSION = "1.0.0"
SAMPLE_RATE = 16000
PROTOCOL_LIMIT = 64

# ---------------------------------------------------------------------------
# Configuration (every knob is an environment variable, see env.sh.example)
# ---------------------------------------------------------------------------


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


MODEL_NAME = _env("VD_MODEL", "deepdml/faster-whisper-large-v3-turbo-ct2")
DEVICE = _env("VD_DEVICE", "auto").lower()
COMPUTE_TYPE = _env("VD_COMPUTE", "float16")
LANG = _env("VD_LANG", "")
PROMPT = _env("VD_PROMPT", "")
BEAM_SIZE = _env_int("VD_BEAM_SIZE", 5)

PASTE_METHOD = _env("VD_PASTE_METHOD", "auto").lower()
PASTE_KEY = _env("VD_PASTE_KEY", "ctrl+shift+v").lower()
TYPE_DELAY_MS = _env_int("VD_TYPE_DELAY_MS", 8)

CLIPBOARD_RESTORE = _env_bool("VD_CLIPBOARD_RESTORE", True)
CLIPBOARD_RESTORE_DELAY = _env_float("VD_CLIPBOARD_RESTORE_DELAY", 0.8)

LOG_TRANSCRIPT = _env_bool("VD_LOG_TRANSCRIPT", False)
NOTIFICATIONS = _env_bool("VD_NOTIFICATIONS", True)
HOTKEY_LABEL = _env("VD_HOTKEY_LABEL", "Ctrl+Space")

AUDIO_DEVICE = _env("VD_AUDIO_DEVICE", "")
MIN_DURATION = _env_float("VD_MIN_DURATION", 0.2)
TAIL_FLUSH = _env_float("VD_TAIL_FLUSH", 0.15)
TRAILING_SPACE = _env_bool("VD_TRAILING_SPACE", True)


def log(message: str) -> None:
    """Operational logging. Never carries transcribed content."""
    print(f"[{APP}] {message}", flush=True)


def log_error(message: str) -> None:
    print(f"[{APP}] error: {message}", file=sys.stderr, flush=True)


def run(cmd: list[str], timeout: float = 5.0, stdin_data: bytes | None = None,
        capture: bool = True) -> subprocess.CompletedProcess | None:
    """Run a helper binary. Returns None when it is missing, fails or times out.

    Set capture=False for tools that daemonise, such as wl-copy: it forks a
    child that owns the clipboard until someone else claims it, and that child
    inherits our pipes. Capturing output would block until the clipboard
    changes hands instead of returning immediately.
    """
    streams: dict = (
        {"capture_output": True}
        if capture
        else {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    )
    try:
        return subprocess.run(cmd, input=stdin_data, timeout=timeout, check=False, **streams)
    except (OSError, subprocess.SubprocessError) as exc:
        log_error(f"{cmd[0]}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Desktop notifications
# ---------------------------------------------------------------------------


def notify(title: str, body: str = "", urgency: str = "low", duration_ms: int = 0,
           replace_id: str | None = None, icon: str | None = None) -> str | None:
    """Show or update a notification bubble. Returns its id when available."""
    if not NOTIFICATIONS or not which("notify-send"):
        return None
    cmd = ["notify-send", "-p", "-a", APP, "-u", urgency]
    if duration_ms > 0:
        cmd += ["-t", str(duration_ms)]
    if replace_id:
        cmd += ["-r", str(replace_id)]
    if icon:
        cmd += ["-i", icon]
    cmd += [title, body]

    res = run(cmd, timeout=3)
    if res is None or res.returncode != 0:
        # Older notify-send builds lack -p/-a; retry with the portable subset.
        fallback = ["notify-send", "-u", urgency]
        if duration_ms > 0:
            fallback += ["-t", str(duration_ms)]
        run(fallback + [title, body], timeout=3)
        return replace_id

    printed = res.stdout.decode("utf-8", "replace").strip()
    return printed if printed.isdigit() else replace_id


def close_notify(notification_id: str | None) -> None:
    """Dismiss a notification bubble."""
    if not notification_id or not NOTIFICATIONS:
        return
    if which("gdbus"):
        run([
            "gdbus", "call", "--session",
            "--dest", "org.freedesktop.Notifications",
            "--object-path", "/org/freedesktop/Notifications",
            "--method", "org.freedesktop.Notifications.CloseNotification",
            str(notification_id),
        ], timeout=3)
    else:
        notify("", "", duration_ms=1, replace_id=notification_id)


# ---------------------------------------------------------------------------
# Text injection
# ---------------------------------------------------------------------------

# Linux input event codes, see /usr/include/linux/input-event-codes.h
_YDOTOOL_KEYS = {
    "ctrl+v": ["29:1", "47:1", "47:0", "29:0"],
    "ctrl+shift+v": ["29:1", "42:1", "47:1", "47:0", "42:0", "29:0"],
}


class TextInjector:
    """Injects text into the focused window across Wayland and X11.

    Preferred path is clipboard + a paste shortcut: it is atomic, instant and
    never mangles accented or uppercase characters. Direct synthetic typing is
    kept as a fallback for compositors without clipboard tooling.
    """

    def __init__(self) -> None:
        session = os.environ.get("XDG_SESSION_TYPE", "").lower()
        self.wayland = bool(os.environ.get("WAYLAND_DISPLAY")) or session == "wayland"
        self.x11 = not self.wayland and bool(os.environ.get("DISPLAY"))
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
        # Mutter still does not implement zwp_virtual_keyboard_v1, so wtype
        # cannot deliver keystrokes on GNOME. Probe ydotool first there.
        self.prefer_ydotool = "GNOME" in desktop
        self._wtype_broken = False
        self.paste_key = PASTE_KEY if PASTE_KEY in _YDOTOOL_KEYS else "ctrl+shift+v"

    # -- capability probing --------------------------------------------------

    def clipboard_writer(self) -> list[str] | None:
        if self.wayland and which("wl-copy"):
            return ["wl-copy"]
        if which("xclip"):
            return ["xclip", "-selection", "clipboard"]
        if which("xsel"):
            return ["xsel", "--clipboard", "--input"]
        if which("wl-copy"):
            return ["wl-copy"]
        return None

    def clipboard_reader(self) -> list[str] | None:
        if self.wayland and which("wl-paste"):
            return ["wl-paste", "--no-newline"]
        if which("xclip"):
            return ["xclip", "-selection", "clipboard", "-o"]
        if which("xsel"):
            return ["xsel", "--clipboard", "--output"]
        if which("wl-paste"):
            return ["wl-paste", "--no-newline"]
        return None

    def key_senders(self) -> list[str]:
        """Ordered list of tools able to send a paste shortcut."""
        senders: list[str] = []
        if self.wayland:
            if self.prefer_ydotool:
                senders = ["ydotool", "wtype"]
            else:
                senders = ["wtype", "ydotool"]
        else:
            senders = ["xdotool", "ydotool"]
        return [s for s in senders if which(s)]

    def typers(self) -> list[str]:
        candidates = ["wtype", "xdotool"] if self.wayland else ["xdotool", "wtype"]
        return [t for t in candidates if which(t)]

    def describe(self) -> str:
        return (
            f"session={'wayland' if self.wayland else 'x11' if self.x11 else 'headless'} "
            f"clipboard={(self.clipboard_writer() or ['none'])[0]} "
            f"keys={','.join(self.key_senders()) or 'none'} "
            f"typing={','.join(self.typers()) or 'none'}"
        )

    def missing_dependencies(self) -> list[str]:
        problems = []
        if not self.clipboard_writer():
            problems.append("wl-clipboard (Wayland) or xclip (X11)")
        if not self.key_senders() and not self.typers():
            problems.append("wtype/ydotool (Wayland) or xdotool (X11)")
        return problems

    # -- clipboard -----------------------------------------------------------

    def _clipboard_read(self) -> bytes | None:
        reader = self.clipboard_reader()
        if not reader:
            return None
        res = run(reader, timeout=3)
        if res is None or res.returncode != 0:
            return None
        return res.stdout

    def _clipboard_write(self, payload: bytes) -> bool:
        writer = self.clipboard_writer()
        if not writer:
            return False
        res = run(writer, timeout=5, stdin_data=payload, capture=False)
        return res is not None and res.returncode == 0

    def _clipboard_clear(self) -> None:
        if self.wayland and which("wl-copy"):
            run(["wl-copy", "--clear"], timeout=3, capture=False)
        else:
            self._clipboard_write(b"")

    def _restore_clipboard_later(self, previous: bytes | None, ours: bytes) -> None:
        """Put the user's clipboard back so dictated text is not left behind.

        Runs off-thread after a grace period: the target application must have
        consumed the paste first. Aborts if the clipboard changed meanwhile,
        which means the user copied something else and owns it now.
        """

        def worker() -> None:
            time.sleep(CLIPBOARD_RESTORE_DELAY)
            current = self._clipboard_read()
            if current is not None and current.strip() != ours.strip():
                return
            if previous:
                self._clipboard_write(previous)
            else:
                self._clipboard_clear()

        threading.Thread(target=worker, daemon=True).start()

    # -- keystrokes ----------------------------------------------------------

    def _send_paste_key(self) -> bool:
        for sender in self.key_senders():
            if sender == "wtype":
                if self._wtype_broken:
                    continue
                mods = ["-M", "ctrl"]
                if self.paste_key == "ctrl+shift+v":
                    mods += ["-M", "shift"]
                release = ["-m", "shift", "-m", "ctrl"] if self.paste_key == "ctrl+shift+v" else ["-m", "ctrl"]
                res = run(["wtype", "-s", "40"] + mods + ["-k", "v"] + release, timeout=5)
                if res is not None and res.returncode == 0:
                    return True
                # Compositor rejected the virtual keyboard protocol (GNOME):
                # stop retrying wtype for the rest of this session.
                self._wtype_broken = True
                log("wtype cannot send keystrokes on this compositor, falling back")
                continue

            if sender == "ydotool":
                res = run(["ydotool", "key"] + _YDOTOOL_KEYS[self.paste_key], timeout=5)
                if res is not None and res.returncode == 0:
                    return True
                log("ydotool failed; is ydotoold running and is YDOTOOL_SOCKET set?")
                continue

            if sender == "xdotool":
                res = run(["xdotool", "key", "--clearmodifiers", self.paste_key], timeout=5)
                if res is not None and res.returncode == 0:
                    return True
        return False

    def _type_directly(self, text: str) -> bool:
        for typer in self.typers():
            if typer == "wtype":
                if self._wtype_broken:
                    continue
                res = run(["wtype", "-d", "3", "-s", str(TYPE_DELAY_MS), "--", text], timeout=120)
                if res is not None and res.returncode == 0:
                    return True
                self._wtype_broken = True
                continue
            if typer == "xdotool":
                res = run(
                    ["xdotool", "type", "--clearmodifiers", "--delay", str(TYPE_DELAY_MS), "--", text],
                    timeout=120,
                )
                if res is not None and res.returncode == 0:
                    return True
        return False

    # -- public API ----------------------------------------------------------

    def inject(self, text: str) -> None:
        if not text:
            return

        payload = text.encode("utf-8")
        want_clipboard = PASTE_METHOD in ("auto", "clipboard")

        if want_clipboard and self.clipboard_writer():
            previous = self._clipboard_read() if CLIPBOARD_RESTORE else None
            if self._clipboard_write(payload):
                if self._send_paste_key():
                    if CLIPBOARD_RESTORE:
                        self._restore_clipboard_later(previous, payload)
                    return
                # Clipboard holds the text but no tool could press paste.
                notify(
                    f"{APP}: text copied",
                    f"Could not send {self.paste_key}. Press it yourself to paste.",
                    urgency="normal",
                    duration_ms=4000,
                )
                return

        if PASTE_METHOD in ("auto", "type") and self._type_directly(text):
            return

        if self.clipboard_writer() and self._clipboard_write(payload):
            notify(
                f"{APP}: text copied",
                "No injection backend available. Paste manually.",
                urgency="normal",
                duration_ms=4000,
            )
            return

        notify(
            f"{APP}: cannot inject text",
            "Install wl-clipboard + wtype/ydotool (Wayland) or xclip + xdotool (X11).",
            urgency="critical",
            duration_ms=6000,
        )
        log_error("no text injection backend available")


# ---------------------------------------------------------------------------
# Audio capture
# ---------------------------------------------------------------------------


def audio_backend() -> list[str] | None:
    """First available raw 16 kHz mono s16le capture command."""
    if which("parec"):
        cmd = ["parec", f"--rate={SAMPLE_RATE}", "--channels=1", "--format=s16le"]
        if AUDIO_DEVICE:
            cmd.append(f"--device={AUDIO_DEVICE}")
        return cmd
    if which("ffmpeg"):
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "pulse", "-i", AUDIO_DEVICE or "default",
            "-ar", str(SAMPLE_RATE), "-ac", "1", "-f", "s16le", "-",
        ]
    if which("arecord"):
        cmd = ["arecord", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", "1"]
        if AUDIO_DEVICE:
            cmd += ["-D", AUDIO_DEVICE]
        return cmd
    if which("sox"):
        return [
            "sox", "-q", "-d", "-t", "raw", "-r", str(SAMPLE_RATE),
            "-c", "1", "-b", "16", "-e", "signed-integer", "-",
        ]
    return None


class Recorder:
    """On-demand microphone capture feeding the transcription engine."""

    def __init__(self, engine, injector: TextInjector, scratch_dir: str) -> None:
        self.engine = engine
        self.injector = injector
        self.scratch_dir = scratch_dir
        self.lock = threading.Lock()
        self.is_recording = False
        self._proc: subprocess.Popen | None = None
        self._handle = None
        self._path: str | None = None
        self._notification: str | None = None

    def _discard(self, path: str | None) -> None:
        if not path:
            return
        try:
            os.unlink(path)
        except OSError:
            pass

    def start(self) -> None:
        cmd = audio_backend()
        if cmd is None:
            notify(
                f"{APP}: no recorder found",
                "Install pipewire-pulse / pulseaudio-utils (parec), ffmpeg or alsa-utils.",
                urgency="critical",
                duration_ms=6000,
            )
            log_error("no audio capture backend available")
            return

        with self.lock:
            if self.is_recording:
                return
            handle = None
            path = None
            try:
                # Scratch audio lives in tmpfs (XDG_RUNTIME_DIR): raw voice never
                # reaches persistent storage.
                fd, path = tempfile.mkstemp(prefix="capture-", suffix=".raw", dir=self.scratch_dir)
                handle = os.fdopen(fd, "wb")
                proc = subprocess.Popen(cmd, stdout=handle, stderr=subprocess.DEVNULL)
            except OSError as exc:
                if handle is not None:
                    handle.close()
                self._discard(path)
                log_error(f"could not start {cmd[0]}: {exc}")
                notify(f"{APP}: recording failed", str(exc), urgency="critical", duration_ms=5000)
                return

            self._proc = proc
            self._handle = handle
            self._path = path
            self.is_recording = True

        self._notification = notify(
            "● REC  ░▒▓ RECORDING",
            f"› Capturing voice · {HOTKEY_LABEL} to stop",
            urgency="critical",
            duration_ms=0,
            icon="media-record",
        )

    def stop_and_transcribe(self) -> None:
        with self.lock:
            if not self.is_recording:
                return
            self.is_recording = False
            proc, handle, path = self._proc, self._handle, self._path
            notification = self._notification
            self._proc = self._handle = self._path = self._notification = None

        try:
            self._finish(proc, handle, path, notification)
        except Exception as exc:  # keep the daemon alive on any unexpected error
            log_error(f"transcription pipeline: {exc}")
            close_notify(notification)
        finally:
            self._discard(path)

    def _finish(self, proc, handle, path, notification) -> None:
        if proc is not None:
            # Let the PipeWire/PulseAudio ring buffer drain so the final
            # syllable is not clipped.
            time.sleep(TAIL_FLUSH)
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)

        if handle is not None:
            handle.close()

        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError as exc:
            log_error(f"could not read capture buffer: {exc}")
            close_notify(notification)
            return

        import numpy as np

        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if samples.size < SAMPLE_RATE * MIN_DURATION:
            log("capture too short, ignored")
            close_notify(notification)
            return

        notify(
            "⟳  TRANSCRIBING  ▓▒░",
            "› Processing on-device…",
            urgency="normal",
            duration_ms=0,
            replace_id=notification,
            icon="content-loading-symbolic",
        )

        options = {
            "beam_size": BEAM_SIZE,
            "vad_filter": True,
            "vad_parameters": {"min_silence_duration_ms": 500, "speech_pad_ms": 600},
        }
        if LANG:
            options["language"] = LANG
        if PROMPT:
            options["initial_prompt"] = PROMPT

        started = time.monotonic()
        try:
            segments, info = self.engine.transcribe(samples, **options)
            text = "".join(segment.text for segment in segments).strip()
        except Exception as exc:
            log_error(f"inference failed: {exc}")
            notify(f"{APP}: transcription failed", str(exc)[:180], urgency="critical", duration_ms=5000)
            close_notify(notification)
            return

        elapsed = time.monotonic() - started
        close_notify(notification)

        if not text:
            log(f"no speech recognised ({elapsed:.2f}s)")
            return

        detected = getattr(info, "language", "?")
        if LOG_TRANSCRIPT:
            log(f"({detected}, {elapsed:.2f}s) {text}")
        else:
            log(f"transcribed {len(text)} chars ({detected}, {elapsed:.2f}s)")

        self.injector.inject(text + " " if TRAILING_SPACE else text)


# ---------------------------------------------------------------------------
# Socket plumbing
# ---------------------------------------------------------------------------


def runtime_dir() -> str:
    """A private, user-owned directory for the socket and scratch audio.

    Prefers XDG_RUNTIME_DIR (tmpfs, 0700, cleaned on logout). Falls back to a
    per-uid directory that is validated to be a real directory we own with no
    access for anybody else, so a hostile local user cannot pre-create or
    symlink it.
    """
    xdg = os.environ.get("XDG_RUNTIME_DIR", "")
    if xdg and os.path.isdir(xdg) and os.access(xdg, os.W_OK):
        return xdg

    fallback = os.path.join(tempfile.gettempdir(), f"vibedictate-{os.getuid()}")
    try:
        os.mkdir(fallback, 0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        log_error(f"cannot create runtime directory {fallback}: {exc}")
        sys.exit(1)

    info = os.lstat(fallback)
    if not os.path.isdir(fallback) or os.path.islink(fallback):
        log_error(f"{fallback} is not a directory")
        sys.exit(1)
    if info.st_uid != os.getuid():
        log_error(f"{fallback} is not owned by the current user")
        sys.exit(1)
    if info.st_mode & 0o077:
        os.chmod(fallback, 0o700)

    log(f"XDG_RUNTIME_DIR unset, using {fallback}")
    return fallback


def socket_path(directory: str) -> str:
    override = _env("VD_SOCKET")
    return override if override else os.path.join(directory, "vibedictate.sock")


def ping_existing(path: str) -> bool:
    """True when another daemon already owns the socket."""
    if not os.path.exists(path):
        return False
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(2.0)
    try:
        client.connect(path)
        client.sendall(b"ping")
        return client.recv(16).strip() == b"pong"
    except OSError:
        return False
    finally:
        client.close()


def bind_socket(path: str) -> socket.socket:
    if ping_existing(path):
        log_error(f"another {APP} daemon is already running on {path}")
        sys.exit(1)

    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log_error(f"cannot remove stale socket {path}: {exc}")
        sys.exit(1)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # umask makes the 0600 mode atomic at creation time, closing the window
    # where the socket would be reachable by other local users.
    previous_umask = os.umask(0o177)
    try:
        server.bind(path)
    finally:
        os.umask(previous_umask)
    os.chmod(path, 0o600)
    server.listen(8)
    return server


# ---------------------------------------------------------------------------
# Engine bootstrap
# ---------------------------------------------------------------------------


def load_engine():
    """Load the speech model, degrading from GPU to CPU when needed."""
    try:
        from faster_whisper import WhisperModel as SpeechModel
    except ImportError:
        log_error(
            "the speech engine is not installed in this interpreter. "
            "Launch the daemon through 'vibedictate-daemon' or re-run install.sh."
        )
        sys.exit(1)

    attempts: list[tuple[str, str]] = []
    if DEVICE in ("auto", "cuda"):
        attempts.append(("cuda", COMPUTE_TYPE if COMPUTE_TYPE != "int8" else "float16"))
    if DEVICE in ("auto", "cpu", "cuda"):
        attempts.append(("cpu", "int8"))
    if DEVICE not in ("auto", "cuda", "cpu"):
        attempts = [(DEVICE, COMPUTE_TYPE)]

    import numpy as np

    last_error: Exception | None = None
    for device, compute in attempts:
        try:
            model = SpeechModel(MODEL_NAME, device=device, compute_type=compute)
            # Run one real inference before accepting the device. CUDA libraries
            # are resolved lazily, so a GPU that cannot actually run anything
            # only fails here - constructing the model always succeeds. The
            # generator must be consumed or no decoding happens at all.
            silence = np.zeros(SAMPLE_RATE, dtype=np.float32)
            segments, _ = model.transcribe(silence, language=LANG or "en")
            list(segments)
            return model, device, compute
        except Exception as exc:
            last_error = exc
            log(f"cannot run on {device}/{compute}: {exc}")

    log_error(f"model '{MODEL_NAME}' could not be loaded: {last_error}")
    notify(f"{APP}: model failed to load", str(last_error)[:180], urgency="critical", duration_ms=8000)
    sys.exit(1)


def self_check() -> int:
    """Print an environment report. Used by install.sh and bug reports."""
    injector = TextInjector()
    recorder = audio_backend()
    print(f"{APP} {VERSION}")
    print(f"  python           {sys.version.split()[0]} ({sys.executable})")
    try:
        import faster_whisper  # noqa: F401
        import ctranslate2

        print(f"  engine           installed (ctranslate2 {ctranslate2.__version__})")
    except ImportError as exc:
        print(f"  engine           MISSING ({exc.name})")
    print(f"  audio capture    {recorder[0] if recorder else 'MISSING'}")
    print(f"  desktop          {injector.describe()}")
    print(f"  notifications    {'notify-send' if which('notify-send') else 'MISSING (optional)'}")
    print(f"  runtime dir      {runtime_dir()}")
    print(f"  model            {MODEL_NAME}")
    print(f"  device requested {DEVICE} / {COMPUTE_TYPE}")

    library_path = os.environ.get("LD_LIBRARY_PATH", "")
    gpu_libs = [name for name in ("cublas", "cudnn") if f"nvidia/{name}/lib" in library_path]
    if gpu_libs:
        print(f"  gpu libraries    {', '.join(gpu_libs)} on LD_LIBRARY_PATH")
    else:
        print("  gpu libraries    not linked (CPU inference)")

    problems = injector.missing_dependencies()
    if recorder is None:
        problems.append("parec, ffmpeg, arecord or sox")
    if problems:
        print("\n  Missing: " + "; ".join(problems))
        return 1
    print("\n  All required components are present.")
    return 0


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main() -> None:
    if "--version" in sys.argv:
        print(f"{APP} {VERSION}")
        return
    if "--check" in sys.argv:
        sys.exit(self_check())

    directory = runtime_dir()
    path = socket_path(directory)

    injector = TextInjector()
    for missing in injector.missing_dependencies():
        log(f"warning: missing dependency: {missing}")

    notify(f"{APP} starting…", "Loading the speech model", duration_ms=4000)
    started = time.monotonic()
    # load_engine also warms the kernels up, so the first real dictation is not
    # slower than the rest.
    engine, device, compute = load_engine()
    elapsed = time.monotonic() - started

    log(f"ready: device={device} compute={compute} in {elapsed:.1f}s")
    log(f"desktop: {injector.describe()}")
    notify(f"{APP} ready", f"{device.upper()} · {compute} · {elapsed:.1f}s", duration_ms=2500)

    recorder = Recorder(engine, injector, directory)
    server = bind_socket(path)
    log(f"listening on {path}")

    stopping = threading.Event()

    def shutdown(signum, _frame):
        log(f"received signal {signum}, shutting down")
        stopping.set()
        try:
            server.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        server.close()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    def transcribe_async() -> None:
        threading.Thread(target=recorder.stop_and_transcribe, daemon=True).start()

    # A wedged listening socket makes accept() return without blocking, and the
    # bare loop would then spawn threads as fast as the CPU allows. Measured on
    # a laptop: 22k threads/s, 1.5 cores, an extra 35 W. Bail out instead.
    spin = 0
    last_accept = time.time()

    while not stopping.is_set():
        try:
            conn, _ = server.accept()
        except OSError:
            break

        now = time.time()
        spin = spin + 1 if now - last_accept < 1.0 else 0
        last_accept = now
        if spin > 2000:
            log_error("accept() is spinning; stopping so it cannot drain the battery")
            notify(f"{APP}: connection loop", "the daemon stopped to protect the battery",
                   urgency="critical", duration_ms=8000)
            break

        try:
            conn.settimeout(2.0)
            command = conn.recv(PROTOCOL_LIMIT).decode("utf-8", "ignore").strip().lower()

            if command == "ping":
                reply = b"pong"
            elif command == "status":
                reply = b"recording" if recorder.is_recording else b"idle"
            elif command == "toggle":
                if recorder.is_recording:
                    transcribe_async()
                else:
                    recorder.start()
                reply = b"ok"
            elif command == "start":
                if not recorder.is_recording:
                    recorder.start()
                reply = b"ok"
            elif command == "stop":
                if recorder.is_recording:
                    transcribe_async()
                reply = b"ok"
            elif command == "quit":
                reply = b"ok"
                stopping.set()
            else:
                reply = b"unknown"

            try:
                conn.sendall(reply)
            except OSError:
                pass
        except (OSError, socket.timeout) as exc:
            log_error(f"client connection: {exc}")
        finally:
            conn.close()

    if recorder.is_recording:
        recorder.stop_and_transcribe()

    server.close()
    try:
        os.unlink(path)
    except OSError:
        pass
    log("stopped")


if __name__ == "__main__":
    main()
