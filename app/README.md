# op-keysync

A GNOME tray app that fetches API keys from 1Password and injects them as environment variables into your local shell sessions — with automatic purge on screen lock and idle timeout.

## Architecture

```
┌─────────────────────────────────────┐
│  op-keysync (tray app, Python)      │
│                                     │
│  ┌───────────┐  ┌────────────────┐  │
│  │ Fernet    │  │ D-Bus          │  │
│  │ encrypted │  │ lock/unlock    │  │
│  │ secrets   │  │ + idle monitor │  │
│  └───────────┘  └────────────────┘  │
│         │                │          │
│  ┌──────┴──────┐   /run/user/UID/  │
│  │ Unix socket │   op-keysync/     │
│  │ server      │   {state,version} │
│  └──────┬──────┘                   │
└─────────┼───────────────────────────┘
          │
    ┌─────┴──────────────────────┐
    │  Shell hook (LOCAL only)   │
    │  precmd / PROMPT_COMMAND   │
    │  $SSH_CONNECTION → denied  │
    └────────────────────────────┘
```

## How it works

- **Secrets live only in RAM** — encrypted with Fernet (AES-128-CBC), key generated fresh at startup, never written to disk
- **On startup** — auto-syncs from 1Password after 2 seconds
- **On screen lock** — secrets purged from memory, version bumped → all local shells unset vars on next prompt
- **On 60 min idle** — same purge, triggered by no keyboard/mouse input via Mutter idle monitor
- **On unlock / input resumes** — 1Password queried automatically (5s delay for 1P desktop to wake), fresh keys loaded → shells re-inject
- **Manual sync** — "Full Sync" button in tray menu fetches from 1Password
- **Clipboard** — copy value or KEY=VALUE from tray menu submenu, auto-cleared after 20 seconds via `wl-copy`
- **SSH sessions denied** — shell hook checks `$SSH_CONNECTION` and skips entirely

## Security model

| Concern | How it's handled |
|---|---|
| Keys on disk | Never written — `/run/user/$UID/` is tmpfs (RAM) |
| Keys in keyring | Not used — no `secret-tool`, no GNOME keyring |
| SSH access | Shell hook checks `$SSH_CONNECTION` and skips entirely |
| Screen lock | D-Bus `org.gnome.ScreenSaver.ActiveChanged` triggers immediate purge |
| Idle timeout | D-Bus `org.gnome.Mutter.IdleMonitor` purges after 60 min no input |
| Socket access | Socket is `chmod 600`, owner-only |
| Key values in menu | Never shown — only key names in tray, values only in submenus |
| Clipboard | Values via `wl-copy` stdin (not in process args), auto-cleared after 20s |

## Files

| File | Purpose |
|---|---|
| `app/op-keysync.py` | Main tray app (Python, GTK3) |
| `app/op-keysync-shell.sh` | Shell hook — source from `.zshrc` / `.bashrc` |
| `app/op-keysync.desktop` | Desktop launcher + autostart |
| `app/README.md` | This file |

## Installed locations

| What | Where |
|---|---|
| App | `~/apps/op-keysync/op-keysync.py` |
| Shell hook | `~/apps/op-keysync/op-keysync-shell.sh` |
| Desktop launcher | `~/.local/share/applications/op-keysync.desktop` |
| Autostart | `~/.config/autostart/op-keysync.desktop` |
| Shell hook source | `~/.zshrc` and `~/.bashrc` |
| Runtime state | `/run/user/$UID/op-keysync/{state,version,sock}` |
| Debug log | `~/.local/share/op-keysync/debug.log` |

## Setup

### 1. Install dependencies

```bash
pip install cryptography
sudo apt install python3-gi python3-dbus socat wl-clipboard
```

### 2. Deploy files

```bash
mkdir -p ~/apps/op-keysync
cp app/op-keysync.py app/op-keysync-shell.sh ~/apps/op-keysync/
cp app/op-keysync.desktop ~/.local/share/applications/
cp app/op-keysync.desktop ~/.config/autostart/
```

### 3. Add shell hook

```bash
# .zshrc and/or .bashrc
source ~/apps/op-keysync/op-keysync-shell.sh
```

### 4. 1Password vault setup

Create a vault called **Exports** in 1Password. For each API key, create an item with:

| Field | Value |
|---|---|
| `env` (label) | Environment variable name, e.g. `ANTHROPIC_API_KEY` |
| `credential` (id) | The secret value |

### 5. First run

Launch from Ubuntu app menu: search "1Password Key Sync". First time, `op` CLI will prompt for 1Password authentication — approve once.

## Tray menu

```
🔓 Unlocked — 13 keys in memory
  ⏱ Auto-purge after 60 min idle
─────────────────────────────────
  ANTHROPIC_API_KEY  ▶  Copy value / Copy KEY=VALUE
  OPENAI_API_KEY     ▶  Copy value / Copy KEY=VALUE
  ...
─────────────────────────────────
↻  Full Sync (fetch from 1Password)
─────────────────────────────────
Quit
```

## Tray icon colours

| Colour | Meaning |
|---|---|
| 🟢 Green | Unlocked, keys loaded |
| 🔴 Red | Screen locked, keys purged |
| 🟠 Orange | Sync error |
| 🔵 Blue | Currently syncing |
| 🟡 Amber (clipboard) | Secret copied, waiting for paste (20s) |
| ⚪ Grey | Running, no keys yet |

## Troubleshooting

```bash
# Check daemon is running
ls /run/user/$UID/op-keysync/

# Check state
cat /run/user/$UID/op-keysync/state
cat /run/user/$UID/op-keysync/version

# Test socket
printf 'GET\n' | socat -t2 - UNIX-CONNECT:/run/user/$UID/op-keysync/sock

# Debug log
tail -f ~/.local/share/op-keysync/debug.log

# Test 1Password auth
op item list --vault Exports --format json

# Test D-Bus lock signal
dbus-monitor --session "type='signal',interface='org.gnome.ScreenSaver'"
```

## Known limitations

- **Clipboard clear-on-paste**: Not possible on GNOME Wayland — Mutter doesn't support `wlr-data-control` protocol. Uses 20-second auto-clear instead (same approach as 1Password, KeePassXC, Bitwarden).
- **GTK3**: Required because `AyatanaAppIndicator` (system tray) has no GTK4 support.
- **Deprecation warning**: `libayatana-appindicator` shows a deprecation warning on startup — cosmetic only, harmless.
