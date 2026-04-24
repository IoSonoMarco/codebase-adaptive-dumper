"""
import_resolver.py
Builds a symbol-resolution table for the internal library.
Answers: "from lib.models import compute" → which actual module and symbol?

Handles all 5 import patterns:
  A. from lib.models.comp import compute
  B. from lib.models import compute          (resolves __init__.py re-exports)
  C. from lib.module import (foo1, foo2)     (split and resolve each)
  D. import lib.models.comp                 (module-level)
  E. import lib.models.comp as comp         (alias)
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Set, Tuple

from .module_indexer import ImportInfo, ModuleInfo

logger = logging.getLogger(__name__)

# Type aliases
ModuleMap = Dict[str, ModuleInfo]
# (module_name, local_name) → (actual_module, actual_symbol_name)
# actual_symbol_name=None means the whole module is imported
SymbolTable = Dict[Tuple[str, str], Tuple[str, Optional[str]]]


class ImportResolver:
    """
    Builds a flat symbol resolution table and provides lookup helpers.

    For each module that imports from lib, the table maps:
        (importing_module_name, local_name) → (defining_module, symbol_name)
    """

    def __init__(self, modules: ModuleMap, path_resolver):
        self._modules = modules
        self._resolver = path_resolver
        # The main symbol table
        self.symbol_table: SymbolTable = {}
        # Warnings accumulated during resolution
        self.warnings: List[str] = []
        self._build()

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def _build(self) -> None:
        for mod_name, mod_info in self._modules.items():
            for imp in mod_info.imports:
                if not imp.is_internal:
                    continue
                self._resolve_import(mod_name, imp)

    def _resolve_import(self, importing_module: str, imp: ImportInfo) -> None:
        if imp.is_from:
            # from lib.x import name1, name2
            for orig_name, alias in imp.names:
                local_name = alias or orig_name
                if orig_name == "*":
                    # Wildcard — conservative: note the warning, skip table entry
                    warn = f"{importing_module}: wildcard import from {imp.module}"
                    self.warnings.append(warn)
                    logger.warning(warn)
                    continue
                resolved = self._resolve_from_import(imp.module, orig_name)
                if resolved:
                    self.symbol_table[(importing_module, local_name)] = resolved
                else:
                    warn = f"{importing_module}: cannot resolve '{orig_name}' from '{imp.module}'"
                    self.warnings.append(warn)
                    logger.debug(warn)
        else:
            # import lib.x  /  import lib.x as alias
            module_path = imp.module
            if imp.names:
                _, alias = imp.names[0]
                local_name = alias if alias else module_path.split(".")[0]
            else:
                local_name = module_path.split(".")[0]

            if imp.names and imp.names[0][1]:
                # import lib.x as alias → alias maps to entire module
                self.symbol_table[(importing_module, local_name)] = (module_path, None)
            else:
                # import lib.x  → 'lib' maps to top-level (keep module_path for attr resolution)
                self.symbol_table[(importing_module, local_name)] = (module_path, None)

    # ------------------------------------------------------------------
    # Resolution helpers
    # ------------------------------------------------------------------

    def _resolve_from_import(
        self, from_module: str, symbol_name: str, _depth: int = 0
    ) -> Optional[Tuple[str, Optional[str]]]:
        """
        Resolve (from_module, symbol_name) → (actual_module, actual_symbol).
        Follows __init__.py re-export chains (up to depth 10).
        """
        if _depth > 10:
            logger.warning("Re-export chain depth exceeded for %s:%s", from_module, symbol_name)
            return None

        # Case A: the module is a concrete .py file → check if symbol is defined there
        mod_info = self._modules.get(from_module)
        if mod_info:
            if symbol_name in mod_info.symbols or symbol_name in mod_info.top_level_assigns:
                return (from_module, symbol_name)
            # Symbol not directly defined → might be re-exported via an import inside
            # that module (common pattern in __init__.py)
            reexport = self._find_reexport(mod_info, symbol_name, _depth)
            if reexport:
                return reexport

        # Maybe from_module is a package (has __init__.py)
        # Try via sub-module search: does any direct child define it?
        pkg_init_name = from_module  # package __init__ has same module name
        init_mod = self._modules.get(pkg_init_name)
        if init_mod and init_mod.is_init:
            reexport = self._find_reexport(init_mod, symbol_name, _depth)
            if reexport:
                return reexport

        # Last resort: scan all modules under from_module for the symbol
        prefix = from_module + "."
        for mod_name, mod in self._modules.items():
            if mod_name.startswith(prefix) and not mod.is_init:
                if symbol_name in mod.symbols or symbol_name in mod.top_level_assigns:
                    logger.debug("Resolved %s:%s via scan → %s", from_module, symbol_name, mod_name)
                    return (mod_name, symbol_name)

        return None

    def _find_reexport(
        self, mod_info: ModuleInfo, symbol_name: str, _depth: int
    ) -> Optional[Tuple[str, Optional[str]]]:
        """
        Look through mod_info's imports for a re-export of symbol_name.
        """
        for imp in mod_info.imports:
            if not imp.is_internal:
                continue
            if imp.is_from:
                for orig, alias in imp.names:
                    local = alias or orig
                    if local == symbol_name:
                        # Found the re-export: recurse to find ultimate source
                        result = self._resolve_from_import(imp.module, orig, _depth + 1)
                        if result:
                            return result
                        # Fallback: claim it lives in imp.module
                        return (imp.module, orig)
        return None

    # ------------------------------------------------------------------
    # Public lookup API
    # ------------------------------------------------------------------

    def resolve(
        self, importing_module: str, local_name: str
    ) -> Optional[Tuple[str, Optional[str]]]:
        """
        Look up (module, local_name) → (actual_module, actual_symbol).
        Returns None if not an internal library reference.
        """
        return self.symbol_table.get((importing_module, local_name))

    def resolve_attr_access(
        self, importing_module: str, attr_chain: str
    ) -> Optional[Tuple[str, Optional[str]]]:
        """
        Resolve attribute accesses like 'comp.compute' or 'lib.models.comp.compute'
        by checking if the root name is a known module alias.

        Returns (actual_module, symbol_name) or None.
        """
        parts = attr_chain.split(".")
        if len(parts) < 2:
            return None

        root = parts[0]
        # Check if root is a module alias in this importing module
        entry = self.symbol_table.get((importing_module, root))
        if entry:
            actual_module, _ = entry
            # The remaining parts form a sub-path
            rest = parts[1:]
            if len(rest) == 1:
                # comp.compute  → symbol 'compute' in actual_module
                return (actual_module, rest[0])
            else:
                # lib.models.comp.compute  → sub-module + symbol
                sub_module = actual_module + "." + ".".join(rest[:-1])
                symbol = rest[-1]
                return (sub_module, symbol)

        # Maybe the chain IS a dotted module path: lib.models.comp.compute
        # Try progressively longer prefixes as module names
        for split_at in range(1, len(parts)):
            candidate_module = ".".join(parts[:split_at])
            symbol = parts[split_at]
            if candidate_module in self._modules:
                # Confirm symbol exists there
                mod = self._modules[candidate_module]
                if symbol in mod.symbols or symbol in mod.top_level_assigns:
                    return (candidate_module, symbol)

        return None

    def get_internal_name_map(
        self, module_name: str
    ) -> Dict[str, Tuple[str, Optional[str]]]:
        """
        Return {local_name → (actual_module, actual_symbol)} for all internal
        imports in the given module.  Used by reachability to expand dependencies.
        """
        result: Dict[str, Tuple[str, Optional[str]]] = {}
        for (mod, local), target in self.symbol_table.items():
            if mod == module_name:
                result[local] = target
        return result
