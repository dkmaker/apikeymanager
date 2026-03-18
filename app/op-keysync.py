#!/usr/bin/env python3
"""
op-keysync — 1Password in-memory key sync + tray indicator.

Architecture:
  - Secrets fetched from 1Password via `op` CLI, encrypted with Fernet in RAM only
  - Unix socket serves `export KEY=val` lines to LOCAL shell sessions only
  - D-Bus listener detects screen lock/unlock (org.gnome.ScreenSaver.ActiveChanged)
  - D-Bus idle watch (org.gnome.Mutter.IdleMonitor) purges keys after IDLE_TIMEOUT_MS
    of no input — same effect as a lock, without needing the screen to lock
  - On lock/idle: secrets purged from memory, socket returns empty, state→locked, version bumped
  - On unlock:    auto re-fetch from 1Password, state→unlocked, version bumped
  - Manual:       "Full Sync" menu item triggers re-fetch at any time
  - Version file bumped on every state change so shell hooks detect changes cheaply
  - SSH sessions are DENIED by the shell hook ($SSH_CONNECTION check), not here

Dependencies:
  pip install cryptography
  apt install python3-gi python3-dbus socat
"""

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('AyatanaAppIndicator3', '0.1')
from gi.repository import Gtk, GLib, AyatanaAppIndicator3 as AppIndicator

import dbus
import dbus.mainloop.glib
import subprocess
import threading
import socketserver
import os
import tempfile
import time
import json
import logging
from threading import RLock

from cryptography.fernet import Fernet

# ── Startup dependency check ───────────────────────────────────────────────────

def _check_dependencies() -> list[str]:
    """
    Check all runtime dependencies exist before starting the app.
    Returns a list of missing dependency descriptions (empty = all good).
    """
    import shutil
    missing = []
    deps = [
        ("op",          "1Password CLI — install from https://developer.1password.com/docs/cli/get-started/"),
        ("wl-copy",     "wl-clipboard — sudo apt install wl-clipboard"),
        ("socat",       "socat — sudo apt install socat"),
        ("notify-send", "libnotify-bin — sudo apt install libnotify-bin"),
    ]
    for cmd, description in deps:
        if shutil.which(cmd) is None:
            missing.append(f"  • {cmd}: {description}")
    return missing

_missing_deps = _check_dependencies()
if _missing_deps:
    import sys
    print("op-keysync: missing required dependencies:\n" + "\n".join(_missing_deps), file=sys.stderr)
    # Don't exit — show the error in the tray so the user can see it without a terminal

# ── Debug logging ──────────────────────────────────────────────────────────────
_LOG_DIR = os.path.expanduser("~/.local/share/op-keysync")
os.makedirs(_LOG_DIR, exist_ok=True)
logging.basicConfig(
    filename=os.path.join(_LOG_DIR, "debug.log"),
    level=logging.DEBUG,
    format="%(asctime)s.%(msecs)03d %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("op-keysync")

# ── Config ─────────────────────────────────────────────────────────────────────

IDLE_TIMEOUT_MINUTES = 60
IDLE_TIMEOUT_MS      = IDLE_TIMEOUT_MINUTES * 60 * 1000   # Mutter uses milliseconds

# ── Runtime directory (tmpfs — RAM backed, cleared on reboot) ──────────────────

_RUN_DIR     = os.path.join(f"/run/user/{os.getuid()}", "op-keysync")
STATE_FILE   = os.path.join(_RUN_DIR, "state")
VERSION_FILE = os.path.join(_RUN_DIR, "version")
SOCK_PATH    = os.path.join(_RUN_DIR, "sock")

os.makedirs(_RUN_DIR, exist_ok=True)

# ── SVG tray icons ─────────────────────────────────────────────────────────────

ICON_DIR = os.path.join(tempfile.gettempdir(), "op-keysync-icons")
os.makedirs(ICON_DIR, exist_ok=True)

def _lock_svg(fill: str, badge: str | None = None) -> str:
    badge_el = (f'<circle cx="16" cy="16" r="6" fill="{badge}" '
                f'stroke="#1e1e2e" stroke-width="1.5"/>') if badge else ""
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 22 22">
  <rect x="4" y="10" width="14" height="10" rx="2" fill="{fill}"/>
  <path d="M7 10V7a4 4 0 0 1 8 0v3" fill="none" stroke="{fill}"
        stroke-width="2.2" stroke-linecap="round"/>
  <rect x="9.5" y="13" width="3" height="4" rx="1.5" fill="#1e1e2e" opacity="0.5"/>
  {badge_el}
</svg>'''

def _write_icon(name: str, svg: str) -> str:
    path = os.path.join(ICON_DIR, f"{name}.svg")
    with open(path, "w") as f:
        f.write(svg)
    return path

def _paste_svg() -> str:
    """Clipboard/paste icon in amber — shown when a secret is in the clipboard."""
    return '''<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 22 22">
  <rect x="6" y="4" width="10" height="14" rx="1.5" fill="#e5a000" opacity="0.9"/>
  <rect x="8" y="2" width="6" height="3" rx="1" fill="#e5a000"/>
  <rect x="8" y="8" width="6" height="1.5" rx="0.75" fill="#1e1e2e" opacity="0.5"/>
  <rect x="8" y="11" width="6" height="1.5" rx="0.75" fill="#1e1e2e" opacity="0.5"/>
  <rect x="8" y="14" width="4" height="1.5" rx="0.75" fill="#1e1e2e" opacity="0.5"/>
</svg>'''

ICONS = {
    "green":   _write_icon("green",   _lock_svg("#57e389", "#57e389")),
    "red":     _write_icon("red",     _lock_svg("#e01b24", "#e01b24")),
    "orange":  _write_icon("orange",  _lock_svg("#ff7800", "#ff7800")),
    "grey":    _write_icon("grey",    _lock_svg("#888888")),
    "syncing": _write_icon("syncing", _lock_svg("#62a0ea", "#62a0ea")),
    "paste":   _write_icon("paste",   _paste_svg()),
}

# ── State file helpers ─────────────────────────────────────────────────────────

def _write_state(state: str):
    with open(STATE_FILE, "w") as f:
        f.write(state)

def _bump_version():
    try:
        with open(VERSION_FILE) as f:
            cur = int(f.read().strip())
    except Exception:
        cur = 0
    with open(VERSION_FILE, "w") as f:
        f.write(str(cur + 1))

# ── 1Password fetch ────────────────────────────────────────────────────────────

def fetch_from_1password() -> tuple[dict[str, str], str | None]:
    """
    Fetch all items from the Exports vault via `op` CLI.
    Each item needs an `env` field (env var name) and a `credential` field (secret value).
    Returns (dict of env_name→value, error_string or None).
    """
    try:
        r = subprocess.run(
            ["op", "item", "list", "--vault", "Exports", "--format", "json"],
            capture_output=True, text=True
        )
    except FileNotFoundError:
        return {}, (
            "1Password CLI (op) not found.\n"
            "Install it from: https://developer.1password.com/docs/cli/get-started/"
        )

    if r.returncode != 0:
        stderr = r.stderr.strip()
        # Friendly message for the most common auth failure
        if "not currently signed in" in stderr or "authentication" in stderr.lower():
            return {}, "Not signed in to 1Password — run: op signin"
        if "no such vault" in stderr.lower() or "vault" in stderr.lower():
            return {}, "Vault 'Exports' not found — create it in 1Password first"
        return {}, stderr or "op item list failed (unknown error)"

    try:
        items = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        return {}, f"JSON parse error: {e}"

    results = {}
    for item in items:
        try:
            r2 = subprocess.run(
                ["op", "item", "get", item["id"],
                 "--vault", "Exports", "--format", "json"],
                capture_output=True, text=True
            )
        except FileNotFoundError:
            return {}, "1Password CLI (op) not found"

        if r2.returncode != 0:
            log.warning("Could not fetch item %s: %s", item.get("title", "?"), r2.stderr.strip())
            continue
        try:
            detail = json.loads(r2.stdout)
        except json.JSONDecodeError:
            continue

        fields = detail.get("fields", [])
        env_name = next(
            (f.get("value", "") for f in fields if f.get("label") == "env"), ""
        )
        cred_value = next(
            (f.get("value", "") for f in fields if f.get("id") == "credential"), ""
        )
        if env_name and cred_value:
            results[env_name] = cred_value
        elif env_name and not cred_value:
            log.warning("Item '%s' has 'env' field but no 'credential' field — skipping",
                        item.get("title", "?"))

    return results, None

# ── Unix socket server ─────────────────────────────────────────────────────────

class _Handler(socketserver.StreamRequestHandler):
    """Serve a single GET request from a shell hook."""
    def handle(self):
        try:
            line = self.rfile.readline().strip().decode(errors="ignore")
            if line != "GET":
                return
            payload = self.server.app.get_export_payload()
            self.wfile.write(payload.encode())
        except Exception:
            pass

class _SocketServer(socketserver.UnixStreamServer):
    allow_reuse_address = True

    def __init__(self, app: "OpKeysyncApp"):
        self.app = app
        if os.path.exists(SOCK_PATH):
            os.unlink(SOCK_PATH)
        super().__init__(SOCK_PATH, _Handler)
        # Only owner can connect — extra safety on top of SSH_CONNECTION check in hook
        os.chmod(SOCK_PATH, 0o600)

# ── App ────────────────────────────────────────────────────────────────────────

class OpKeysyncApp:
    def __init__(self):
        # Fernet key generated fresh at startup — never persisted, never leaves process
        self._fernet        = Fernet(Fernet.generate_key())
        self._enc_keys: bytes | None = None   # Fernet-encrypted JSON blob of keys
        self._key_names: list[str]   = []     # names only, for menu display
        self._locked        = False
        self._syncing       = False
        self._last_error: str | None = None
        self._clipboard_active  = False
        self._clipboard_timer   = None    # GLib timer source id
        # Protects _enc_keys and _locked — accessed from both GTK thread and socket thread
        self._secrets_lock  = RLock()

        # Surface missing dependencies in tray immediately
        if _missing_deps:
            self._last_error = "Missing deps — see tray menu"

        # Write initial runtime state
        _write_state("unlocked")
        _bump_version()

        # ── Tray indicator ────────────────────────────────────────────────────
        self._indicator = AppIndicator.Indicator.new(
            "op-keysync",
            ICONS["grey"],
            AppIndicator.IndicatorCategory.SYSTEM_SERVICES,
        )
        self._indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self._indicator.set_title("1Password Key Sync")

        self._menu = Gtk.Menu()
        self._indicator.set_menu(self._menu)
        self._rebuild_menu()
        self._menu.connect("show", lambda _: self._rebuild_menu())

        # ── Unix socket server (background thread) ────────────────────────────
        self._server = _SocketServer(self)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

        # ── Auto-sync on startup ──────────────────────────────────────────────
        GLib.timeout_add_seconds(2, self._startup_sync)

        # ── D-Bus screen-lock listener ────────────────────────────────────────
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        bus = dbus.SessionBus()
        bus.add_signal_receiver(
            self._on_screensaver_active_changed,
            signal_name="ActiveChanged",
            dbus_interface="org.gnome.ScreenSaver",
            path="/org/gnome/ScreenSaver",
        )

        # ── Mutter idle monitor ───────────────────────────────────────────────
        # Fires _on_idle after IDLE_TIMEOUT_MS of no keyboard/mouse input.
        # Fires _on_idle_resume when input resumes (user comes back).
        self._idle_watch_id: int | None = None
        self._idle_resume_watch_id: int | None = None
        self._idle_monitor = None
        try:
            mutter_obj = bus.get_object(
                "org.gnome.Mutter.IdleMonitor",
                "/org/gnome/Mutter/IdleMonitor/Core",
            )
            self._idle_monitor = dbus.Interface(
                mutter_obj, "org.gnome.Mutter.IdleMonitor"
            )
            # Register idle threshold watch
            self._idle_watch_id = self._idle_monitor.AddIdleWatch(
                dbus.UInt64(IDLE_TIMEOUT_MS)
            )
            # Listen for WatchFired signals from Mutter
            bus.add_signal_receiver(
                self._on_mutter_watch_fired,
                signal_name="WatchFired",
                dbus_interface="org.gnome.Mutter.IdleMonitor",
                path="/org/gnome/Mutter/IdleMonitor/Core",
            )
        except dbus.DBusException as e:
            print(f"[op-keysync] Mutter idle monitor unavailable: {e}")

    # ── Secret store ───────────────────────────────────────────────────────────

    def get_export_payload(self) -> str:
        """Called by socket handler (separate thread). Returns export lines or empty."""
        with self._secrets_lock:
            if self._locked or self._enc_keys is None:
                return ""
            try:
                raw  = self._fernet.decrypt(self._enc_keys)
                keys = json.loads(raw)
                return "".join(f"export {k}={v}\n" for k, v in keys.items())
            except Exception:
                return ""

    def _store_keys(self, keys: dict[str, str]):
        with self._secrets_lock:
            raw = json.dumps(keys).encode()
            self._enc_keys  = self._fernet.encrypt(raw)
            self._key_names = sorted(keys.keys())

    def _purge_keys(self):
        """Discard encrypted blob — secrets gone from memory."""
        with self._secrets_lock:
            self._enc_keys  = None
            self._key_names = []

    # ── Lock / unlock ──────────────────────────────────────────────────────────

    def _on_screensaver_active_changed(self, active):
        if bool(active):
            GLib.idle_add(self._do_lock)
        else:
            GLib.idle_add(self._do_unlock)

    def _on_mutter_watch_fired(self, watch_id: int):
        watch_id = int(watch_id)
        if watch_id == self._idle_watch_id:
            # User has been idle for IDLE_TIMEOUT_MINUTES — purge keys
            GLib.idle_add(self._do_idle_lock)
            # Register a user-active (resume) watch so we know when they're back
            if self._idle_monitor:
                try:
                    self._idle_resume_watch_id = int(
                        self._idle_monitor.AddUserActiveWatch()
                    )
                except dbus.DBusException:
                    pass
        elif watch_id == self._idle_resume_watch_id:
            # User moved mouse / pressed a key — resume (re-fetch)
            self._idle_resume_watch_id = None
            GLib.idle_add(self._do_unlock)

    def _do_idle_lock(self):
        """Idle timeout fired — treat the same as a screen lock."""
        with self._secrets_lock:
            self._locked = True
        self._purge_keys()
        _write_state("locked")
        _bump_version()
        self._update_icon()
        self._rebuild_menu()
        self._notify(
            f"⏱ Idle for {IDLE_TIMEOUT_MINUTES} min — API keys purged from all shells"
        )

    def _do_lock(self):
        with self._secrets_lock:
            self._locked = True
        self._purge_keys()
        _write_state("locked")
        _bump_version()
        self._update_icon()
        self._rebuild_menu()
        self._notify("🔒 Screen locked — API keys purged from all shells")


    def _do_unlock(self):
        with self._secrets_lock:
            self._locked = False
        _write_state("unlocked")
        # Immediately refresh UI so tray no longer shows "Locked"
        self._update_icon()
        self._rebuild_menu()
        # Wait 5s after unlock — gives 1Password desktop app time to wake up
        GLib.timeout_add_seconds(5, self._delayed_unlock_sync)

    def _delayed_unlock_sync(self):
        self._start_sync(reason="unlock")
        return False  # one-shot

    def _startup_sync(self):
        self._start_sync(reason="startup")
        return False  # one-shot

    # ── Sync ───────────────────────────────────────────────────────────────────

    def _start_sync(self, reason: str = "manual"):
        if self._syncing:
            return
        self._syncing = True
        self._update_icon()
        self._rebuild_menu()
        threading.Thread(target=self._do_sync, args=(reason,), daemon=True).start()

    def _do_sync(self, reason: str):
        keys, error = fetch_from_1password()
        GLib.idle_add(self._apply_sync_result, keys, error, reason)

    def _apply_sync_result(self, keys: dict, error: str | None, reason: str):
        self._syncing    = False
        self._last_error = error

        if error:
            self._notify(f"⚠️ Sync failed: {error[:80]}")
        elif keys:
            self._store_keys(keys)
            _bump_version()   # shells will pick up new values on next prompt
            self._notify(f"✅ {len(keys)} keys loaded — all local shells updated")
        else:
            # 1Password returned 0 items — vault empty or no matching fields
            self._notify("⚠️ No keys found — check your Exports vault has items with 'env' and 'credential' fields")

        self._update_icon()
        self._rebuild_menu()

    # ── Tray menu ──────────────────────────────────────────────────────────────

    def _rebuild_menu(self):
        for child in self._menu.get_children():
            self._menu.remove(child)

        # ── Status header ─────────────────────────────────────────────────────
        if self._locked:
            status = "🔒 Locked — keys purged"
        elif self._syncing:
            status = "⟳ Syncing from 1Password…"
        elif self._key_names:
            status = f"🔓 Unlocked — {len(self._key_names)} keys in memory"
        elif self._last_error:
            status = f"⚠️  {self._last_error[:50]}"
        else:
            status = "⬜ No keys — press Full Sync"

        idle_note = Gtk.MenuItem(label=f"  ⏱ Auto-purge after {IDLE_TIMEOUT_MINUTES} min idle")
        idle_note.set_sensitive(False)

        hdr = Gtk.MenuItem(label=status)
        hdr.set_sensitive(False)
        self._menu.append(hdr)
        self._menu.append(idle_note)

        if self._clipboard_active:
            clip_note = Gtk.MenuItem(label="  📋 Secret in clipboard — waiting for paste…")
            clip_note.set_sensitive(False)
            self._menu.append(clip_note)

        # ── Missing dependencies warning ──────────────────────────────────────
        if _missing_deps:
            self._menu.append(Gtk.SeparatorMenuItem())
            warn_item = Gtk.MenuItem(label="⚠️  Missing dependencies:")
            warn_item.set_sensitive(False)
            self._menu.append(warn_item)
            for dep_line in _missing_deps:
                # dep_line is "  • cmd: install hint"
                cmd = dep_line.strip().lstrip("• ").split(":")[0]
                hint = dep_line.split(":", 1)[1].strip() if ":" in dep_line else dep_line
                dep_item = Gtk.MenuItem(label=f"  {cmd}  —  {hint}")
                dep_item.set_sensitive(False)
                self._menu.append(dep_item)

        self._menu.append(Gtk.SeparatorMenuItem())

        # ── Keys — hover to expand submenu, copy value or KEY=VALUE ─────────
        if self._key_names:
            keys = self._get_decrypted_keys()
            for name in self._key_names:
                value = keys.get(name, "")

                # Parent item — no activate handler, just opens submenu on hover
                item = Gtk.MenuItem(label=f"  {name}")

                sub = Gtk.Menu()

                copy_val = Gtk.MenuItem(label="Copy value")
                copy_val.connect("activate", self._on_copy_value, name, value)
                sub.append(copy_val)

                copy_kv = Gtk.MenuItem(label="Copy KEY=VALUE")
                copy_kv.connect("activate", self._on_copy_kv, name, value)
                sub.append(copy_kv)

                item.set_submenu(sub)
                self._menu.append(item)
        else:
            placeholder = Gtk.MenuItem(label="  (no keys in memory)")
            placeholder.set_sensitive(False)
            self._menu.append(placeholder)

        self._menu.append(Gtk.SeparatorMenuItem())

        # ── Full Sync ─────────────────────────────────────────────────────────
        sync_item = Gtk.MenuItem(label="↻  Full Sync  (fetch from 1Password)")
        sync_item.connect("activate", lambda _: self._start_sync("manual"))
        sync_item.set_sensitive(not self._syncing and not self._locked)
        self._menu.append(sync_item)

        self._menu.append(Gtk.SeparatorMenuItem())

        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", self._on_quit)
        self._menu.append(quit_item)

        self._menu.show_all()

    # ── Tray icon ──────────────────────────────────────────────────────────────

    def _update_icon(self):
        if self._clipboard_active:
            icon, tip = ICONS["paste"],   "Secret in clipboard — paste it! (clears in 20s)"
        elif self._syncing:
            icon, tip = ICONS["syncing"], "Syncing from 1Password…"
        elif self._locked:
            icon, tip = ICONS["red"],     "Locked — keys purged from memory"
        elif self._last_error and not self._key_names:
            icon, tip = ICONS["orange"],  f"Sync error: {self._last_error[:60]}"
        elif self._key_names:
            icon, tip = ICONS["green"],   f"{len(self._key_names)} keys loaded"
        else:
            icon, tip = ICONS["grey"],    "No keys — use Full Sync"
        self._indicator.set_icon_full(icon, tip)

    # ── Clipboard ──────────────────────────────────────────────────────────────

    def _get_decrypted_keys(self) -> dict[str, str]:
        """Decrypt and return all keys — only called for clipboard, never stored."""
        with self._secrets_lock:
            if self._locked or self._enc_keys is None:
                return {}
            try:
                return json.loads(self._fernet.decrypt(self._enc_keys))
            except Exception:
                return {}

    def _copy_to_clipboard(self, text: str):
        """Copy text to clipboard via wl-copy (stdin — value not in process list).
        Clipboard auto-clears after 60 seconds via timer.
        """
        preview = text[:6] + "…" if len(text) > 6 else text
        log.debug("COPY called  text_len=%d preview=%s", len(text), preview)

        try:
            subprocess.run(["wl-copy"], input=text, text=True, check=True)
            log.debug("COPY wl-copy (CLIPBOARD) done")
        except FileNotFoundError:
            log.error("wl-copy not found — install wl-clipboard: sudo apt install wl-clipboard")
            self._notify("⚠️ wl-clipboard not installed — sudo apt install wl-clipboard")
            return
        except Exception as e:
            log.error("COPY wl-copy (CLIPBOARD) FAILED: %s", e)

        try:
            subprocess.run(["wl-copy", "--primary"], input=text, text=True, check=True)
            log.debug("COPY wl-copy (PRIMARY) done")
        except FileNotFoundError:
            pass  # PRIMARY clipboard optional — CLIPBOARD already copied above
        except Exception as e:
            log.error("COPY wl-copy (PRIMARY) FAILED: %s", e)

        self._clipboard_start()

    def _on_copy_value(self, _item, name: str, value: str):
        log.debug("CLICK Copy value: %s", name)
        self._copy_to_clipboard(value)
        self._notify(f"📋 {name} — value copied  (clears in 20s)")

    def _on_copy_kv(self, _item, name: str, value: str):
        log.debug("CLICK Copy KEY=VALUE: %s", name)
        self._copy_to_clipboard(f"{name}={value}")
        self._notify(f"📋 {name}=VALUE copied  (clears in 20s)")

    def _clipboard_start(self):
        """Mark clipboard active, show paste icon, start 60s fallback timer."""
        log.debug("CLIPBOARD_START active=%s", self._clipboard_active)
        # Cancel any existing timer
        if self._clipboard_timer is not None:
            GLib.source_remove(self._clipboard_timer)
        self._clipboard_active = True
        self._update_icon()
        self._clipboard_timer = GLib.timeout_add_seconds(20, self._clipboard_expire)

    def _clipboard_expire(self):
        """60s passed without paste — clear clipboard and restore icon."""
        log.debug("CLIPBOARD_EXPIRE (60s timeout)")
        self._clipboard_clear()
        return False  # one-shot

    def _clipboard_clear(self):
        """Clear clipboard and restore tray icon."""
        log.debug("CLIPBOARD_CLEAR called  active=%s", self._clipboard_active)
        if not self._clipboard_active:
            log.debug("CLIPBOARD_CLEAR skipped (not active)")
            return
        if self._clipboard_timer is not None:
            GLib.source_remove(self._clipboard_timer)
            self._clipboard_timer = None
        self._clipboard_active = False
        try:
            subprocess.run(["wl-copy", "--clear"], check=True)
            subprocess.run(["wl-copy", "--clear", "--primary"], check=True)
            log.debug("CLIPBOARD_CLEAR done — clipboard+primary cleared")
        except FileNotFoundError:
            log.error("wl-copy not found — clipboard was not cleared")
        except Exception as e:
            log.error("CLIPBOARD_CLEAR FAILED: %s", e)
        self._update_icon()

    # ── Quit ───────────────────────────────────────────────────────────────────

    def _on_quit(self, _item):
        self._purge_keys()
        _write_state("locked")
        _bump_version()
        try:
            self._server.shutdown()
            os.unlink(SOCK_PATH)
        except Exception:
            pass
        Gtk.main_quit()

    # ── Notify ────────────────────────────────────────────────────────────────

    def _notify(self, body: str):
        def _send():
            try:
                subprocess.run([
                    "notify-send", "-u", "normal", "-t", "4000",
                    "1Password Key Sync", body,
                ], check=False)
            except FileNotFoundError:
                log.warning("notify-send not found — install libnotify-bin: sudo apt install libnotify-bin")
        threading.Thread(target=_send, daemon=True).start()

    def run(self):
        Gtk.main()


if __name__ == "__main__":
    app = OpKeysyncApp()
    app.run()
