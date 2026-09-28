The app is a standard `Adw.Application` launched by `python -m vitrine`, so breakpoints can be placed anywhere in startup, activation, UI callbacks, or worker callbacks without changing application code. The only setup wrinkle is interpreter selection: I’m verifying the Nix-provided Python path and GTK import from the shell so I can distinguish the reliable “launch VS Code from Nix” route from a brittle hard-coded store path.

The best workflow is to launch VS Code from the Nix dev shell, then use the Python debugger normally.

**1. Start VS Code with Vitrine’s Nix environment**

From the repository root:

```sh
nix develop
code --reuse-window .
```

The shell provides GTK, libadwaita, WebKit, Python, gogdl, and the required environment variables via `flake.nix`.

**2. Select the Nix Python interpreter**

In VS Code:

1. Open `Ctrl+Shift+P`
2. Run **Python: Select Interpreter**
3. Choose the interpreter returned by:

```sh
nix develop -c which python
```

For this checkout it is currently a Nix store Python environment.

**3. Add a debug profile**

Create `.vscode/launch.json`:

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "Vitrine",
      "type": "debugpy",
      "request": "launch",
      "module": "vitrine",
      "cwd": "${workspaceFolder}",
      "console": "integratedTerminal",
      "justMyCode": true,
      "env": {
        "VITRINE_DEV": "1",
        "PYTHONUNBUFFERED": "1"
      }
    },
    {
      "name": "Vitrine Tests",
      "type": "debugpy",
      "request": "launch",
      "module": "pytest",
      "args": ["-q"],
      "cwd": "${workspaceFolder}",
      "console": "integratedTerminal",
      "justMyCode": true
    }
  ]
}
```

This launches the same module as `__main__.py`, which enters the GTK application defined in `application.py`.

Then use **Run and Debug → Vitrine**, place breakpoints, and inspect values in:

- **Variables**
- **Watch**
- **Debug Console**
- **Call Stack**

For example, in a UI breakpoint you can evaluate:

```python
self.library.games()
game.config
game.source
```

For GOG installs, useful breakpoints are `_install_gog_game`, `_finish_download`, and `_gog_finish_install` in `window.py`. The gogdl process is external, so VS Code cannot step inside it, but you can inspect its command and captured output through the download job.

For a headless environment, run the debugger under Xvfb:

```sh
nix develop -c sh -c 'Xvfb :99 -screen 0 1280x800x24 & DISPLAY=:99 GDK_BACKEND=x11 python -m debugpy --listen 5678 --wait-for-client -m vitrine'
```

Usually the desktop workflow is simpler: run VS Code from `nix develop`, select the Nix interpreter, and launch the debug profile.
