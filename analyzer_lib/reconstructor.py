"""
reconstructor.py
Re-generates minimal, runnable source for each reachable module.

Strategy:
  • Use line-range slicing from original source (preserves formatting, docstrings,
    comments, decorators — avoids ast.unparse style rewrites).
  • Filter internal imports to only needed names.
  • Keep external imports only when any retained symbol references them.
  • Reconstruct trimmed __init__.py files.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Set, Tuple

from .module_indexer import ImportInfo, ModuleInfo, SymbolInfo
from .import_resolver import ImportResolver
from .reachability import ReachabilityAnalyzer

logger = logging.getLogger(__name__)


def _fmt_from_import(module: str, names: List[Tuple[str, Optional[str]]]) -> str:
    """Render a filtered 'from X import ...' line."""
    parts = []
    for orig, alias in names:
        if alias:
            parts.append(f"{orig} as {alias}")
        else:
            parts.append(orig)
    if len(parts) == 1:
        return f"from {module} import {parts[0]}"
    joined = ", ".join(parts)
    if len(joined) <= 70:
        return f"from {module} import {joined}"
    inner = ",\n    ".join(parts)
    return f"from {module} import (\n    {inner},\n)"


def _fmt_plain_import(module: str, alias: Optional[str]) -> str:
    if alias:
        return f"import {module} as {alias}"
    return f"import {module}"


class Reconstructor:
    """
    Produces minimal source text for each reachable module.
    """

    def __init__(
        self,
        modules: Dict[str, ModuleInfo],
        import_resolver: ImportResolver,
        reachability: ReachabilityAnalyzer,
    ):
        self._modules = modules
        self._resolver = import_resolver
        self._reach = reachability

    def reconstruct(self, module_name: str) -> str:
        """Return the minimal source text for module_name."""
        mod = self._modules[module_name]
        if mod.is_init:
            return self._reconstruct_init(mod, module_name)
        return self._reconstruct_module(mod, module_name)

    # ------------------------------------------------------------------
    # Regular module reconstruction
    # ------------------------------------------------------------------

    def _reconstruct_module(self, mod: ModuleInfo, module_name: str) -> str:
        reachable_syms = self._reach.reachable_symbols_in(module_name)
        reachable_assigns = self._reach.reachable_assigns_in(module_name)

        # Symbols in source order
        ordered_syms = sorted(
            [mod.symbols[s] for s in reachable_syms if s in mod.symbols],
            key=lambda s: s.start_line,
        )
        ordered_assigns = sorted(
            [mod.top_level_assigns[a] for a in reachable_assigns if a in mod.top_level_assigns],
            key=lambda s: s.start_line,
        )

        # Collect all names referenced by kept items
        all_refs: Set[str] = set()
        for sym in ordered_syms + ordered_assigns:
            all_refs |= sym.name_refs
            all_refs |= {chain.split(".")[0] for chain in sym.attr_chains}
            all_refs |= set(sym.decorator_names)
            all_refs |= set(getattr(sym, "bases", []))

        # Build import lines
        import_lines = self._build_import_lines(mod, module_name, all_refs)

        # Assemble output
        sections: List[str] = []
        if import_lines:
            sections.append("\n".join(import_lines))

        for sym in ordered_assigns:
            sections.append("".join(sym.source_lines).rstrip())

        for sym in ordered_syms:
            sections.append("".join(sym.source_lines).rstrip())

        return "\n\n".join(s for s in sections if s.strip()) + "\n"

    # ------------------------------------------------------------------
    # __init__.py reconstruction (trim to only needed re-exports)
    # ------------------------------------------------------------------

    def _reconstruct_init(self, mod: ModuleInfo, module_name: str) -> str:
        reachable_syms = self._reach.reachable_symbols_in(module_name)
        local_syms = {s for s in reachable_syms if s in mod.symbols}
        local_assigns = self._reach.reachable_assigns_in(module_name)

        needed_names = self._needed_names_from_init(module_name)

        import_lines: List[str] = []
        seen_lines: Set[str] = set()

        for imp in mod.imports:
            line: Optional[str] = None

            if imp.is_from:
                # from X import a, b  — filter to only needed names
                keep = []
                for orig, alias in imp.names:
                    local = alias or orig
                    if orig in needed_names or local in needed_names or orig == "*":
                        keep.append((orig, alias))
                if keep:
                    line = _fmt_from_import(imp.module, keep)
            else:
                # import X  /  import X as Y
                # Keep unconditionally if the module is internal (it's a dep),
                # or if any bound name is in needed_names (external).
                if imp.is_internal:
                    alias = imp.names[0][1] if imp.names else None
                    line = _fmt_plain_import(imp.module, alias)
                else:
                    bound = imp.bound_names
                    if any(n in needed_names for n in bound):
                        alias = imp.names[0][1] if imp.names else None
                        line = _fmt_plain_import(imp.module, alias)

            if line and line not in seen_lines:
                import_lines.append(line)
                seen_lines.add(line)

        # External imports needed by local symbols defined directly in __init__
        all_refs: Set[str] = set()
        for sym_name in local_syms | local_assigns:
            sym = mod.symbols.get(sym_name) or mod.top_level_assigns.get(sym_name)
            if sym:
                all_refs |= sym.name_refs
        for line in self._build_external_imports(mod, all_refs):
            if line not in seen_lines:
                import_lines.append(line)
                seen_lines.add(line)

        sections: List[str] = []
        if import_lines:
            sections.append("\n".join(import_lines))

        # Local symbols/assignments defined directly in __init__
        for sym_name in sorted(local_syms):
            sym = mod.symbols[sym_name]
            sections.append("".join(sym.source_lines).rstrip())

        return "\n\n".join(s for s in sections if s.strip()) + "\n" if sections else ""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _needed_names_from_init(self, init_module_name: str) -> Set[str]:
        """
        Determine which names must be re-exported by this __init__.py.

        A name is needed when:
          1. A reachable lib module imports it from this package, OR
          2. The target script ('__target__') imports it from this package
             AND the resolved symbol is reachable.

        We treat '__target__' specially because it is never added to
        reach.reachable (it is the analysis root, not a lib node).
        """
        needed: Set[str] = set()

        for mod_name, mod_info in self._modules.items():
            # Always include the target script; for lib modules require reachability.
            is_target = mod_name == "__target__"
            if not is_target and not self._reach.is_module_reachable(mod_name):
                continue

            for imp in mod_info.imports:
                if imp.module != init_module_name or not imp.is_from:
                    continue
                for orig, alias in imp.names:
                    if orig == "*":
                        # Conservative: wildcard forces full init inclusion downstream
                        needed.add("*")
                        continue
                    # Only include if the ultimately-resolved symbol is reachable.
                    resolved = self._resolver._resolve_from_import(init_module_name, orig)
                    if resolved:
                        act_mod, act_sym = resolved
                        if (act_mod, act_sym) in self._reach.reachable or \
                           (act_mod, None) in self._reach.reachable:
                            needed.add(orig)
                    else:
                        # Cannot resolve → conservative inclusion
                        needed.add(orig)

        # Also include symbols defined directly in this __init__ that are reachable
        needed |= self._reach.reachable_symbols_in(init_module_name)
        return needed

    def _build_import_lines(
        self, mod: ModuleInfo, module_name: str, all_refs: Set[str]
    ) -> List[str]:
        """
        Build minimal import lines for a regular (non-init) module.
        """
        internal_map = self._resolver.get_internal_name_map(module_name)
        reachable_syms = self._reach.reachable_symbols_in(module_name)
        reachable_assigns = self._reach.reachable_assigns_in(module_name)

        # Collect names actually needed from each import
        needed_from_import: Dict[int, List[Tuple[str, Optional[str]]]] = {}
        used_modules: Set[str] = set()

        for local_name, (act_mod, act_sym) in internal_map.items():
            # Is this local name referenced by any retained symbol?
            if local_name in all_refs:
                # Find the import that provides this local name
                for i, imp in enumerate(mod.imports):
                    if not imp.is_internal:
                        continue
                    bound = imp.bound_names
                    if local_name in bound:
                        if imp.is_from:
                            orig = bound[local_name]
                            alias = local_name if local_name != orig else None
                            needed_from_import.setdefault(i, []).append((orig, alias))
                        else:
                            used_modules.add(i)
                        break

        lines: List[str] = []
        seen: Set[str] = set()

        for i, imp in enumerate(mod.imports):
            if imp.is_internal:
                if i in needed_from_import and imp.is_from:
                    line = _fmt_from_import(imp.module, needed_from_import[i])
                    if line not in seen:
                        lines.append(line)
                        seen.add(line)
                elif i in used_modules:
                    bound = imp.bound_names
                    alias = list(bound.keys())[0] if bound else None
                    line = _fmt_plain_import(imp.module, alias if alias != imp.module.split(".")[-1] else None)
                    if line not in seen:
                        lines.append(line)
                        seen.add(line)
            else:
                # External import: keep if any bound name is referenced
                bound = imp.bound_names
                if any(name in all_refs for name in bound):
                    line = self._render_external_import(imp)
                    if line not in seen:
                        lines.append(line)
                        seen.add(line)

        return lines

    def _build_external_imports(self, mod: ModuleInfo, all_refs: Set[str]) -> List[str]:
        lines: List[str] = []
        for imp in mod.imports:
            if imp.is_internal:
                continue
            bound = imp.bound_names
            if any(name in all_refs for name in bound):
                lines.append(self._render_external_import(imp))
        return lines

    def _render_external_import(self, imp: ImportInfo) -> str:
        if imp.is_from:
            return _fmt_from_import(imp.module, imp.names)
        else:
            if imp.names:
                _, alias = imp.names[0]
                return _fmt_plain_import(imp.module, alias)
            return _fmt_plain_import(imp.module, None)