# Contributing to VibeDictate

Thanks for taking the time. This is a small project, so the process is light.

## Reporting a bug

Include the output of:

```bash
~/.local/share/VibeDictate/vibedictate-daemon --check
journalctl --user -u vibedictate.service -n 50
```

Plus your distribution, desktop environment and whether you are on Wayland or
X11. Most reports come down to a missing injection backend, and `--check`
answers that immediately.

**Never paste journal output that contains dictated text.** It only appears if
you enabled `VD_LOG_TRANSCRIPT=1`; turn it off before collecting logs.

## Development setup

```bash
git clone https://github.com/KaliGASJ/VibeDictate.git
cd VibeDictate
./install.sh --prefix /tmp/vibedictate-dev --skip-system-deps
```

`--prefix` keeps your working installation untouched. Run the daemon in the
foreground to watch it:

```bash
VD_SOCKET=/tmp/vd-dev.sock /tmp/vibedictate-dev/vibedictate-daemon
VD_SOCKET=/tmp/vd-dev.sock ./vibedictate-toggle status
```

Use a small model while iterating: `VD_MODEL=tiny VD_DEVICE=cpu`.

## Before opening a pull request

```bash
python3 -m compileall -q daemon.py vibedictate-gui.py
python3 -c "import ast; ast.parse(open('vibedictate-toggle').read())"
bash -n install.sh uninstall.sh vibedictate-daemon
shellcheck install.sh uninstall.sh vibedictate-daemon
```

CI runs the same checks plus a full installation on a clean Ubuntu runner.

## Things to keep in mind

* **Privacy is a feature, not a nice-to-have.** Transcribed text must never
  reach the journal, a log file or the network by default. If you add a code
  path that touches the transcript, gate it behind an explicit opt-in.
* **Degrade, never crash.** Every external tool is optional. If a backend is
  missing or fails, fall back and tell the user what to install — the daemon
  should stay up.
* **No hardcoded interpreter or library versions.** Paths are derived at
  runtime; a pinned `python3.12` breaks on every distribution that moved on.
* **User-facing strings are English.** The README is translated; the code is not.
* **Test on both Wayland and X11** when touching text injection, and mention in
  the PR which compositors you tried.
