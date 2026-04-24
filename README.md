# codebase_dumper

A PowerShell + Python tool that produces a **minimal runnable `dump.txt`**
containing only the code a given target script actually needs from your
internal `lib/` package.

Unused functions, classes, constants, and `__init__.py` re-exports are
aggressively removed. The resulting dump is sufficient for a new person (or
LLM) to reconstruct and run the target script.

---

## Quick start

### PowerShell (recommended for non-Python users)

```powershell
.\run_analyzer.ps1 `
    -TargetScriptPath  .\scripts\main.py `
    -InternalLibraryPath .\my_project `
    -OutputPath        .\dump.txt `
    -ReportPath        .\dump_report.json
```

Add `-Verbose` for debug-level logging from the Python layer.  
Use `-PythonExe python3` (or a full path) when `python` is not on your PATH.

### Python directly

```bash
python analyzer.py \
    --target   scripts/main.py \
    --lib-root /path/to/my_project \
    --output   dump.txt \
    --report   dump_report.json \
    --verbose
```

---

## Inputs

| Parameter | Description |
|-----------|-------------|
| `--target` / `-TargetScriptPath` | The main `.py` script to analyze |
| `--lib-root` / `-InternalLibraryPath` | Folder containing `lib/`, or `lib/` itself |
| `--output` / `-OutputPath` | Where to write `dump.txt` |
| `--report` / `-ReportPath` | *(optional)* JSON diagnostics file |

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
run_analyzer.ps1          PowerShell wrapper (validates paths, calls Python)
analyzer.py               CLI entry point (orchestrates all phases)
analyzer_lib/
  path_resolver.py        Path ↔ module-name normalization
  module_indexer.py       AST parsing and symbol indexing
  import_resolver.py      Symbol table + import resolution (A–E + re-export chains)
  reachability.py         BFS reachability from target script
  reconstructor.py        Minimal source reconstruction via line slicing
  dump_writer.py          Writes dump.txt + dump_report.json
tests/
  test_analyzer.py        12 required test cases + integration tests
```

**Key design principle:** reachability is *symbol-driven, not file-driven*.
A module is included only because a required symbol lives there.

---

## Running the tests

```bash
# With pytest
python -m pytest tests/ -v

# Without pytest
python tests/test_analyzer.py
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
