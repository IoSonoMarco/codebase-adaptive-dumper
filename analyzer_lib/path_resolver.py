"""
path_resolver.py
Normalizes filesystem paths into canonical module names and vice-versa.
Identifies the repository root as the parent of lib/.
"""
from pathlib import Path
from typing import Optional, List
import logging

logger = logging.getLogger(__name__)


class PathResolver:
    """Handles all path ↔ module-name conversions for the project."""

    def __init__(self, target_script_path: str, internal_library_path: str):
        self.target_script = Path(target_script_path).resolve()
        if not self.target_script.exists():
            raise FileNotFoundError(f"Target script not found: {self.target_script}")

        lib_input = Path(internal_library_path).resolve()
        self.lib_dir = self._find_lib_dir(lib_input)
        self.repo_root = self.lib_dir.parent

        logger.info("Repo root   : %s", self.repo_root)
        logger.info("Lib dir     : %s", self.lib_dir)
        logger.info("Target      : %s", self.target_script)

    # ------------------------------------------------------------------
    # Lib directory discovery
    # ------------------------------------------------------------------

    def _find_lib_dir(self, base: Path) -> Path:
        """Accept either '.../lib' or the parent '.../project' containing lib/."""
        if base.name == "lib" and base.is_dir():
            return base
        candidate = base / "lib"
        if candidate.is_dir():
            return candidate
        raise FileNotFoundError(
            f"Cannot find a 'lib/' directory at '{base}' or '{base / 'lib'}'. "
            "Pass either the path TO lib/ or its parent directory."
        )

    # ------------------------------------------------------------------
    # Path → module name
    # ------------------------------------------------------------------

    def path_to_module_name(self, file_path: Path) -> str:
        """
        /repo/lib/models/comp.py      → 'lib.models.comp'
        /repo/lib/models/__init__.py  → 'lib.models'
        """
        rel = file_path.resolve().relative_to(self.repo_root)
        parts = list(rel.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts)

    # ------------------------------------------------------------------
    # Module name → path
    # ------------------------------------------------------------------

    def module_to_path(self, module_name: str) -> Optional[Path]:
        """Convert 'lib.models.comp' to its .py file, if it exists."""
        parts = module_name.split(".")
        # Try package first  (lib/models/__init__.py)
        pkg = self.repo_root.joinpath(*parts) / "__init__.py"
        if pkg.exists():
            return pkg
        # Try module file  (lib/models/comp.py)
        if len(parts) >= 2:
            mod = self.repo_root.joinpath(*parts[:-1]) / (parts[-1] + ".py")
        else:
            mod = self.repo_root / (parts[0] + ".py")
        if mod.exists():
            return mod
        return None

    # ------------------------------------------------------------------
    # Dump-format paths
    # ------------------------------------------------------------------

    def relative_dump_path(self, file_path: Path) -> str:
        """Return a Unix-style path relative to repo root, e.g. /lib/models/utils.py."""
        try:
            rel = file_path.resolve().relative_to(self.repo_root)
        except ValueError:
            rel = Path(file_path.name)
        return "/" + str(rel).replace("\\", "/")

    def target_dump_path(self) -> str:
        try:
            rel = self.target_script.relative_to(self.repo_root)
            return "/" + str(rel).replace("\\", "/")
        except ValueError:
            return "/" + self.target_script.name

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_lib_modules(self) -> List[Path]:
        """Recursively enumerate all .py files under lib/, sorted."""
        return sorted(self.lib_dir.rglob("*.py"))
