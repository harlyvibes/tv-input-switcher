#!/usr/bin/env python3
"""
Samsung TV Input Switcher
-------------------------
A small Tkinter GUI + system tray / menu bar icon that changes the input on a
Samsung SmartThings TV (e.g. Q90R / Q90T) using the SmartThings REST API
(https://api.smartthings.com). No SmartThings CLI needed at runtime.

Works on Windows, macOS and Linux, using only the Python standard library
(plus two optional packages for the tray icon).

Requirements
  * Python 3.8+ with Tkinter (included with the python.org installers)
  * A SmartThings sign-in, one of:
      - Personal access token (quickest): create one at
        https://account.smartthings.com/tokens with the Devices scopes.
        Tokens created after 30 Dec 2024 expire after 24 hours.
      - OAuth app (renews itself automatically): see "Account…" in the app
        and the README for the one-time setup.
  * Optional, for the tray / menu bar icon:  pip install pystray pillow

Usage
  python tv_input_switcher.py      (macOS: python3 tv_input_switcher.py)

How the tray works
  The tray icon runs in its own small helper process. macOS requires both
  Tkinter and the menu bar icon to own the main thread of their process, so
  they can't share one; running them separately makes the app behave the same
  way on every platform. The two processes talk over a pair of queues.
"""

import base64
import http.server
import json
import multiprocessing as mp
import os
import queue
import re
import secrets
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

try:
    import pystray
    from PIL import Image, ImageDraw
    HAVE_TRAY = True
except ImportError:
    HAVE_TRAY = False

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win")

APP_NAME = "TV Input Switcher"
CONFIG_PATH = Path.home() / ".tv_input_switcher.json"

API = "https://api.smartthings.com/v1"
OAUTH_AUTHORIZE = "https://api.smartthings.com/oauth/authorize"
OAUTH_TOKEN = "https://api.smartthings.com/oauth/token"
OAUTH_SCOPES = "r:devices:* x:devices:*"
DEFAULT_REDIRECT = "https://httpbin.org/get"
TOKENS_PAGE = "https://account.smartthings.com/tokens"
HTTP_TIMEOUT = 20

# Capabilities Samsung TVs use for input selection (standard one first).
INPUT_CAPS = ("mediaInputSource", "samsungvd.mediaInputSource")

FRIENDLY_NAMES = {
    "digitaltv": "TV", "dtv": "TV",
    "hdmi1": "HDMI 1", "hdmi2": "HDMI 2", "hdmi3": "HDMI 3", "hdmi4": "HDMI 4",
    "usb": "USB", "am": "AM", "fm": "FM",
}


class APIError(Exception):
    pass


class AuthError(APIError):
    """Missing, expired or rejected credentials."""


# ---------------------------------------------------------------- config
_config_lock = threading.Lock()


def load_config():
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
    except Exception:
        cfg = {}
    cfg.setdefault("auth", {})
    cfg["auth"].setdefault("mode", "pat")
    cfg["auth"].setdefault("redirect_uri", DEFAULT_REDIRECT)
    cfg.setdefault("device_id", None)
    return cfg


def save_config(cfg):
    with _config_lock:
        try:
            CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
            if not IS_WIN:
                os.chmod(CONFIG_PATH, 0o600)  # the file holds your tokens
        except OSError:
            pass


# ---------------------------------------------------------------- HTTP
def http_json(method, url, headers=None, body=None, form=None):
    headers = dict(headers or {})
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    headers.setdefault("Accept", "application/json")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            j = json.loads(detail)
            err = j.get("error")
            detail = (err.get("message") if isinstance(err, dict) else None) \
                or j.get("error_description") or err or detail
        except Exception:
            pass
        if e.code in (401, 403):
            raise AuthError(f"SmartThings rejected the sign-in ({e.code}): {detail}")
        raise APIError(f"SmartThings API error {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise APIError(f"Couldn't reach SmartThings: {e.reason}")


# ---------------------------------------------------------------- auth
class SmartThingsAuth:
    """Hands out a valid bearer token, refreshing OAuth tokens as needed.
    `on_change` is called (from any thread) when tokens are updated."""

    def __init__(self, auth_cfg, on_change):
        self.cfg = auth_cfg
        self.on_change = on_change
        self.lock = threading.Lock()

    def _basic(self):
        pair = f"{self.cfg.get('client_id', '')}:{self.cfg.get('client_secret', '')}"
        return "Basic " + base64.b64encode(pair.encode()).decode()

    def _store(self, tok):
        self.cfg["access_token"] = tok["access_token"]
        if tok.get("refresh_token"):
            self.cfg["refresh_token"] = tok["refresh_token"]
        self.cfg["expires_at"] = time.time() + float(tok.get("expires_in", 86400))
        self.on_change()

    def authorize_url(self, state):
        return OAUTH_AUTHORIZE + "?" + urllib.parse.urlencode({
            "client_id": self.cfg["client_id"],
            "response_type": "code",
            "redirect_uri": self.cfg["redirect_uri"],
            "scope": OAUTH_SCOPES,
            "state": state,
        })

    def exchange_code(self, code):
        tok = http_json("POST", OAUTH_TOKEN, headers={"Authorization": self._basic()}, form={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.cfg["client_id"],
            "redirect_uri": self.cfg["redirect_uri"],
        })
        with self.lock:
            self._store(tok)

    def _refresh(self):
        if not self.cfg.get("refresh_token"):
            raise AuthError("Not signed in. Open Account… and sign in.")
        try:
            tok = http_json("POST", OAUTH_TOKEN, headers={"Authorization": self._basic()}, form={
                "grant_type": "refresh_token",
                "client_id": self.cfg["client_id"],
                "refresh_token": self.cfg["refresh_token"],
            })
        except AuthError:
            raise AuthError(
                "Your SmartThings sign-in has expired (the app wasn't used "
                "for about 30 days). Open Account… and sign in again."
            )
        self._store(tok)

    def token(self, min_valid=300):
        if self.cfg.get("mode") == "pat":
            pat = (self.cfg.get("pat") or "").strip()
            if not pat:
                raise AuthError("No access token set. Open Account… to add one.")
            return pat
        with self.lock:
            if not self.cfg.get("client_id"):
                raise AuthError("OAuth isn't set up. Open Account… to set it up.")
            if time.time() + min_valid >= float(self.cfg.get("expires_at", 0)):
                self._refresh()
            return self.cfg["access_token"]

    def keep_alive(self):
        """Refresh early so the 30-day refresh token keeps renewing while the
        app runs in the tray."""
        if self.cfg.get("mode") == "oauth" and self.cfg.get("refresh_token"):
            self.token(min_valid=4 * 3600)


# ---------------------------------------------------------------- API
class SmartThingsAPI:
    def __init__(self, auth):
        self.auth = auth

    def _call(self, method, path_or_url, body=None):
        url = path_or_url if path_or_url.startswith("http") else API + path_or_url
        try:
            return http_json(method, url, headers={
                "Authorization": f"Bearer {self.auth.token()}"}, body=body)
        except AuthError as e:
            if self.auth.cfg.get("mode") == "pat":
                raise AuthError(
                    f"{e}\n\nPersonal access tokens created after 30 Dec 2024 "
                    "only last 24 hours. Create a new one, or switch to OAuth "
                    "under Account… so the app renews its sign-in itself."
                )
            raise

    def list_tvs(self):
        """[(device_id, label)] for devices that support input switching."""
        tvs, url = [], "/devices"
        while url:
            page = self._call("GET", url)
            for d in page.get("items", []):
                caps = {c.get("id") for comp in d.get("components", [])
                        for c in comp.get("capabilities", [])}
                if caps & set(INPUT_CAPS):
                    tvs.append((d["deviceId"], d.get("label") or d.get("name") or d["deviceId"]))
            url = ((page.get("_links") or {}).get("next") or {}).get("href")
        return tvs

    def tv_state(self, device_id):
        status = self._call("GET", f"/devices/{device_id}/status")
        main = status.get("components", {}).get("main", {})
        power = main.get("switch", {}).get("switch", {}).get("value")
        for cap in INPUT_CAPS:
            data = main.get(cap)
            if not data:
                continue
            current = (data.get("inputSource") or {}).get("value")
            inputs = []
            if cap == "samsungvd.mediaInputSource":
                for item in (data.get("supportedInputSourcesMap") or {}).get("value") or []:
                    inputs.append((item.get("id"), item.get("name") or item.get("id")))
            else:
                for src in (data.get("supportedInputSources") or {}).get("value") or []:
                    inputs.append((src, FRIENDLY_NAMES.get(src.lower(), src)))
            if inputs:
                return {"power": power, "cap": cap, "current": current, "inputs": inputs}
        raise APIError("This TV didn't report any input sources. Make sure it's "
                       "on and try Refresh.")

    def command(self, device_id, capability, command, *args):
        self._call("POST", f"/devices/{device_id}/commands", body={"commands": [{
            "component": "main", "capability": capability,
            "command": command, "arguments": list(args),
        }]})


def extract_code(text):
    """Pull an OAuth code out of a pasted redirect URL, page text, or bare code."""
    text = text.strip()
    q = urllib.parse.parse_qs(urllib.parse.urlparse(text).query)
    if q.get("code"):
        return q["code"][0]
    m = re.search(r'"code"\s*:\s*"([^"]+)"', text) or re.search(r"[?&]code=([^&\s\"]+)", text)
    if m:
        return m.group(1)
    return text if re.fullmatch(r"[A-Za-z0-9_\-]+", text) else None


def capture_code_locally(redirect_uri, state, timeout=180):
    """If the redirect URI points at this computer, catch the code with a tiny
    local web server. Returns the code, or None if it didn't arrive."""
    u = urllib.parse.urlparse(redirect_uri)
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [None])[0] == state and q.get("code"):
                result["code"] = q["code"][0]
                msg = "Signed in. You can close this tab and return to TV Input Switcher."
            else:
                msg = "Sign-in failed or was cancelled."
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<p style='font:16px sans-serif'>{msg}</p>".encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer((u.hostname, u.port or 80), Handler)
    srv.timeout = 1
    end = time.time() + timeout
    while "code" not in result and time.time() < end:
        srv.handle_request()
    srv.server_close()
    return result.get("code")


# ---------------------------------------------------------------- tray process
def make_tray_image(size=64):
    """Draw a simple TV icon so no image file is needed."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64
    d.rounded_rectangle([4 * s, 8 * s, 60 * s, 46 * s], radius=5 * s,
                        fill=(30, 30, 30, 255), outline=(200, 200, 200, 255),
                        width=max(1, int(3 * s)))
    d.rectangle([10 * s, 14 * s, 54 * s, 40 * s], fill=(40, 120, 220, 255))
    d.rectangle([28 * s, 46 * s, 36 * s, 53 * s], fill=(200, 200, 200, 255))
    d.rectangle([18 * s, 53 * s, 46 * s, 58 * s], fill=(200, 200, 200, 255))
    return img


def tray_process(state_q, cmd_q):
    """Runs in a separate process: owns the tray / menu bar icon.

    Receives state snapshots on `state_q` (None means exit) and sends
    user commands as tuples on `cmd_q`.
    """
    call_on_main = lambda fn: fn()  # noqa: E731
    if IS_MAC:
        try:
            import AppKit  # keep this helper out of the Dock
            AppKit.NSApplication.sharedApplication().setActivationPolicy_(
                AppKit.NSApplicationActivationPolicyAccessory)
        except Exception:
            pass
        try:
            from PyObjCTools import AppHelper  # AppKit needs the main thread
            call_on_main = AppHelper.callAfter
        except Exception:
            pass

    snap = {"tvs": [], "device": None, "inputs": [], "current": None}

    def send(*msg):
        return lambda icon, item: cmd_q.put(msg)

    def device_items():
        if not snap["tvs"]:
            yield pystray.MenuItem("(no TVs found yet)", None, enabled=False)
            return
        for idx, (dev_id, label) in enumerate(snap["tvs"]):
            yield pystray.MenuItem(label, send("select_tv", idx),
                                   checked=lambda item, d=dev_id: snap["device"] == d,
                                   radio=True)

    def input_items():
        if not snap["inputs"]:
            yield pystray.MenuItem("(refresh to load inputs)", None, enabled=False)
            return
        for src_id, name in snap["inputs"]:
            yield pystray.MenuItem(name, send("input", src_id),
                                   checked=lambda item, s=src_id: snap["current"] == s,
                                   radio=True)

    menu = pystray.Menu(
        pystray.MenuItem("Show window", send("show"), default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Device", pystray.Menu(device_items)),
        pystray.MenuItem("Input", pystray.Menu(input_items)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Power On", send("power", "on")),
        pystray.MenuItem("Power Off", send("power", "off")),
        pystray.MenuItem("Refresh", send("refresh")),
        pystray.MenuItem("Reload device list", send("reload")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", send("quit")),
    )
    icon = pystray.Icon("tv_input_switcher", make_tray_image(), APP_NAME, menu)

    def listen():
        while True:
            try:
                msg = state_q.get()
            except (EOFError, OSError):
                msg = None
            if msg is None:
                call_on_main(icon.stop)
                return
            snap.update(msg)
            call_on_main(icon.update_menu)

    def setup(icon):
        icon.visible = True
        threading.Thread(target=listen, daemon=True).start()

    icon.run(setup=setup)


# ---------------------------------------------------------------- account dialog
class AccountDialog(tk.Toplevel):
    """Choose between a personal access token and an OAuth app, and sign in."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("SmartThings Account")
        self.resizable(False, False)
        self.transient(app)
        a = app.cfg["auth"]

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)

        self.mode = tk.StringVar(value=a.get("mode", "pat"))
        ttk.Radiobutton(frm, text="Personal access token (quick; new tokens last 24 hours)",
                        variable=self.mode, value="pat", command=self._toggle
                        ).grid(row=0, column=0, columnspan=3, sticky="w")
        self.pat = tk.StringVar(value=a.get("pat", ""))
        ttk.Label(frm, text="Token:").grid(row=1, column=0, sticky="w", padx=(20, 6))
        self.pat_entry = ttk.Entry(frm, textvariable=self.pat, width=44, show="•")
        self.pat_entry.grid(row=1, column=1, sticky="ew")
        self.pat_btn = ttk.Button(frm, text="Get token…",
                                  command=lambda: webbrowser.open(TOKENS_PAGE))
        self.pat_btn.grid(row=1, column=2, padx=(6, 0))

        ttk.Separator(frm).grid(row=2, column=0, columnspan=3, sticky="ew", pady=10)

        ttk.Radiobutton(frm, text="OAuth app (renews itself; one-time setup, see README)",
                        variable=self.mode, value="oauth", command=self._toggle
                        ).grid(row=3, column=0, columnspan=3, sticky="w")
        self.client_id = tk.StringVar(value=a.get("client_id", ""))
        self.client_secret = tk.StringVar(value=a.get("client_secret", ""))
        self.redirect = tk.StringVar(value=a.get("redirect_uri", DEFAULT_REDIRECT))
        self.oauth_widgets = []
        for r, (label, var, show) in enumerate([
            ("Client ID:", self.client_id, ""),
            ("Client secret:", self.client_secret, "•"),
            ("Redirect URI:", self.redirect, ""),
        ], start=4):
            ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", padx=(20, 6))
            e = ttk.Entry(frm, textvariable=var, width=44, show=show)
            e.grid(row=r, column=1, columnspan=2, sticky="ew", pady=2)
            self.oauth_widgets.append(e)
        signed_in = "Signed in" if a.get("refresh_token") else "Not signed in"
        self.oauth_status = ttk.Label(frm, text=signed_in, foreground="#666")
        self.oauth_status.grid(row=7, column=1, sticky="w")
        self.signin_btn = ttk.Button(frm, text="Sign in…", command=self.sign_in)
        self.signin_btn.grid(row=7, column=2, sticky="e", pady=(4, 0))
        self.oauth_widgets.append(self.signin_btn)

        btns = ttk.Frame(frm)
        btns.grid(row=8, column=0, columnspan=3, sticky="e", pady=(14, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Save", command=self.save).pack(side="right", padx=6)

        self._toggle()
        self.grab_set()

    def _toggle(self):
        oauth = self.mode.get() == "oauth"
        for w in self.oauth_widgets:
            w.config(state="normal" if oauth else "disabled")
        for w in (self.pat_entry, self.pat_btn):
            w.config(state="disabled" if oauth else "normal")

    def _apply(self):
        a = self.app.cfg["auth"]
        old = (a.get("client_id"), a.get("client_secret"), a.get("redirect_uri"))
        a["mode"] = self.mode.get()
        a["pat"] = self.pat.get().strip()
        a["client_id"] = self.client_id.get().strip()
        a["client_secret"] = self.client_secret.get().strip()
        a["redirect_uri"] = self.redirect.get().strip() or DEFAULT_REDIRECT
        if old != (a["client_id"], a["client_secret"], a["redirect_uri"]):
            for k in ("access_token", "refresh_token", "expires_at"):
                a.pop(k, None)
        save_config(self.app.cfg)

    def save(self):
        self._apply()
        self.destroy()
        self.app.load_tvs()

    def sign_in(self):
        self._apply()
        a = self.app.cfg["auth"]
        if not (a["client_id"] and a["client_secret"]):
            messagebox.showerror(APP_NAME, "Enter the OAuth client ID and secret first.", parent=self)
            return
        state = secrets.token_urlsafe(16)
        webbrowser.open(self.app.auth.authorize_url(state))

        u = urllib.parse.urlparse(a["redirect_uri"])
        if u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1"):
            # Redirect comes back to this computer: catch it automatically.
            self.oauth_status.config(text="Waiting for the browser…")

            def work():
                code = capture_code_locally(a["redirect_uri"], state)
                if not code:
                    raise AuthError("Didn't receive a sign-in from the browser.")
                self.app.auth.exchange_code(code)
            self._run_signin(work)
        else:
            text = simpledialog.askstring(
                APP_NAME,
                "Sign in and approve access in the browser. You'll land on a "
                "page whose address contains ?code=…\n\n"
                "Copy that whole address (or the code) and paste it here:",
                parent=self,
            )
            code = extract_code(text or "")
            if not code:
                return
            self._run_signin(lambda: self.app.auth.exchange_code(code))

    def _run_signin(self, work):
        self.signin_btn.config(state="disabled")

        def target():
            try:
                work()
                err = None
            except Exception as e:  # noqa: BLE001
                err = e
            self.after(0, lambda: self._signin_done(err))
        threading.Thread(target=target, daemon=True).start()

    def _signin_done(self, err):
        if not self.winfo_exists():
            return
        self.signin_btn.config(state="normal")
        if err:
            self.oauth_status.config(text="Sign-in failed")
            messagebox.showerror(APP_NAME, str(err), parent=self)
        else:
            self.oauth_status.config(text="Signed in")
            messagebox.showinfo(APP_NAME, "Signed in to SmartThings.", parent=self)


# ---------------------------------------------------------------- main window
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.minsize(360, 200)
        self.resizable(True, False)

        self.cfg = load_config()
        self.auth = SmartThingsAuth(self.cfg["auth"], lambda: save_config(self.cfg))
        self.api = SmartThingsAPI(self.auth)
        self.tvs = []  # [(id, label)]
        self.state_info = None
        self.busy = False

        pad = {"padx": 10, "pady": 6}

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="TV:").pack(side="left")
        self.tv_combo = ttk.Combobox(top, state="readonly")
        self.tv_combo.pack(side="left", fill="x", expand=True, padx=(6, 6))
        self.tv_combo.bind("<<ComboboxSelected>>", self.on_tv_selected)
        self.refresh_btn = ttk.Button(top, text="Refresh", command=self.refresh_state)
        self.refresh_btn.pack(side="left")
        self.account_btn = ttk.Button(top, text="Account…", command=self.open_account)
        self.account_btn.pack(side="left", padx=(4, 0))

        power = ttk.Frame(self)
        power.pack(fill="x", **pad)
        self.on_btn = ttk.Button(power, text="Power On", command=lambda: self.power("on"))
        self.off_btn = ttk.Button(power, text="Power Off", command=lambda: self.power("off"))
        self.on_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self.off_btn.pack(side="left", expand=True, fill="x", padx=(4, 0))

        heading_size = 13 if IS_MAC else 10
        ttk.Label(self, text="Inputs",
                  font=("TkDefaultFont", heading_size, "bold")).pack(anchor="w", padx=10)
        self.inputs_frame = ttk.Frame(self)
        self.inputs_frame.pack(fill="x", **pad)

        self.status_var = tk.StringVar(value="Starting…")
        ttk.Label(self, textvariable=self.status_var, foreground="#666",
                  wraplength=460, justify="left").pack(anchor="w", padx=10, pady=(0, 10))

        self.tray_proc = None
        if HAVE_TRAY:
            self.start_tray()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        if IS_MAC:
            self.createcommand("tk::mac::Quit", self.quit_app)
            self.createcommand("tk::mac::ReopenApplication", self.show_window)

        self.after(3 * 3600 * 1000, self.keep_alive)

        a = self.cfg["auth"]
        if (a["mode"] == "pat" and not a.get("pat")) or \
           (a["mode"] == "oauth" and not a.get("refresh_token")):
            self.status_var.set("Sign in to SmartThings to get started.")
            self.after(200, self.open_account)
        else:
            self.load_tvs()

    # ---- tray communication
    def start_tray(self):
        ctx = mp.get_context("spawn")
        self.tray_state_q = ctx.Queue()
        self.tray_cmd_q = ctx.Queue()
        self.tray_proc = ctx.Process(target=tray_process,
                                     args=(self.tray_state_q, self.tray_cmd_q), daemon=True)
        self.tray_proc.start()
        self.after(200, self.poll_tray)

    def tray_alive(self):
        return self.tray_proc is not None and self.tray_proc.is_alive()

    def poll_tray(self):
        if not self.tray_alive():
            self.tray_proc = None
            self.show_window()
            return
        while True:
            try:
                msg = self.tray_cmd_q.get_nowait()
            except queue.Empty:
                break
            self.handle_tray_command(msg)
        self.after(200, self.poll_tray)

    def handle_tray_command(self, msg):
        action, *args = msg
        if action == "show":
            self.show_window()
        elif action == "select_tv":
            self.select_tv(args[0])
        elif action == "input":
            self.set_input(args[0])
        elif action == "power":
            self.power(args[0])
        elif action == "refresh":
            self.refresh_state()
        elif action == "reload":
            self.load_tvs()
        elif action == "quit":
            self.quit_app()
            return
        self.update_tray()

    def update_tray(self):
        if not self.tray_alive():
            return
        info = self.state_info or {}
        try:
            self.tray_state_q.put({
                "tvs": list(self.tvs),
                "device": self.current_device(),
                "inputs": list(info.get("inputs", [])),
                "current": info.get("current"),
            })
        except Exception:
            pass

    # ---- window management
    def on_close(self):
        if self.tray_alive():
            self.withdraw()
        else:
            self.quit_app()

    def show_window(self):
        self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        self.after(150, lambda: self.attributes("-topmost", False))
        self.focus_force()

    def quit_app(self):
        if self.tray_proc is not None:
            try:
                self.tray_state_q.put(None)
                self.tray_proc.join(timeout=2)
                if self.tray_proc.is_alive():
                    self.tray_proc.terminate()
            except Exception:
                pass
            self.tray_proc = None
        self.destroy()

    def open_account(self):
        self.show_window()
        AccountDialog(self)

    def keep_alive(self):
        threading.Thread(target=lambda: self._quietly(self.auth.keep_alive), daemon=True).start()
        self.after(3 * 3600 * 1000, self.keep_alive)

    @staticmethod
    def _quietly(fn):
        try:
            fn()
        except Exception:
            pass

    # ---- background work
    def run_bg(self, label, work, done):
        """Run `work()` in a thread, then `done(result)` on the UI thread."""
        if self.busy:
            return
        self.busy = True
        self.set_controls_enabled(False)
        self.status_var.set(label)

        def target():
            try:
                result, err = work(), None
            except Exception as e:  # noqa: BLE001
                result, err = None, e
            self.after(0, lambda: self._finish(done, result, err))

        threading.Thread(target=target, daemon=True).start()

    def _finish(self, done, result, err):
        self.busy = False
        self.set_controls_enabled(True)
        if err:
            self.status_var.set("Error — see message.")
            if isinstance(err, AuthError):
                if messagebox.askyesno(APP_NAME, f"{err}\n\nOpen account settings now?"):
                    self.open_account()
            else:
                messagebox.showerror(APP_NAME, str(err))
        else:
            done(result)
        self.update_tray()

    def set_controls_enabled(self, enabled):
        st = "normal" if enabled else "disabled"
        for w in (self.refresh_btn, self.on_btn, self.off_btn, self.account_btn):
            w.config(state=st)
        self.tv_combo.config(state="readonly" if enabled else "disabled")
        for w in self.inputs_frame.winfo_children():
            w.config(state=st)

    # ---- actions
    def current_device(self):
        idx = self.tv_combo.current()
        return self.tvs[idx][0] if 0 <= idx < len(self.tvs) else None

    def load_tvs(self):
        def done(tvs):
            self.tvs = tvs
            self.tv_combo["values"] = [label for _, label in tvs]
            if not tvs:
                self.tv_combo.set("")
                self.status_var.set("No TVs with input control found on this SmartThings account.")
                return
            ids = [i for i, _ in tvs]
            saved = self.cfg.get("device_id")
            self.tv_combo.current(ids.index(saved) if saved in ids else 0)
            self.on_tv_selected()

        self.run_bg("Looking for TVs…", self.api.list_tvs, done)

    def on_tv_selected(self, _event=None):
        dev = self.current_device()
        if dev:
            self.cfg["device_id"] = dev
            save_config(self.cfg)
            self.refresh_state()

    def select_tv(self, idx):
        if self.busy or idx == self.tv_combo.current():
            return
        self.tv_combo.current(idx)
        self.state_info = None
        self.on_tv_selected()

    def refresh_state(self):
        dev = self.current_device()
        if not dev:
            return

        def done(info):
            self.state_info = info
            self.build_input_buttons()
            power = info["power"] or "unknown"
            current = dict(info["inputs"]).get(info["current"], info["current"])
            self.status_var.set(f"Power: {power}   •   Current input: {current or '—'}")

        self.run_bg("Reading TV status…", lambda: self.api.tv_state(dev), done)

    def build_input_buttons(self):
        for w in self.inputs_frame.winfo_children():
            w.destroy()
        info = self.state_info
        if not info:
            return
        cols = 3
        for i, (src_id, name) in enumerate(info["inputs"]):
            text = f"● {name}" if src_id == info["current"] else name
            b = ttk.Button(self.inputs_frame, text=text,
                           command=lambda s=src_id: self.set_input(s))
            b.grid(row=i // cols, column=i % cols, sticky="ew", padx=3, pady=3)
        for c in range(cols):
            self.inputs_frame.columnconfigure(c, weight=1)

    def set_input(self, src_id):
        dev = self.current_device()
        if not dev or not self.state_info:
            return
        cap = self.state_info["cap"]

        def done(_):
            self.state_info["current"] = src_id
            self.build_input_buttons()
            name = dict(self.state_info["inputs"]).get(src_id, src_id)
            self.status_var.set(f"Switched to {name}")

        self.run_bg(f"Switching to {src_id}…",
                    lambda: self.api.command(dev, cap, "setInputSource", src_id), done)

    def power(self, state):
        dev = self.current_device()
        if not dev:
            return

        def done(_):
            self.status_var.set(f"Power {state} sent")
            if state == "on":
                self.after(5000, self.refresh_state)  # let the TV wake first

        self.run_bg(f"Turning TV {state}…",
                    lambda: self.api.command(dev, "switch", state), done)


if __name__ == "__main__":
    mp.freeze_support()
    App().mainloop()
