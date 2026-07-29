# Security Policy

## Reporting a vulnerability

Open a [private security advisory](https://github.com/KaliGASJ/VibeDictate/security/advisories/new)
rather than a public issue. Please allow a reasonable window for a fix before
disclosing.

## Threat model

VibeDictate runs entirely as an unprivileged user process. It never installs a
setuid binary, never opens a network socket, and never requires root at runtime.
The assets worth protecting are the microphone stream and the transcribed text.

### What is protected

* **Control socket.** A UNIX domain socket in `$XDG_RUNTIME_DIR`, created with
  mode `0600` under a `0177` umask so it is never momentarily reachable by other
  local users. It accepts a fixed set of one-word commands, reads at most 64
  bytes and times out after two seconds.
* **Runtime directory fallback.** When `XDG_RUNTIME_DIR` is unset, a per-uid
  directory under the system temporary directory is used. It is verified to be a
  real directory, owned by the current user, with no group or other access, and
  the daemon exits rather than proceeding if any of that fails.
* **Recorded audio.** Written to `$XDG_RUNTIME_DIR`, which is `tmpfs` on systemd
  systems, and unlinked as soon as it has been transcribed. It does not reach
  persistent storage.
* **Transcripts.** Not logged. The systemd journal is persistent and readable by
  administrators, so only character counts and timings are emitted unless
  `VD_LOG_TRANSCRIPT=1` is set explicitly.
* **Clipboard.** Restored to its previous contents after pasting, so clipboard
  managers do not persist dictated text to disk.
* **Subprocesses.** Every external tool is invoked with an argument list, never
  through a shell, so transcribed text cannot be interpreted as a command.

### Known limitations

* **Any process in your session can read the clipboard** while the dictated text
  is on it. This is a Wayland and X11 property, not something the daemon can
  prevent. Use `VD_PASTE_METHOD=type` to bypass the clipboard entirely.
* **`ydotool` requires a daemon with access to `/dev/uinput`**, which can inject
  input into any application. This is only needed on GNOME; the setup is
  documented in the README and is entirely optional.
* **Models are downloaded from Hugging Face** over HTTPS on first run and are
  not signature-verified beyond that. Pin `VD_MODEL` to a repository you trust.
* **Anyone who can execute code as your user can trigger recording**, because
  the socket is by definition reachable by you. There is no protection against
  an attacker who is already you.
