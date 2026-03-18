# Installing op-keysync

## Option A — APT repository (recommended, gets automatic updates)

### 1. Add the GPG signing key

```bash
curl -fsSL https://dkmaker.github.io/apikeymanager/gpg.key \
  | sudo gpg --dearmor -o /usr/share/keyrings/op-keysync.gpg
```

### 2. Add the repository

```bash
echo "deb [arch=all signed-by=/usr/share/keyrings/op-keysync.gpg] \
  https://dkmaker.github.io/apikeymanager stable main" \
  | sudo tee /etc/apt/sources.list.d/op-keysync.list
```

### 3. Install

```bash
sudo apt update
sudo apt install op-keysync
```

### 4. Done — what happens automatically

After install, the package will have:
- Added a `source` line to your `~/.zshrc` and `~/.bashrc`
- Created an autostart entry so op-keysync launches at login

Restart your shell (or `source ~/.zshrc` / `source ~/.bashrc`), then launch
**1Password Key Sync** from your GNOME application menu.

### Updating later

```bash
sudo apt update && sudo apt upgrade op-keysync
```

---

## Option B — One-off .deb download (no automatic updates)

Download the latest `.deb` from the [Releases page](https://github.com/dkmaker/apikeymanager/releases)
and install:

```bash
sudo apt install ./op-keysync_1.0.0-1_all.deb
```

---

## Requirements

- Ubuntu 22.04+ (or any Debian-based distro with GNOME and Wayland)
- Python 3.10+
- **1Password CLI** (`op`) — install from https://developer.1password.com/docs/cli/get-started/

  ```bash
  # Add 1Password apt repo then:
  sudo apt install 1password-cli
  ```

All other dependencies (`python3-gi`, `python3-dbus`, `python3-cryptography`,
`socat`, `wl-clipboard`, `libnotify-bin`) are installed automatically.

---

## 1Password vault setup

Create a vault named **Exports** in 1Password. For each API key, create an item with:

| Field label | Value |
|---|---|
| `env` | Environment variable name — e.g. `ANTHROPIC_API_KEY` |
| `credential` (field id) | The secret value |

On first launch, `op` will prompt for 1Password authentication. Approve it once.

---

## Uninstalling

```bash
sudo apt remove op-keysync
```

The uninstall script will remove the shell hook lines from your `.zshrc`/`.bashrc`
and delete the autostart entry.
