"""
reachability.py
Performs transitive-closure reachability from the target script.

Node types in the graph:
  (module_name, symbol_name)  — a specific symbol
  (module_name, None)         — the entire module (module-level import)

Starting from the root set (what the target script uses), the BFS adds:
  • internal functions/classes called inside reachable symbols
  • decorator dependencies
  • base class dependencies
  • top-level constants referenced by reachable symbols
  • module-level dependencies for bare 'import lib.x' patterns

Conservative rule: when a wildcard import or unresolved reference is detected,
the entire module is included.
"""
from __future__ import annotations

import logging
from collections import deque
from typing import Dict, Optional, Set, Tuple

from .module_indexer import ModuleInfo, SymbolInfo
from .import_resolver import ImportResolver

logger = logging.getLogger(__name__)

# (module_name, symbol_or_None)
ReachNode = Tuple[str, Optional[str]]


class ReachabilityAnalyzer:
    def __init__(
        self,
        modules: Dict[str, ModuleInfo],
        import_resolver: ImportResolver,
    ):
        self._modules = modules
        self._resolver = import_resolver

        # Results
        self.reachable: Set[ReachNode] = set()
        self.dropped_symbols: Dict[str, Set[str]] = {}   # module → {sym, …}
        self.unresolved: Set[str] = set()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def compute(self, target_info: ModuleInfo) -> None:
        """
        Run BFS starting from everything the target script imports from lib/.
        target_info is the indexed ModuleInfo for the target script.
        """
        seeds = self._extract_root_set(target_info)
        logger.info("Root set has %d seeds", len(seeds))
        for seed in seeds:
            logger.debug("  seed: %s", seed)

        queue: deque[ReachNode] = deque(seeds)
        while queue:
            node = queue.popleft()
            if node in self.reachable:
                continue
            self.reachable.add(node)
            logger.debug("Reached: %s", node)
            new_nodes = self._expand(node)
            for n in new_nodes:
                if n not in self.reachable:
                    queue.append(n)

        self._compute_dropped()

    # ------------------------------------------------------------------
    # Root-set extraction from target script
    # ------------------------------------------------------------------

    def _extract_root_set(self, target_info: ModuleInfo) -> Set[ReachNode]:
        """
        Build the root set from the target script.

        Key rule (from spec §6): the root set is the set of internal library
        symbols *actually used* by the target script — not just imported.

        Strategy:
          1. Scan the entire target AST (all non-import nodes) for Name refs
             and attribute chains.
          2. Map each referenced local name to its internal import resolution.
          3. Bare module imports (import lib.x / import lib.x as alias) that are
             referenced anywhere in the body are included as module-level deps.
        """
        import ast as _ast
        from .module_indexer import _NameRefVisitor

        roots: Set[ReachNode] = set()
        internal_name_map = self._resolver.get_internal_name_map(target_info.module_name)

        # --- Step 1: wildcard imports always force whole-module inclusion ----
        for imp in target_info.imports:
            if not imp.is_internal:
                continue
            if imp.is_from and any(orig == "*" for orig, _ in imp.names):
                roots.add((imp.module, None))
                logger.warning("Wildcard import from %s — whole module added to root set", imp.module)

        # --- Step 2: scan body for actual name refs --------------------------
        # Walk the full AST but skip ImportFrom/Import statements to avoid
        # treating imported names as "used" just because they were imported.
        tree = _ast.parse(target_info.source, filename=str(target_info.file_path))
        visitor = _NameRefVisitor()
        for node in _ast.iter_child_nodes(tree):
            if isinstance(node, (_ast.Import, _ast.ImportFrom)):
                continue
            visitor.visit(node)

        used_names: Set[str] = visitor.names
        used_chains: Set[str] = visitor.attr_chains

        # --- Step 3: map used names to internal symbols ----------------------
        for name in used_names:
            entry = internal_name_map.get(name)
            if entry:
                roots.add(entry)

        # --- Step 4: map attribute chains (comp.compute, lib.x.fn, …) --------
        for chain in used_chains:
            entry = self._resolver.resolve_attr_access(target_info.module_name, chain)
            if entry:
                roots.add(entry)

        # --- Step 5: bare module imports that are referenced in the body ------
        # e.g. `import lib.models.comp` and body uses `lib.models.comp.compute`
        for imp in target_info.imports:
            if not imp.is_internal or imp.is_from:
                continue
            # alias or top-level name
            if imp.names and imp.names[0][1]:
                alias = imp.names[0][1]
            else:
                alias = imp.module.split(".")[0]
            if alias in used_names or any(c.startswith(alias + ".") for c in used_chains):
                roots.add((imp.module, None))

        return roots

    # ------------------------------------------------------------------
    # BFS expansion
    # ------------------------------------------------------------------

    def _expand(self, node: ReachNode) -> Set[ReachNode]:
        mod_name, sym_name = node
        new_nodes: Set[ReachNode] = set()

        mod_info = self._modules.get(mod_name)
        if not mod_info:
            logger.debug("Module %s not in index (external?)", mod_name)
            return new_nodes

        # Include the __init__.py of the package if this is a sub-module
        pkg = ".".join(mod_name.split(".")[:-1])
        if pkg and pkg in self._modules:
            # We want to note the package is needed, but not expand ALL its symbols
            new_nodes.add((pkg, None))

        if sym_name is None:
            # Entire module included — add all its symbols
            for sname in mod_info.symbols:
                new_nodes.add((mod_name, sname))
            for aname in mod_info.top_level_assigns:
                new_nodes.add((mod_name, "__assign__:" + aname))
            if mod_info.has_wildcard_import:
                logger.warning("Module %s has wildcard import — conservatively included", mod_name)
            return new_nodes

        # Specific symbol
        sym_info = mod_info.symbols.get(sym_name)
        if sym_info is None:
            # Maybe it's an assignment/constant
            if sym_name.startswith("__assign__:"):
                assign_name = sym_name[len("__assign__:"):]
                sym_info = mod_info.top_level_assigns.get(assign_name)
            elif sym_name in mod_info.top_level_assigns:
                sym_info = mod_info.top_level_assigns[sym_name]

        if sym_info is None:
            logger.debug("Symbol %s not found in %s", sym_name, mod_name)
            return new_nodes

        internal_map = self._resolver.get_internal_name_map(mod_name)

        # Expand name refs
        for ref_name in sym_info.name_refs:
            entry = internal_map.get(ref_name)
            if entry:
                new_nodes.add(entry)
            # Also check if it's a top-level assign in same module
            if ref_name in mod_info.top_level_assigns:
                new_nodes.add((mod_name, "__assign__:" + ref_name))
            # Or a symbol in same module
            if ref_name in mod_info.symbols:
                new_nodes.add((mod_name, ref_name))

        # Expand attribute chains (e.g. 'comp.compute')
        for chain in sym_info.attr_chains:
            entry = self._resolver.resolve_attr_access(mod_name, chain)
            if entry:
                new_nodes.add(entry)

        # Expand decorators
        for dec_name in sym_info.decorator_names:
            entry = internal_map.get(dec_name)
            if entry:
                new_nodes.add(entry)
            if dec_name in mod_info.symbols:
                new_nodes.add((mod_name, dec_name))

        # Expand base classes
        for base_name in getattr(sym_info, "bases", []):
            entry = internal_map.get(base_name)
            if entry:
                new_nodes.add(entry)
            if base_name in mod_info.symbols:
                new_nodes.add((mod_name, base_name))

        return new_nodes

    # ------------------------------------------------------------------
    # Compute dropped symbols for reporting
    # ------------------------------------------------------------------

    def _compute_dropped(self) -> None:
        reachable_by_module: Dict[str, Set[str]] = {}
        for mod_name, sym_name in self.reachable:
            if sym_name is not None:
                reachable_by_module.setdefault(mod_name, set()).add(sym_name)

        for mod_name, mod_info in self._modules.items():
            reachable_syms = reachable_by_module.get(mod_name, set())
            dropped: Set[str] = set()
            for sym_name in mod_info.symbols:
                if sym_name not in reachable_syms:
                    dropped.add(sym_name)
            if dropped:
                self.dropped_symbols[mod_name] = dropped

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def is_module_reachable(self, module_name: str) -> bool:
        return any(m == module_name for m, _ in self.reachable)

    def reachable_symbols_in(self, module_name: str) -> Set[str]:
        """Return all symbol names reachable in a given module."""
        syms: Set[str] = set()
        for mod, sym in self.reachable:
            if mod == module_name and sym is not None:
                if not sym.startswith("__assign__:"):
                    syms.add(sym)
        return syms

    def reachable_assigns_in(self, module_name: str) -> Set[str]:
        """Return all top-level assignment names reachable in a given module."""
        assigns: Set[str] = set()
        for mod, sym in self.reachable:
            if mod == module_name and sym is not None:
                if sym.startswith("__assign__:"):
                    assigns.add(sym[len("__assign__:"):])
        return assigns