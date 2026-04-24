"""
dump_writer.py
Writes the final dump.txt file, optional dump_report.json,
and optional code_tree.txt showing the reachable module/symbol hierarchy.

Dump format:
  <<<PY_MODULE_START path="/lib/models/utils.py" lines=4>>>
  ...source...
  <<<PY_MODULE_END>>>

Modules are emitted in dependency order:
  1. Leaf utility modules (no internal deps)
  2. Dependent modules (in topological order)
  3. Package __init__.py files
  4. Target script last
"""
from __future__ import annotations

import json
import logging
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from .module_indexer import ModuleInfo
from .import_resolver import ImportResolver
from .reachability import ReachabilityAnalyzer
from .reconstructor import Reconstructor

logger = logging.getLogger(__name__)

BLOCK_START = '<<<PY_MODULE_START path="{path}" lines={lines}>>>'
BLOCK_END = "<<<PY_MODULE_END>>>"


class DumpWriter:
    def __init__(
        self,
        modules: Dict[str, ModuleInfo],
        import_resolver: ImportResolver,
        reachability: ReachabilityAnalyzer,
        reconstructor: Reconstructor,
        path_resolver,
        target_info: ModuleInfo,
    ):
        self._modules = modules
        self._resolver = import_resolver
        self._reach = reachability
        self._reconstructor = reconstructor
        self._path_resolver = path_resolver
        self._target = target_info

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def write(self, output_path: str, diagnostics_dir: Optional[str] = None) -> None:
        """
        Write dump.txt to output_path.
        If diagnostics_dir is given, also write into that directory:
          • dump_report.json
          • code_tree_full.txt   (all symbols, kept and dropped)
          • code_tree_pruned.txt (kept symbols only)
        """
        ordered = self._topological_order()
        logger.info("Emission order: %s", [m for m in ordered])

        blocks: List[str] = []

        # Library modules
        for mod_name in ordered:
            mod = self._modules[mod_name]
            source = self._reconstructor.reconstruct(mod_name)
            if not source.strip():
                logger.debug("Skipping empty module %s", mod_name)
                continue
            dump_path = self._path_resolver.relative_dump_path(mod.file_path)
            lines = source.count("\n") + (1 if not source.endswith("\n") else 0)
            source_body = source if source.endswith("\n") else source + "\n"
            block = (
                BLOCK_START.format(path=dump_path, lines=lines)
                + "\n"
                + source_body
                + BLOCK_END
            )
            blocks.append(block)
            logger.info("Emitted %s (%d lines)", dump_path, lines)

        # Target script (always full)
        target_source = self._target.source
        target_path = self._path_resolver.target_dump_path()
        target_lines = target_source.count("\n") + 1
        target_body = target_source if target_source.endswith("\n") else target_source + "\n"
        target_block = (
            BLOCK_START.format(path=target_path, lines=target_lines)
            + "\n"
            + target_body
            + BLOCK_END
        )
        blocks.append(target_block)
        logger.info("Emitted target %s (%d lines)", target_path, target_lines)

        dump_content = "\n\n".join(blocks) + "\n"
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(dump_content, encoding="utf-8")
        logger.info("Wrote dump to %s", out)

        if diagnostics_dir:
            d = Path(diagnostics_dir)
            d.mkdir(parents=True, exist_ok=True)
            self._write_report(str(d / "dump_report.json"), ordered)
            self._write_tree(str(d / "code_tree_full.txt"),   include_dropped=True)
            self._write_tree(str(d / "code_tree_pruned.txt"), include_dropped=False)

    # ------------------------------------------------------------------
    # Topological sort of reachable modules
    # ------------------------------------------------------------------

    def _topological_order(self) -> List[str]:
        """
        Return reachable lib modules in reading-friendly order:
          1. __init__.py files first, shallowest package first
             (they act as the package façade / table of contents)
          2. Regular modules in leaf-first topological order
             (dependencies before the modules that depend on them)
        """
        reachable_mods: Set[str] = set()
        for mod_name, sym in self._reach.reachable:
            reachable_mods.add(mod_name)

        # Build adjacency: mod → set of mods it imports from
        deps: Dict[str, Set[str]] = {m: set() for m in reachable_mods}
        for mod_name in reachable_mods:
            mod = self._modules.get(mod_name)
            if not mod:
                continue
            for imp in mod.imports:
                if imp.is_internal and imp.module in reachable_mods:
                    deps[mod_name].add(imp.module)

        # Kahn's algorithm (gives leaves-first topological order)
        in_degree: Dict[str, int] = {m: 0 for m in reachable_mods}
        reverse_deps: Dict[str, Set[str]] = {m: set() for m in reachable_mods}
        for mod_name, dep_set in deps.items():
            for dep in dep_set:
                in_degree[mod_name] += 1
                reverse_deps.setdefault(dep, set()).add(mod_name)

        queue: List[str] = sorted(m for m, d in in_degree.items() if d == 0)
        topo: List[str] = []
        while queue:
            node = queue.pop(0)
            topo.append(node)
            for successor in sorted(reverse_deps.get(node, [])):
                in_degree[successor] -= 1
                if in_degree[successor] == 0:
                    queue.append(successor)

        # Any remaining (cycles) — append conservatively
        remaining = [m for m in sorted(reachable_mods) if m not in topo]
        if remaining:
            logger.warning("Cycle detected; appending remaining modules: %s", remaining)
        topo.extend(remaining)

        # Split: __init__ modules go first (shallowest package first),
        # regular modules keep their topological order (leaves before dependents).
        def is_init(mod_name: str) -> bool:
            mod = self._modules.get(mod_name)
            return bool(mod and mod.is_init)

        init_mods = sorted(
            [m for m in topo if is_init(m)],
            key=lambda m: (m.count("."), m),   # shallower packages before deeper ones
        )
        regular_mods = [m for m in topo if not is_init(m)]

        return init_mods + regular_mods

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------

    def _write_report(self, report_path: str, ordered_mods: List[str]) -> None:
        reachable_syms: Dict[str, List[str]] = {}
        for mod_name, sym in sorted(self._reach.reachable, key=lambda x: (x[0], x[1] or "")):
            if sym and not sym.startswith("__assign__:"):
                reachable_syms.setdefault(mod_name, []).append(sym)

        report = {
            "target_script": self._path_resolver.target_dump_path(),
            "reachable_modules": sorted(set(m for m, _ in self._reach.reachable)),
            "reachable_symbols": reachable_syms,
            "dropped_symbols": {
                mod: sorted(syms)
                for mod, syms in sorted(self._reach.dropped_symbols.items())
            },
            "unresolved_references": sorted(self._resolver.warnings),
            "warnings": {
                "wildcard_imports": [
                    mn for mn, mi in self._modules.items() if mi.has_wildcard_import
                ],
                "dynamic_imports": [
                    mn for mn, mi in self._modules.items() if mi.has_dynamic_import
                ],
            },
            "emission_order": ordered_mods,
        }
        rp = Path(report_path)
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(json.dumps(report, indent=2), encoding="utf-8")
        logger.info("Wrote report to %s", rp)

    # ------------------------------------------------------------------
    # Code tree
    # ------------------------------------------------------------------

    def _write_tree(self, tree_path: str, include_dropped: bool = True) -> None:
        """
        Write a human-readable code tree to tree_path.

        include_dropped=True  → full tree: kept + [dropped] symbols
        include_dropped=False → pruned tree: kept symbols only

        Format example (full):
            lib/
            ├── data/
            │   ├── __init__.py
            │   │   └── → DataLoader  (re-export from lib.data.loader)
            │   └── loader.py
            │       ├── class DataLoader
            │       └── [dropped]  class LegacyLoader
            └── utils.py
                ├── def log_metric
                └── [dropped]  def save_checkpoint
            scripts/
            └── main.py  [target script]
        """
        # Only include modules that actually produce content in the dump
        # (same filter as write() uses — skips empty __init__.py files, etc.)
        emitted_mods: Set[str] = {
            mod_name
            for mod_name in {m for m, _ in self._reach.reachable}
            if self._modules.get(mod_name)
            and self._reconstructor.reconstruct(mod_name).strip()
        }

        path_to_mod: Dict[str, str] = {}
        for mod_name in emitted_mods:
            mod = self._modules[mod_name]
            dp = self._path_resolver.relative_dump_path(mod.file_path)
            path_to_mod[dp] = mod_name

        target_dp = self._path_resolver.target_dump_path()
        path_to_mod[target_dp] = "__target__"

        dir_tree: Dict[str, Any] = {}
        for dump_path in sorted(path_to_mod):
            parts = PurePosixPath(dump_path).parts[1:]
            node = dir_tree
            for segment in parts[:-1]:
                node = node.setdefault(segment + "/", {})
            node[parts[-1]] = dump_path

        header = "Code Tree — Full (kept + dropped)" if include_dropped else "Code Tree — Pruned (kept only)"
        lines: List[str] = [header, "=" * 60]
        lines.extend(self._render_dir(dir_tree, path_to_mod, prefix="", include_dropped=include_dropped))
        lines.append("")

        tp = Path(tree_path)
        tp.parent.mkdir(parents=True, exist_ok=True)
        tp.write_text("\n".join(lines), encoding="utf-8")
        logger.info("Wrote code tree (%s) to %s", "full" if include_dropped else "pruned", tp)

    # ------------------------------------------------------------------
    # Tree rendering helpers
    # ------------------------------------------------------------------

    def _render_dir(
        self,
        node: Dict[str, Any],
        path_to_mod: Dict[str, str],
        prefix: str,
        include_dropped: bool = True,
    ) -> Iterator[str]:
        """Recursively render a directory tree node."""
        items = sorted(node.items(), key=lambda kv: (
            0 if isinstance(kv[1], dict) else 1,
            kv[0],
        ))
        for i, (name, value) in enumerate(items):
            is_last = i == len(items) - 1
            connector  = "└── " if is_last else "├── "
            child_pfx  = prefix + ("    " if is_last else "│   ")

            if isinstance(value, dict):
                yield prefix + connector + name
                yield from self._render_dir(value, path_to_mod, child_pfx, include_dropped)
            else:
                dump_path = value
                mod_name  = path_to_mod.get(dump_path, "")
                is_target = mod_name == "__target__"
                label     = name + ("  [target script]" if is_target else "")
                yield prefix + connector + label
                if not is_target:
                    yield from self._render_symbols(mod_name, child_pfx, include_dropped)

    def _render_symbols(
        self, mod_name: str, prefix: str, include_dropped: bool = True
    ) -> Iterator[str]:
        """Render the kept (and optionally dropped) symbols for one module."""
        mod = self._modules.get(mod_name)
        if not mod:
            return

        kept_syms    = self._reach.reachable_symbols_in(mod_name)
        kept_assigns = self._reach.reachable_assigns_in(mod_name)
        dropped      = self._reach.dropped_symbols.get(mod_name, set())

        entries: List[Tuple[bool, str]] = []   # (is_dropped, label)

        if mod.is_init:
            for imp in mod.imports:
                if not imp.is_from:
                    continue
                for orig, alias in imp.names:
                    resolved = self._resolver._resolve_from_import(imp.module, orig)
                    if resolved and resolved in self._reach.reachable:
                        display = alias or orig
                        entries.append((False, f"→ {display}  (re-export from {imp.module})"))
        else:
            # Constants (source order)
            for sym in sorted(
                [mod.top_level_assigns[a] for a in kept_assigns if a in mod.top_level_assigns],
                key=lambda s: s.start_line,
            ):
                entries.append((False, f"{sym.name}  [constant]"))

            # Kept functions / classes (source order)
            for sym in sorted(
                [mod.symbols[s] for s in kept_syms if s in mod.symbols],
                key=lambda s: s.start_line,
            ):
                kind = "class" if sym.kind == "class" else "def"
                entries.append((False, f"{kind} {sym.name}"))

            # Dropped symbols (alphabetical) — only when include_dropped=True
            if include_dropped:
                for sym_name in sorted(dropped):
                    sym  = mod.symbols.get(sym_name)
                    kind = ("class" if sym and sym.kind == "class" else "def")
                    entries.append((True, f"{kind} {sym_name}"))

        for i, (is_dropped, label) in enumerate(entries):
            is_last   = i == len(entries) - 1
            connector = "└── " if is_last else "├── "
            if is_dropped:
                yield prefix + connector + f"[dropped]  {label}"
            else:
                yield prefix + connector + label