#!/usr/bin/env python3
"""
Samsung TV Input Switcher
-------------------------
A small Tkinter GUI that changes the input on a Samsung SmartThings TV
(e.g. Q90R / Q90T) by calling the SmartThings CLI.

Requirements
  * Python 3.8+ (Tkinter ships with the standard Windows/macOS installers)
  * SmartThings CLI on your PATH:  https://github.com/SmartThingsCommunity/smartthings-cli
    Run `smartthings devices` once in a terminal first so it can log you in.
  * For the system tray icon:  pip install pystray pillow
    (without them the app still works, just without the tray icon)

Usage
  python tv_input_switcher.py

  Right-click the tray icon to pick a TV, switch inputs, or power on/off.
  Closing the window hides it to the tray; use "Quit" in the tray menu to exit.
"""

import json
import shutil
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

try:
    import pystray
    from PIL import Image, ImageDraw
    HAVE_TRAY = True
except ImportError:
    HAVE_TRAY = False

CONFIG_PATH = Path.home() / ".tv_input_switcher.json"
CLI_TIMEOUT = 45  # seconds

# Capabilities Samsung TVs use for input selection (standard one first).
INPUT_CAPS = ("mediaInputSource", "samsungvd.mediaInputSource")

FRIENDLY_NAMES = {
    "digitaltv": "TV",
    "dtv": "TV",
    "hdmi1": "HDMI 1",
    "hdmi2": "HDMI 2",
    "hdmi3": "HDMI 3",
    "hdmi4": "HDMI 4",
    "usb": "USB",
    "am": "AM",
    "fm": "FM",
}


# ---------------------------------------------------------------- CLI helpers
class CLIError(Exception):
    pass


def find_cli():
    return shutil.which("smartthings")


def run_cli(*args, want_json=False):
    cli = find_cli()
    if not cli:
        raise CLIError(
            "The SmartThings CLI ('smartthings') was not found on your PATH.\n"
            "Install it from github.com/SmartThingsCommunity/smartthings-cli."
        )
    cmd = [cli, *args]
    if want_json:
        cmd.append("--json")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=CLI_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        raise CLIError(
            "The SmartThings CLI timed out. If this is your first run, open a "
            "terminal and run `smartthings devices` to log in."
        )
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        raise CLIError(msg or f"smartthings exited with code {proc.returncode}")
    if want_json:
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise CLIError(f"Could not parse CLI output:\n{proc.stdout[:500]}")
    return proc.stdout


def list_tvs():
    """Return [(device_id, label)] for devices that support input switching."""
    devices = run_cli("devices", want_json=True)
    tvs = []
    for d in devices:
        caps = {
            c.get("id")
            for comp in d.get("components", [])
            for c in comp.get("capabilities", [])
        }
        if caps & set(INPUT_CAPS):
            label = d.get("label") or d.get("name") or d["deviceId"]
            tvs.append((d["deviceId"], label))
    return tvs


def get_tv_state(device_id):
    """Return dict: power, capability, current input, list of (id, name)."""
    status = run_cli("devices:status", device_id, want_json=True)
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

    raise CLIError(
        "This device didn't report any input sources. Make sure the TV is on "
        "and try Refresh."
    )


def send_command(device_id, command):
    # Format: [component:]capability:command(args)
    run_cli("devices:commands", device_id, command)


# ---------------------------------------------------------------- config
def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


def save_config(cfg):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    except OSError:
        pass


def make_tray_image(size=64):
    """Draw a simple TV icon so no image file is needed."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64
    # screen
    d.rounded_rectangle([4 * s, 8 * s, 60 * s, 46 * s], radius=5 * s,
                        fill=(30, 30, 30, 255), outline=(200, 200, 200, 255),
                        width=max(1, int(3 * s)))
    d.rectangle([10 * s, 14 * s, 54 * s, 40 * s], fill=(40, 120, 220, 255))
    # stand
    d.rectangle([28 * s, 46 * s, 36 * s, 53 * s], fill=(200, 200, 200, 255))
    d.rectangle([18 * s, 53 * s, 46 * s, 58 * s], fill=(200, 200, 200, 255))
    return img


# ---------------------------------------------------------------- GUI
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("TV Input Switcher")
        self.minsize(320, 200)
        self.resizable(True, False)

        self.cfg = load_config()
        self.tvs = []  # [(id, label)]
        self.state_info = None
        self.busy = False

        pad = {"padx": 10, "pady": 6}

        # TV picker
        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="TV:").pack(side="left")
        self.tv_var = tk.StringVar()
        self.tv_combo = ttk.Combobox(top, textvariable=self.tv_var, state="readonly")
        self.tv_combo.pack(side="left", fill="x", expand=True, padx=(6, 6))
        self.tv_combo.bind("<<ComboboxSelected>>", self.on_tv_selected)
        self.refresh_btn = ttk.Button(top, text="Refresh", command=self.refresh_state)
        self.refresh_btn.pack(side="left")

        # Power buttons
        power = ttk.Frame(self)
        power.pack(fill="x", **pad)
        self.on_btn = ttk.Button(power, text="Power On", command=lambda: self.power("on"))
        self.off_btn = ttk.Button(power, text="Power Off", command=lambda: self.power("off"))
        self.on_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self.off_btn.pack(side="left", expand=True, fill="x", padx=(4, 0))

        # Input buttons
        ttk.Label(self, text="Inputs", font=("TkDefaultFont", 10, "bold")).pack(
            anchor="w", padx=10
        )
        self.inputs_frame = ttk.Frame(self)
        self.inputs_frame.pack(fill="x", **pad)

        # Status bar
        self.status_var = tk.StringVar(value="Starting…")
        ttk.Label(self, textvariable=self.status_var, foreground="#555").pack(
            anchor="w", padx=10, pady=(0, 10)
        )

        # System tray
        self.tray = None
        if HAVE_TRAY:
            self.setup_tray()
            # Closing the window hides it to the tray instead of quitting.
            self.protocol("WM_DELETE_WINDOW", self.withdraw)
        else:
            self.protocol("WM_DELETE_WINDOW", self.quit_app)

        if not find_cli():
            self.status_var.set("SmartThings CLI not found on PATH.")
            messagebox.showerror("SmartThings CLI missing", str(CLIError(
                "The SmartThings CLI ('smartthings') was not found on your PATH.\n"
                "Install it from github.com/SmartThingsCommunity/smartthings-cli, "
                "then run `smartthings devices` once to log in."
            )))
        else:
            self.load_tvs()

    # ---- system tray
    def setup_tray(self):
        def ui(fn):
            # pystray calls menu handlers on its own thread; hop to Tk's thread.
            return lambda icon, item: self.after(0, fn)

        def device_items():
            if not self.tvs:
                yield pystray.MenuItem("(no TVs found yet)", None, enabled=False)
                return
            for idx, (dev_id, label) in enumerate(self.tvs):
                yield pystray.MenuItem(
                    label,
                    ui(lambda i=idx: self.select_tv(i)),
                    checked=lambda item, d=dev_id: self.current_device() == d,
                    radio=True,
                )

        def input_items():
            info = self.state_info
            if not info:
                yield pystray.MenuItem("(refresh to load inputs)", None, enabled=False)
                return
            for src_id, name in info["inputs"]:
                yield pystray.MenuItem(
                    name,
                    ui(lambda s=src_id: self.set_input(s)),
                    checked=lambda item, s=src_id: (
                        self.state_info is not None
                        and self.state_info.get("current") == s
                    ),
                    radio=True,
                )

        menu = pystray.Menu(
            pystray.MenuItem("Show window", ui(self.show_window), default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Device", pystray.Menu(device_items)),
            pystray.MenuItem("Input", pystray.Menu(input_items)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Power On", ui(lambda: self.power("on"))),
            pystray.MenuItem("Power Off", ui(lambda: self.power("off"))),
            pystray.MenuItem("Refresh", ui(self.refresh_state)),
            pystray.MenuItem("Reload device list", ui(self.load_tvs)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", ui(self.quit_app)),
        )
        self.tray = pystray.Icon(
            "tv_input_switcher", make_tray_image(), "TV Input Switcher", menu
        )
        self.tray.run_detached()

    def update_tray(self):
        if self.tray:
            try:
                self.tray.update_menu()
            except Exception:
                pass

    def show_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def select_tv(self, idx):
        if self.busy or idx == self.tv_combo.current():
            return
        self.tv_combo.current(idx)
        self.state_info = None
        self.on_tv_selected()

    def quit_app(self):
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.destroy()

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
            messagebox.showerror("SmartThings", str(err))
        else:
            done(result)
        self.update_tray()

    def set_controls_enabled(self, enabled):
        st = "normal" if enabled else "disabled"
        for w in (self.refresh_btn, self.on_btn, self.off_btn):
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
            if not tvs:
                self.status_var.set("No TVs with input control found.")
                return
            self.tv_combo["values"] = [label for _, label in tvs]
            saved = self.cfg.get("device_id")
            ids = [i for i, _ in tvs]
            self.tv_combo.current(ids.index(saved) if saved in ids else 0)
            self.on_tv_selected()

        self.run_bg("Looking for TVs…", list_tvs, done)

    def on_tv_selected(self, _event=None):
        dev = self.current_device()
        if dev:
            self.cfg["device_id"] = dev
            save_config(self.cfg)
            self.refresh_state()

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

        self.run_bg("Reading TV status…", lambda: get_tv_state(dev), done)

    def build_input_buttons(self):
        for w in self.inputs_frame.winfo_children():
            w.destroy()
        info = self.state_info
        cols = 3
        for i, (src_id, name) in enumerate(info["inputs"]):
            text = f"● {name}" if src_id == info["current"] else name
            b = ttk.Button(
                self.inputs_frame, text=text,
                command=lambda s=src_id: self.set_input(s),
            )
            b.grid(row=i // cols, column=i % cols, sticky="ew", padx=3, pady=3)
        for c in range(cols):
            self.inputs_frame.columnconfigure(c, weight=1)

    def set_input(self, src_id):
        dev = self.current_device()
        if not dev or not self.state_info:
            return
        cap = self.state_info["cap"]
        cmd = f"main:{cap}:setInputSource({json.dumps(src_id)})"

        def done(_):
            self.state_info["current"] = src_id
            self.build_input_buttons()
            name = dict(self.state_info["inputs"]).get(src_id, src_id)
            self.status_var.set(f"Switched to {name}")

        self.run_bg(f"Switching to {src_id}…", lambda: send_command(dev, cmd), done)

    def power(self, state):
        dev = self.current_device()
        if not dev:
            return

        def done(_):
            self.status_var.set(f"Power {state} sent")
            if state == "on":
                # Give the TV a moment to wake before re-reading inputs.
                self.after(4000, self.refresh_state)

        self.run_bg(f"Turning TV {state}…",
                    lambda: send_command(dev, f"main:switch:{state}"), done)


if __name__ == "__main__":
    App().mainloop()
