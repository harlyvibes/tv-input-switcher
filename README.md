# TV Input Switcher

A small desktop app for switching inputs on a Samsung SmartThings TV (built for the Q90R / Q90T) using the [SmartThings REST API](https://developer.smartthings.com/docs/api/public). It opens a window with one button per input and adds an icon to the system tray (Windows/Linux) or menu bar (macOS), so you can pick a TV, switch inputs, or power it on and off without opening the window.

Works on Windows, macOS and Linux, using only the Python standard library (plus two optional packages for the tray icon). It works from anywhere with internet access, not just your home network.

## Requirements

- Python 3.8 or newer with Tkinter (included with the python.org installers). **On macOS, do not use the system Python at `/usr/bin/python3`** — it bundles an old Tcl/Tk with serious display bugs on modern macOS (windows can render completely blank or refuse to resize, especially in Dark Mode). Install Python from [python.org/downloads/macos](https://www.python.org/downloads/macos/) (or `brew install python-tk`) and run the script with that `python3` instead.
- A SmartThings account with your TV added to it
- Optional, for the tray / menu bar icon: `pystray` and `pillow`

```sh
pip install -r requirements.txt   # optional: tray / menu bar icon
python tv_input_switcher.py       # macOS: python3 tv_input_switcher.py
```

## Signing in

The first time you run the app, it opens **Account…**. Choose one of two ways to sign in.

### Option 1: Personal access token (quickest)

1. Go to [account.smartthings.com/tokens](https://account.smartthings.com/tokens) and create a token. Under **Devices**, tick *List all devices*, *See all devices* and *Control all devices*.
2. Paste the token into the app and click **Save**.

SmartThings tokens created after 30 December 2024 expire after 24 hours, so you'll need to paste a new one each day. The app tells you when it has expired. For a sign-in that lasts, use Option 2.

### Option 2: OAuth app (renews itself)

This needs a one-time setup with the SmartThings CLI to register your own OAuth app. After that the app refreshes its own sign-in, and you won't need the CLI again.

1. Install the [SmartThings CLI](https://github.com/SmartThingsCommunity/smartthings-cli) and run:
   ```sh
   smartthings apps:create
   ```
2. Choose **OAuth-In App**. Give it any display name, and when asked:
   - **Scopes:** `r:devices:*` and `x:devices:*`
   - **Redirect URI:** `https://httpbin.org/get`
3. Copy the **OAuth Client ID** and **Client Secret** it prints. The secret is shown only once.
4. In the app, open **Account…**, choose **OAuth app**, enter the client ID and secret, and click **Sign in…**.
5. Sign in to Samsung in the browser and approve access. You'll land on an httpbin page whose address contains `?code=…`. Copy the whole address, paste it into the app, and click **Save**.

The sign-in lasts as long as the app is used at least once every 30 days. While it's running in the tray it keeps the sign-in fresh on its own.

If you'd rather not paste the code, register the redirect URI as `http://localhost:53682/callback` instead and enter the same address in the app. The app then catches the sign-in itself. Some SmartThings accounts only accept https redirect URIs; if yours rejects it, use the httpbin one.

## Using it

The app lists every device on your SmartThings account that supports input switching. Pick your TV, then click an input; the active one is marked with ●. Right-click the tray icon (or click the menu bar icon on macOS) for the same controls. Closing the window hides it to the tray; choose **Quit** from the icon's menu to exit. Your selected TV and sign-in are saved in `~/.tv_input_switcher.json`. That file contains your tokens, so keep it private.

The TV must be on to change inputs. For **Power On** to work, turn on **Power On with Mobile** (or IP Remote / Network Standby) in the TV's settings.

## How it works

The app calls the SmartThings API over HTTPS:

| Action | Request |
| --- | --- |
| Find TVs | `GET /v1/devices` (keeps devices with `mediaInputSource` or `samsungvd.mediaInputSource`) |
| Read inputs and state | `GET /v1/devices/{id}/status` |
| Switch input | `POST /v1/devices/{id}/commands` with `setInputSource` |
| Power on / off | `POST /v1/devices/{id}/commands` with `switch` `on` / `off` |
| OAuth sign-in and refresh | `POST /oauth/token` |

The tray icon runs in its own helper process, because macOS requires both Tkinter and the menu bar icon to own the main thread of their process. The window and tray exchange state and commands over a pair of queues.

## Troubleshooting

- **"SmartThings rejected the sign-in":** your personal access token has expired, or your OAuth sign-in lapsed. Open **Account…** and sign in again.
- **No TVs found:** make sure the TV appears in the SmartThings app on your phone under the same Samsung account.
- **Inputs missing or out of date:** SmartThings may not update while the TV is off. Turn it on and click **Refresh**.
- **Blank window on macOS, especially if it also won't resize:** you're almost certainly running macOS's built-in Python (`/usr/bin/python3`), which bundles Tk 8.5 — it predates Dark Mode and has serious, unfixable-from-inside-the-app display bugs on modern macOS. Check with `which python3`; if it prints `/usr/bin/python3`, install Python from [python.org/downloads/macos](https://www.python.org/downloads/macos/) and run the script with that `python3` instead. The app also prints a warning to the terminal on startup if it detects this.
