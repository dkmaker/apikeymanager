# op-keysync — API Key Manager

## Project overview

A GNOME system tray application that securely manages API keys from 1Password, injecting them as environment variables into local shell sessions with automatic purge on screen lock, idle timeout, and SSH denial.

## Repository structure

```
app/
  op-keysync.py          # Main tray app — Python, GTK3, AyatanaAppIndicator
  op-keysync-shell.sh    # Shell hook — source from .zshrc/.bashrc
  op-keysync.desktop     # Desktop launcher + autostart file
  README.md              # Full documentation
AGENTS.md                # This file — context for coding agents
```

## Tech stack

- **Python 3** with GTK3 (`gi.repository`), `AyatanaAppIndicator3`, `dbus-python`
- **Fernet** encryption (from `cryptography` package) for in-memory secret storage
- **Unix domain socket** at `/run/user/$UID/op-keysync/sock` serving `export KEY=val` lines
- **D-Bus** signals: `org.gnome.ScreenSaver.ActiveChanged` (lock/unlock), `org.gnome.Mutter.IdleMonitor` (idle detection)
- **wl-copy** / **wl-clipboard** for clipboard operations (Wayland)
- **1Password CLI** (`op`) for fetching secrets from the Exports vault
- **socat** used by the shell hook to query the Unix socket

## Installed locations (on desktop machine)

| What | Where |
|---|---|
| App | `~/apps/op-keysync/op-keysync.py` |
| Shell hook | `~/apps/op-keysync/op-keysync-shell.sh` |
| Desktop launcher | `~/.local/share/applications/op-keysync.desktop` |
| Autostart | `~/.config/autostart/op-keysync.desktop` |
| Shell integration | `~/.zshrc` and `~/.bashrc` — `source ~/apps/op-keysync/op-keysync-shell.sh` |
| Runtime state | `/run/user/$UID/op-keysync/{state,version,sock}` (tmpfs) |
| Debug log | `~/.local/share/op-keysync/debug.log` |

## Key design decisions

1. **No keyring/secret-tool** — secrets only in process memory, encrypted with Fernet
2. **No disk persistence** — runtime files are on tmpfs (`/run/user`), cleared on reboot
3. **Shell hook via precmd/PROMPT_COMMAND** — checks a version file on every prompt (~1ms), queries socket only on version change
4. **SSH sessions denied** — shell hook checks `$SSH_CONNECTION` first, skips if set
5. **Clipboard via wl-copy stdin** — secret never appears in process args (`/proc/*/cmdline`)
6. **20-second clipboard auto-clear** — GNOME Wayland has no way to detect paste events (Mutter refuses to implement `wlr-data-control`), so timer-based clear like 1Password/KeePassXC
7. **GTK3 required** — `AyatanaAppIndicator` (system tray) has no GTK4 support
8. **Sync triggers**: startup (2s delay), unlock (5s delay for 1P desktop to wake), manual "Full Sync" only — no timer-based periodic sync
9. **Idle purge** — Mutter IdleMonitor D-Bus signal after 60 min no input, keys re-fetched when input resumes

## Deploying changes

After editing files in `app/`:

```bash
# Copy to installed location
cp app/op-keysync.py ~/apps/op-keysync/op-keysync.py
cp app/op-keysync-shell.sh ~/apps/op-keysync/op-keysync-shell.sh

# User restarts the app from Ubuntu application launcher
# (don't try to start it from SSH/terminal — needs full GNOME session environment)
```

## Debugging

```bash
# Tail the debug log (clipboard events, sync, lock/unlock)
tail -f ~/.local/share/op-keysync/debug.log

# Check runtime state
cat /run/user/$(id -u)/op-keysync/state    # "locked" or "unlocked"
cat /run/user/$(id -u)/op-keysync/version  # integer, bumped on every state change

# Test the socket manually
printf 'GET\n' | socat -t2 - UNIX-CONNECT:/run/user/$(id -u)/op-keysync/sock

# Test 1Password CLI
op item list --vault Exports --format json
```

## Known limitations

- Clipboard cannot clear on paste (GNOME Wayland limitation) — uses 20s timer instead
- GTK3 clipboard API (`Gtk.Clipboard.set_text` + `store()`) doesn't persist on Wayland tray apps — must use `wl-copy` subprocess
- `wl-copy --foreground` hangs forever on GNOME/Mutter — cannot monitor process exit for paste detection
- App must be started from the desktop GUI (needs `WAYLAND_DISPLAY`, `DBUS_SESSION_BUS_ADDRESS`, etc.)
- `AyatanaAppIndicator3` shows a harmless deprecation warning (the glib replacement requires Gio.Menu rewrite)

## Otto integration

The old otto secrets loader in `~/workspaces/otto/.pi/otto.zsh` has been replaced with a comment pointing to op-keysync. The `_otto_secrets_first_prompt` precmd hook no longer runs.
