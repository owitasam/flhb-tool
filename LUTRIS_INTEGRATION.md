# flhb-tool — Lutris Integration & Packaging Guide (Phase 4)

This guide walks you through installing flhb-tool and wiring it into Lutris
as a custom executable / launch wrapper so it can manage Wine prefixes and
custom loaders for your games.

## 1. Install flhb-tool

Run the install script from the project root:

```bash
cd /path/to/flhb-tool   # e.g. /home/sam/Projects/modding
./install.sh
```

By default this:

- Copies the package to `~/.local/share/flhb-tool/`
- Installs a convenience wrapper at `~/.local/bin/flhb-bridge`
- Installs a Lutris-friendly wrapper at
  `~/.local/share/flhb-tool/flhb-lutris-wrapper.sh`
- Sets `chmod +x` on all executable files

Optional custom locations:

```bash
./install.sh --prefix /opt/flhb-tool --bindir /usr/local/bin
```

Ensure `~/.local/bin` is on your `PATH` if you want to call `flhb-bridge`
directly from a terminal.

## 2. Create a per-game config.json

Copy the example and edit it for your game:

```bash
cp config.json.example ~/Games/my-game/config.json
nano ~/Games/my-game/config.json
```

Example `config.json`:

```json
{
  "$schema": "flhb-tool bridge config v1",
  "wine_prefix_path": "/home/youruser/Games/my-game/pfx",
  "target_executable": "/home/youruser/Games/my-game/drive_c/Program Files/MyGame/game.exe",
  "loader_arguments": [],
  "environment_overrides": {
    "DXVK": "1",
    "VKD3D": "1",
    "WINEDEBUG": "-all",
    "WINEESYNC": "1",
    "WINEFSYNC": "1"
  },
  "working_directory": "/home/youruser/Games/my-game/drive_c/Program Files/MyGame",
  "wine_arch": "win64"
}
```

Key fields:

| Field | Purpose |
|---|---|
| `wine_prefix_path` | Absolute path to the Lutris Wine prefix for this game. |
| `target_executable` | Absolute path to the game or loader binary inside the prefix. |
| `loader_arguments` | CLI arguments passed to the loader/binary. |
| `environment_overrides` | Custom env vars (DXVK, VKD3D, Wine debug flags, esync/fsync). |
| `working_directory` | Defaults to the directory of `target_executable`. |
| `wine_arch` | `"win64"` or `"win32"`. |

## 3. Configure Lutris

There are two supported integration patterns.

### Option A — Script runner (recommended)

Lutris' "Script" runner lets you point the executable directly at a shell
script, which is ideal for the flhb wrapper.

1. Open Lutris → right-click your game → **Configure**.
2. **Runner:** change to `Script`.
3. **Game options → Executable:** browse to and select
   `~/.local/share/flhb-tool/flhb-lutris-wrapper.sh`
4. **Game options → Working directory:** set to your game folder
   (e.g. `/home/youruser/Games/my-game`).
5. **System options → Environment variables:** add:
   - `FLHB_CONFIG` = `/home/youruser/Games/my-game/config.json`
   - `FLHB_LOG_FILE` = `/home/youruser/Games/my-game/flhb_debug.log`
   - `FLHB_AUTO_FIX` = `0` (set to `1` to attempt automated remediation)
6. **Save**.

### Option B — Custom executable / pre-launch command

If you prefer to keep the Wine runner and only inject flhb as a wrapper:

1. Open Lutris → right-click your game → **Configure**.
2. **Runner:** keep `Wine`.
3. **Game options → Executable:** your game's main binary (as usual).
4. **Game options → Wine prefix:** your Lutris prefix path (as usual).
5. **Game options → Pre-launch command:**
   ```
   /home/youruser/.local/share/flhb-tool/flhb-lutris-wrapper.sh /home/youruser/Games/my-game/config.json
   ```
   Or, if you only want pre-flight checks without blocking launch, use the
   standalone check:
   ```
   python3 -m flhb_tool --log-file /home/youruser/Games/my-game/flhb_debug.log
   ```
6. **Save**.

### Option C — Direct `flhb-bridge` wrapper

If `~/.local/bin` is on your PATH, you can point Lutris' executable directly
at the wrapper:

```
flhb-bridge /home/youruser/Games/my-game/config.json
```

## 4. Verify it works

Launch the game from Lutris. You should see:

- Phase 1 pre-flight checks run (logged to `FLHB_LOG_FILE`).
- The target executable launch through `GameBridge`.
- Real-time stdout/stderr captured in `flhb_debug.log`.
- On a crash or known error signature (missing DLL, C++ runtime, Vulkan
  init failure, access violation), a structured **crash dump** is appended
  to `flhb_debug.log` and a summary is printed to Lutris' output.

Check the debug log:

```bash
tail -n 50 ~/Games/my-game/flhb_debug.log
```

## 5. Directory layout after install

```
~/.local/share/flhb-tool/
├── bridge.py              # Phase 2 core orchestration bridge
├── config.py              # config.json schema & loader
├── log_monitor.py         # Phase 3 log monitor + error dictionary
├── system_check.py        # Phase 1 dependency validation
├── __init__.py            # Package API
├── __main__.py            # `python -m flhb_tool` entry point
├── flhb-lutris-wrapper.sh # Lutris launch wrapper (Phase 4)
├── config.json.example    # Example config
└── README.md
```

## 6. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `bridge.py: command not found` | Wrapper not on PATH or not executable | Use the full path to `flhb-lutris-wrapper.sh`, or run `chmod +x` |
| `config not found` | `FLHB_CONFIG` points to a missing file | Verify the path in Lutris environment variables |
| Pre-flight check fails | Missing Fedora package (wine-core, vulkan-loader, etc.) | Run the suggested `sudo dnf install ...` command from the log |
| Crash dump written | Game hit a known error signature | Review `flhb_debug.log`; follow the fix instructions listed |
| Buffer deadlock / no output | Stream reading issue | This is what Phase 2's reader threads prevent; ensure you're using `bridge.py`, not a raw `wine` call |

## 7. Uninstall

```bash
rm -rf ~/.local/share/flhb-tool
rm -f ~/.local/bin/flhb-bridge
```
