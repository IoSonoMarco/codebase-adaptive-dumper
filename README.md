# Codebase Adaptive Dumper

A PowerShell/Bash + Python tool that produces a **minimal runnable `dump.txt`**
containing only the code a given target script actually needs from your
internal `lib/` package.

Unused functions, classes, constants, and `__init__.py` re-exports are
aggressively removed. The resulting dump is sufficient for a new person, **or
an LLM**, to reconstruct and run the target script.

---

## Tutorial: first-time setup and usage

This walkthrough installs the tool once as a global command so you can call it from any project directory.

### Step 1 — Clone and place the repo

Create a permanent home for your personal tools, then clone the repo there:

```bash
# Linux / macOS
mkdir -p ~/my_tools
cd ~/my_tools
git clone https://github.com/your-username/codebase-adaptive-dumper
```

```powershell
# Windows PowerShell
New-Item -ItemType Directory -Path "$HOME\my_tools" -Force
cd "$HOME\my_tools"
git clone https://github.com/your-username/codebase-adaptive-dumper
```

The only files and folders the tool needs to function are:

```
my_tools/
└── codebase-adaptive-dumper/
    ├── analyzer_lib/        ← required
    ├── analyzer.py          ← required
    ├── run_analyzer.sh      ← required on Linux / macOS
    └── run_analyzer.ps1     ← required on Windows
```

Everything else (README, tests, etc.) can be deleted to keep it lean.

### Step 2 — Add the folder to your PATH

**Linux / macOS** — add this line to your `~/.bashrc`, `~/.zshrc`, or equivalent:

```bash
export PATH="$HOME/my_tools/codebase-adaptive-dumper:$PATH"
```

Then reload your shell:

```bash
source ~/.bashrc   # or source ~/.zshrc
```

**Windows** — run once in PowerShell (persists across sessions):

```powershell
$toolDir = "$HOME\my_tools\codebase-adaptive-dumper"
[System.Environment]::SetEnvironmentVariable(
    "PATH",
    "$toolDir;" + [System.Environment]::GetEnvironmentVariable("PATH", "User"),
    "User"
)
```

### Step 3 — Enable script execution

**Linux / macOS** — make the script executable (only needed once):

```bash
chmod +x ~/my_tools/codebase-adaptive-dumper/run_analyzer.sh
```

**Windows** — allow local scripts to run (only needed once, per user):

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

### Step 4 — Run it from your project

Open a new terminal and `cd` into any Python project. The typical layout the tool expects is:

```
my_project/
├── lib/
│   ├── __init__.py
│   ├── models/
│   │   ├── __init__.py
│   │   └── comp.py
│   └── utils.py
└── scripts/
    └── main.py          ← your target script
```

From the project root, run the analyzer:

```bash
# Linux / macOS
run_analyzer.sh \
    --target   scripts/main.py \
    --lib-root . \  # or ./lib
    --output   dump.txt \
    --diagnostics ./diag \
    --verbose
```

```powershell
# Windows PowerShell
run_analyzer.ps1 `
    -TargetScriptPath    scripts\main.py `
    -InternalLibraryPath . `  # or .\lib
    -OutputPath          dump.txt `
    -DiagnosticsDir      .\diag `
    -Verbose
```

When it completes you will find `dump.txt` in the project root and, if you passed `--diagnostics` / `-DiagnosticsDir`, a `diag/` folder containing `dump_report.json`, `code_tree_full.txt`, and `code_tree_pruned.txt`.

---

## Quick start

### Bash — Linux / macOS

```bash
./run_analyzer.sh \
    --target   scripts/main.py \
    --lib-root ./my_project \
    --output   dump.txt \
    --diagnostics ./diag
```

Add `--verbose` for debug-level logging.  
Use `--python python3` (or a full path) when the default interpreter is not on your PATH.  
Make the script executable once with `chmod +x run_analyzer.sh`.

### PowerShell — Windows

```powershell
.\run_analyzer.ps1 `
    -TargetScriptPath  .\scripts\main.py `
    -InternalLibraryPath .\my_project `
    -OutputPath        .\dump.txt `
    -DiagnosticsDir    .\diag
```

Add `-Verbose` for debug-level logging from the Python layer.  
Use `-PythonExe python3` (or a full path) when `python` is not on your PATH.

### Python directly

```bash
python analyzer.py \
    --target   scripts/main.py \
    --lib-root /path/to/my_project \
    --output   dump.txt \
    --diagnostics ./diag \
    --verbose
```

---

## Inputs

| Parameter | Description |
|-----------|-------------|
| `--target` / `-TargetScriptPath` | The main `.py` script to analyze |
| `--lib-root` / `-InternalLibraryPath` | Folder containing `lib/`, or `lib/` itself |
| `--output` / `-OutputPath` | Where to write `dump.txt` |
| `--diagnostics` / `-DiagnosticsDir` | *(optional)* Directory for `dump_report.json` and code trees |
| `--python` / `-PythonExe` | Python interpreter to use (default: `python3` on bash, `python` on PS1) |
| `--verbose` / `-Verbose` | Enable debug-level logging |

---

## Output format

`dump.txt` uses tagged blocks:

```
<<<PY_MODULE_START path="/lib/models/utils.py" lines=4>>>
import numpy as np

def normalize(x):
    return (x - x.mean()) / (x.std() + 1e-8)
<<<PY_MODULE_END>>>

<<<PY_MODULE_START path="/scripts/main.py" lines=6>>>
...target script (always full)...
<<<PY_MODULE_END>>>
```

Modules are emitted in dependency order (leaves first, target script last).

---

## What gets pruned

| Item | Rule |
|------|------|
| Unused top-level `def` / `class` | Removed |
| Unused `__init__.py` re-exports | Removed — only needed names kept |
| Unused top-level constants | Removed unless referenced by kept code |
| External imports (`numpy`, `torch`, …) | Kept only if used by retained code |
| Internal imports to removed symbols | Removed |

## What is always kept

| Item | Rule |
|------|------|
| Target script | Always emitted in full |
| Decorators of kept symbols | Always followed and included |
| Base classes of kept classes | Always included |
| Transitive helpers called by kept code | Recursively included |

---

## Supported import patterns

```python
from lib.models.comp import compute          # A: direct
from lib.models import compute               # B: via __init__.py
from lib.module import (foo1, foo2)          # C: multi-import (each resolved)
import lib.models.comp                       # D: bare module import
import lib.models.comp as comp; comp.fn()    # E: module alias
from lib.models.comp import compute as c     # Alias rename
```

---

## Diagnostics (`dump_report.json`)

```json
{
  "target_script": "/scripts/main.py",
  "reachable_modules": ["lib.models.comp", "lib.utils"],
  "reachable_symbols": { "lib.models.comp": ["compute"] },
  "dropped_symbols":   { "lib.models.comp": ["help", "unused_fn"] },
  "unresolved_references": [],
  "warnings": {
    "wildcard_imports": [],
    "dynamic_imports":  []
  },
  "emission_order": ["lib.models.comp", "lib.utils", "lib.models", ...]
}
```

---

## Architecture

```
run_analyzer.sh           Bash wrapper — Linux / macOS (validates paths, calls Python)
run_analyzer.ps1          PowerShell wrapper — Windows (validates paths, calls Python)
analyzer.py               CLI entry point (orchestrates all phases)
analyzer_lib/
  path_resolver.py        Path ↔ module-name normalization
  module_indexer.py       AST parsing and symbol indexing
  import_resolver.py      Symbol table + import resolution (A–E + re-export chains)
  reachability.py         BFS reachability from target script
  reconstructor.py        Minimal source reconstruction via line slicing
  dump_writer.py          Writes dump.txt + diagnostics
  test_analyzer.py        12 required test cases + integration tests
```

**Key design principle:** reachability is *symbol-driven, not file-driven*.
A module is included only because a required symbol lives there.

---

## Running the tests

```bash
# With pytest
python -m pytest analyzer_lib/test_analyzer.py -v

# Without pytest
python analyzer_lib/test_analyzer.py
```

---

## Limitations and warnings

- **Dynamic imports** (`__import__`, `importlib`) are not resolved; a warning
  is emitted and the module is conservatively included.
- **Wildcard imports** (`from x import *`) trigger a warning and force
  whole-module inclusion.
- **`getattr(module, name)`** with dynamic strings cannot be resolved.
- Classes are always included in full (method-level pruning is out of scope
  for v1 as per spec §12).
- Runtime-evaluated annotations may require manual review.
