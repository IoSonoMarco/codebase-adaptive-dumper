#!/usr/bin/env python3
"""
analyzer.py  —  CLI entry point for the codebase dependency pruner.

Usage:
    python analyzer.py --target scripts/main.py --lib-root /path/to/project --output dump.txt
    python analyzer.py --target scripts/main.py --lib-root /path/to/project/lib --output dump.txt --report dump_report.json --verbose
"""
import argparse
import logging
import sys
from pathlib import Path


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(levelname)-8s %(name)s: %(message)s"
    logging.basicConfig(level=level, format=fmt, stream=sys.stderr)
    # Suppress noisy child loggers unless verbose
    if not verbose:
        logging.getLogger("analyzer_lib.module_indexer").setLevel(logging.WARNING)
        logging.getLogger("analyzer_lib.reachability").setLevel(logging.WARNING)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Produce a minimal runnable code dump for a Python target script."
    )
    parser.add_argument(
        "--target", required=True,
        help="Path to the target script (e.g. scripts/main.py)"
    )
    parser.add_argument(
        "--lib-root", required=True,
        help="Path containing the lib/ package (or the lib/ dir itself)"
    )
    parser.add_argument(
        "--output", required=True,
        help="Output path for dump.txt"
    )
    parser.add_argument(
        "--diagnostics", default=None, metavar="DIR",
        help=(
            "Optional directory for diagnostic outputs: "
            "dump_report.json, code_tree_full.txt, code_tree_pruned.txt"
        )
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Enable debug logging"
    )
    args = parser.parse_args()
    _setup_logging(args.verbose)

    logger = logging.getLogger("analyzer")

    try:
        from analyzer_lib.path_resolver import PathResolver
        from analyzer_lib.module_indexer import ModuleIndexer
        from analyzer_lib.import_resolver import ImportResolver
        from analyzer_lib.reachability import ReachabilityAnalyzer
        from analyzer_lib.reconstructor import Reconstructor
        from analyzer_lib.dump_writer import DumpWriter
    except ImportError as exc:
        logger.error("Failed to import analyzer_lib: %s", exc)
        logger.error("Make sure you are running from the directory containing analyzer_lib/")
        return 1

    # ------------------------------------------------------------------ #
    # Phase 1: Normalize paths
    # ------------------------------------------------------------------ #
    logger.info("=== Phase 1: Path normalization ===")
    try:
        resolver = PathResolver(args.target, args.lib_root)
    except FileNotFoundError as exc:
        logger.error("Path error: %s", exc)
        return 1

    # ------------------------------------------------------------------ #
    # Phase 2: AST indexing
    # ------------------------------------------------------------------ #
    logger.info("=== Phase 2: AST indexing ===")
    indexer = ModuleIndexer(resolver)
    try:
        indexer.index_lib()
    except SyntaxError as exc:
        logger.error("Syntax error during lib indexing: %s", exc)
        return 1

    logger.info("Indexed %d lib modules", len(indexer.modules))
    for mod_name in sorted(indexer.modules):
        logger.info("  %s", mod_name)

    try:
        target_info = indexer.index_file(resolver.target_script)
    except SyntaxError as exc:
        logger.error("Syntax error in target script: %s", exc)
        return 1

    # ------------------------------------------------------------------ #
    # Phase 3: Import resolution
    # ------------------------------------------------------------------ #
    logger.info("=== Phase 3: Import resolution ===")
    imp_resolver = ImportResolver(indexer.modules, resolver)
    logger.info("Symbol table has %d entries", len(imp_resolver.symbol_table))

    # ------------------------------------------------------------------ #
    # Phase 4 + 5: Root extraction and reachability
    # ------------------------------------------------------------------ #
    logger.info("=== Phase 4+5: Reachability analysis ===")
    reach = ReachabilityAnalyzer(indexer.modules, imp_resolver)
    reach.compute(target_info)

    reachable_mods = sorted({m for m, _ in reach.reachable})
    logger.info("Reachable modules: %s", reachable_mods)

    total_syms = sum(len(reach.reachable_symbols_in(m)) for m in reachable_mods)
    total_dropped = sum(len(v) for v in reach.dropped_symbols.values())
    logger.info("Reachable symbols: %d  |  Dropped symbols: %d", total_syms, total_dropped)

    for mod_name, dropped in sorted(reach.dropped_symbols.items()):
        logger.info("  Pruned from %s: %s", mod_name, sorted(dropped))

    # ------------------------------------------------------------------ #
    # Phase 6: Reconstruction
    # ------------------------------------------------------------------ #
    logger.info("=== Phase 6: Module reconstruction ===")
    reconstructor = Reconstructor(indexer.modules, imp_resolver, reach)

    # ------------------------------------------------------------------ #
    # Phase 7: Dump emission
    # ------------------------------------------------------------------ #
    logger.info("=== Phase 7: Dump emission ===")
    writer = DumpWriter(
        modules=indexer.modules,
        import_resolver=imp_resolver,
        reachability=reach,
        reconstructor=reconstructor,
        path_resolver=resolver,
        target_info=target_info,
    )

    try:
        writer.write(args.output, args.diagnostics)
    except Exception as exc:
        logger.error("Failed to write dump: %s", exc)
        return 1

    if args.diagnostics:
        logger.info("Diagnostics in: %s", args.diagnostics)
    logger.info("Done. Output: %s", args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())