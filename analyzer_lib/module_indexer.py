"""
module_indexer.py
Parses every Python file in lib/ into an AST index.
Produces ModuleInfo (module-level metadata) and SymbolInfo (per top-level object).
"""
from __future__ import annotations

import ast
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data-classes
# ---------------------------------------------------------------------------

@dataclass
class ImportInfo:
    """A single import statement parsed from a module."""
    module: str                          # dotted module path being imported from/as
    names: List[Tuple[str, Optional[str]]]  # [(name, alias), …]; empty list = bare module import
    is_from: bool                        # True → from X import Y
    line: int
    end_line: int
    is_internal: bool = False            # set by indexer when module starts with 'lib.'

    @property
    def bound_names(self) -> Dict[str, str]:
        """Return {local_name: original_name} bound by this import."""
        result: Dict[str, str] = {}
        if self.is_from:
            for orig, alias in self.names:
                result[alias or orig] = orig
        else:
            if self.names:
                # import lib.x as alias  → alias_name
                _, alias = self.names[0]
                if alias:
                    result[alias] = self.module
                else:
                    # import lib.x  → binds top-level name 'lib'
                    top = self.module.split(".")[0]
                    result[top] = self.module
            else:
                top = self.module.split(".")[0]
                result[top] = self.module
        return result


@dataclass
class SymbolInfo:
    """One top-level def, class, or assignment in a module."""
    name: str
    kind: str                  # 'function' | 'class' | 'assignment'
    start_line: int            # includes decorators
    end_line: int
    source_lines: List[str]    # pre-sliced source lines for this symbol
    decorator_names: List[str] = field(default_factory=list)
    bases: List[str] = field(default_factory=list)       # class base names
    name_refs: Set[str] = field(default_factory=set)     # names referenced in body
    attr_chains: Set[str] = field(default_factory=set)   # dotted-attr accesses e.g. 'comp.compute'
    node: Optional[ast.AST] = field(default=None, repr=False)


@dataclass
class ModuleInfo:
    """All metadata about one parsed Python module."""
    module_name: str
    file_path: Path
    source: str
    source_lines: List[str]
    imports: List[ImportInfo] = field(default_factory=list)
    symbols: Dict[str, SymbolInfo] = field(default_factory=dict)
    top_level_assigns: Dict[str, SymbolInfo] = field(default_factory=dict)
    is_init: bool = False
    has_wildcard_import: bool = False
    has_dynamic_import: bool = False
    top_level_stmts: List[Tuple[int, int]] = field(default_factory=list)  # (start,end) of non-obj stmts


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------

class _NameRefVisitor(ast.NodeVisitor):
    """Collect all Name refs and dotted Attribute chains inside a subtree."""

    def __init__(self):
        self.names: Set[str] = set()
        self.attr_chains: Set[str] = set()

    def visit_Name(self, node: ast.Name):
        self.names.add(node.id)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute):
        chain = _attr_chain(node)
        if chain:
            self.attr_chains.add(chain)
        self.generic_visit(node)


def _attr_chain(node: ast.Attribute) -> Optional[str]:
    """Collapse  a.b.c  AST node into the string 'a.b.c', or None if not a plain chain."""
    parts: List[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


def _decorator_names(node) -> List[str]:
    names: List[str] = []
    for dec in getattr(node, "decorator_list", []):
        if isinstance(dec, ast.Name):
            names.append(dec.id)
        elif isinstance(dec, ast.Attribute):
            chain = _attr_chain(dec)
            if chain:
                names.append(chain)
        elif isinstance(dec, ast.Call):
            func = dec.func
            if isinstance(func, ast.Name):
                names.append(func.id)
            elif isinstance(func, ast.Attribute):
                chain = _attr_chain(func)
                if chain:
                    names.append(chain)
    return names


def _base_names(node: ast.ClassDef) -> List[str]:
    names: List[str] = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            names.append(base.id)
        elif isinstance(base, ast.Attribute):
            chain = _attr_chain(base)
            if chain:
                names.append(chain)
    return names


def _collect_refs(nodes) -> Tuple[Set[str], Set[str]]:
    """Collect (name_refs, attr_chains) from a list of AST nodes."""
    visitor = _NameRefVisitor()
    for node in nodes:
        visitor.visit(node)
    return visitor.names, visitor.attr_chains


def _symbol_start_line(node) -> int:
    """First line of a symbol, accounting for decorators."""
    if getattr(node, "decorator_list", None):
        return node.decorator_list[0].lineno
    return node.lineno


# ---------------------------------------------------------------------------
# Import parsing
# ---------------------------------------------------------------------------

def _parse_import(node: ast.stmt) -> Optional[ImportInfo]:
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        names = [(alias.name, alias.asname) for alias in node.names]
        return ImportInfo(
            module=module,
            names=names,
            is_from=True,
            line=node.lineno,
            end_line=node.end_lineno or node.lineno,
        )
    elif isinstance(node, ast.Import):
        results = []
        for alias in node.names:
            results.append(ImportInfo(
                module=alias.name,
                names=[(alias.name, alias.asname)] if alias.asname else [],
                is_from=False,
                line=node.lineno,
                end_line=node.end_lineno or node.lineno,
            ))
        # Return the first (callers may need to handle multiple)
        return results if results else None
    return None


def _is_internal(module: str) -> bool:
    return module == "lib" or module.startswith("lib.")


# ---------------------------------------------------------------------------
# Main indexer
# ---------------------------------------------------------------------------

class ModuleIndexer:
    """Parses all modules and returns a {module_name: ModuleInfo} index."""

    def __init__(self, path_resolver):
        self._resolver = path_resolver
        self.modules: Dict[str, ModuleInfo] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def index_lib(self) -> None:
        """Parse all .py files under lib/."""
        for py_file in self._resolver.discover_lib_modules():
            try:
                self._index_file(py_file)
            except SyntaxError as exc:
                logger.error("Syntax error in %s: %s", py_file, exc)
                raise

    def index_file(self, file_path: Path) -> ModuleInfo:
        """Index a single file (used for the target script)."""
        return self._index_file(file_path, force_module_name="__target__")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _index_file(self, file_path: Path, force_module_name: str = None) -> ModuleInfo:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        source_lines = source.splitlines(keepends=True)
        tree = ast.parse(source, filename=str(file_path))

        module_name = force_module_name or self._resolver.path_to_module_name(file_path)
        is_init = file_path.name == "__init__.py"

        info = ModuleInfo(
            module_name=module_name,
            file_path=file_path,
            source=source,
            source_lines=source_lines,
            is_init=is_init,
        )

        for node in ast.iter_child_nodes(tree):
            self._process_top_level(node, info)

        self.modules[module_name] = info
        logger.debug("Indexed %s  (%d symbols, %d imports)",
                     module_name, len(info.symbols), len(info.imports))
        return info

    def _process_top_level(self, node: ast.stmt, info: ModuleInfo) -> None:
        src_lines = info.source_lines

        # ---- imports -------------------------------------------------------
        if isinstance(node, ast.ImportFrom):
            imp = ImportInfo(
                module=node.module or "",
                names=[(a.name, a.asname) for a in node.names],
                is_from=True,
                line=node.lineno,
                end_line=node.end_lineno or node.lineno,
                is_internal=_is_internal(node.module or ""),
            )
            if any(n == "*" for n, _ in imp.names):
                info.has_wildcard_import = True
                logger.warning("Wildcard import in %s line %d — conservative mode",
                               info.module_name, node.lineno)
            info.imports.append(imp)
            return

        if isinstance(node, ast.Import):
            for alias in node.names:
                imp = ImportInfo(
                    module=alias.name,
                    names=[(alias.name, alias.asname)] if alias.asname else [],
                    is_from=False,
                    line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    is_internal=_is_internal(alias.name),
                )
                info.imports.append(imp)
            return

        # ---- functions -----------------------------------------------------
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = _symbol_start_line(node)
            end = node.end_lineno
            name_refs, attr_chains = _collect_refs(list(ast.walk(node)))
            sym = SymbolInfo(
                name=node.name,
                kind="function",
                start_line=start,
                end_line=end,
                source_lines=src_lines[start - 1: end],
                decorator_names=_decorator_names(node),
                name_refs=name_refs,
                attr_chains=attr_chains,
                node=node,
            )
            info.symbols[node.name] = sym
            return

        # ---- classes -------------------------------------------------------
        if isinstance(node, ast.ClassDef):
            start = _symbol_start_line(node)
            end = node.end_lineno
            name_refs, attr_chains = _collect_refs(list(ast.walk(node)))
            sym = SymbolInfo(
                name=node.name,
                kind="class",
                start_line=start,
                end_line=end,
                source_lines=src_lines[start - 1: end],
                decorator_names=_decorator_names(node),
                bases=_base_names(node),
                name_refs=name_refs,
                attr_chains=attr_chains,
                node=node,
            )
            info.symbols[node.name] = sym
            return

        # ---- top-level assignments (constants etc.) -----------------------
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            self._process_assignment(node, info)
            return

        # ---- dynamic import detection -------------------------------------
        if isinstance(node, ast.Expr):
            src = "".join(info.source_lines[node.lineno - 1: node.end_lineno])
            if "__import__" in src or "importlib" in src:
                info.has_dynamic_import = True
                logger.warning("Dynamic import detected in %s line %d",
                               info.module_name, node.lineno)

        # Record as an "other" top-level statement (side-effect code)
        info.top_level_stmts.append((node.lineno, getattr(node, "end_lineno", node.lineno)))

    def _process_assignment(self, node, info: ModuleInfo) -> None:
        names: List[str] = []
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    names.append(tgt.id)
                elif isinstance(tgt, ast.Tuple):
                    for elt in tgt.elts:
                        if isinstance(elt, ast.Name):
                            names.append(elt.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                names.append(node.target.id)
        elif isinstance(node, ast.AugAssign):
            if isinstance(node.target, ast.Name):
                names.append(node.target.id)

        start = node.lineno
        end = getattr(node, "end_lineno", node.lineno)
        name_refs, attr_chains = _collect_refs([node])

        for name in names:
            sym = SymbolInfo(
                name=name,
                kind="assignment",
                start_line=start,
                end_line=end,
                source_lines=info.source_lines[start - 1: end],
                name_refs=name_refs - set(names),
                attr_chains=attr_chains,
                node=node,
            )
            info.top_level_assigns[name] = sym
