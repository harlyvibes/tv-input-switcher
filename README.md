# TV Input Switcher

A small Tkinter GUI for switching inputs on a Samsung SmartThings TV (e.g. Q90R / Q90T), with an optional system tray icon.

## Requirements

- Python 3.8+ (Tkinter ships with the standard Windows/macOS installers)
- [SmartThings CLI](https://github.com/SmartThingsCommunity/smartthings-cli) on your PATH — run `smartthings devices` once first to log in
- For the tray icon: `pip install pystray pillow` (optional — the app still works without it)

## Usage

```bash
python tv_input_switcher.py
```

Right-click the tray icon to pick a TV, switch inputs, or power on/off. Closing the window hides it to the tray; use "Quit" in the tray menu to exit.

## License

MIT
