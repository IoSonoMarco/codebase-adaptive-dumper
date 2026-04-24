#!/usr/bin/env bash
# run_analyzer.sh — Bash wrapper around analyzer.py (Linux/macOS equivalent of run_analyzer.ps1)
#
# Usage:
#   ./run_analyzer.sh --target scripts/main.py --lib-root ./my_project
#   ./run_analyzer.sh --target scripts/train.py --lib-root ./project/lib \
#                     --output ./output/dump.txt --diagnostics ./diag \
#                     --python python3 --verbose

set -euo pipefail

# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------

_cyan=""
_red=""
_reset=""
if [ -t 1 ] && command -v tput &>/dev/null; then
    _cyan=$(tput setaf 6 2>/dev/null || true)
    _red=$(tput setaf 1 2>/dev/null || true)
    _reset=$(tput sgr0 2>/dev/null || true)
fi

step()  { echo "${_cyan}[analyzer] $*${_reset}"; }
fail()  { echo "${_red}[ERROR] $*${_reset}" >&2; }

# ---------------------------------------------------------------------------
# Portable absolute-path resolution (realpath or readlink -f fallback)
# ---------------------------------------------------------------------------

abs_path() {
    if command -v realpath &>/dev/null; then
        realpath "$1"
    else
        readlink -f "$1"
    fi
}

resolve_required_path() {
    local path="$1" label="$2"
    if [ ! -e "$path" ]; then
        fail "$label not found: $path"
        exit 1
    fi
    abs_path "$path"
}

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Options:
  --target      PATH    (required) Path to the target Python script
  --lib-root    PATH    (required) Path to the lib/ directory or its parent
  --output      PATH    Output path for dump.txt  [default: ./dump.txt]
  --diagnostics DIR     Directory for dump_report.json and code trees  [default: disabled]
  --python      EXE     Python interpreter to use  [default: python3]
  --verbose             Enable debug-level logging from the Python analyzer
  --help                Show this help message and exit

Examples:
  ./run_analyzer.sh --target scripts/main.py --lib-root ./project
  ./run_analyzer.sh --target scripts/train.py --lib-root ./project/lib \\
                    --output ./output/dump.txt --diagnostics ./diag \\
                    --python python3 --verbose
EOF
}

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

TARGET=""
LIB_ROOT=""
OUTPUT="./dump.txt"
DIAGNOSTICS_DIR=""
PYTHON_EXE="python3"
VERBOSE=0

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

while [[ $# -gt 0 ]]; do
    case "$1" in
        --target)      TARGET="$2";         shift 2 ;;
        --lib-root)    LIB_ROOT="$2";       shift 2 ;;
        --output)      OUTPUT="$2";         shift 2 ;;
        --diagnostics) DIAGNOSTICS_DIR="$2"; shift 2 ;;
        --python)      PYTHON_EXE="$2";     shift 2 ;;
        --verbose)     VERBOSE=1;           shift ;;
        --help|-h)     usage; exit 0 ;;
        *)
            fail "Unknown option: $1"
            usage
            exit 1
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Validate required arguments
# ---------------------------------------------------------------------------

if [ -z "$TARGET" ] || [ -z "$LIB_ROOT" ]; then
    fail "--target and --lib-root are required."
    usage
    exit 1
fi

# ---------------------------------------------------------------------------
# Locate analyzer.py next to this script
# ---------------------------------------------------------------------------

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ANALYZER="$SCRIPT_DIR/analyzer.py"

if [ ! -f "$ANALYZER" ]; then
    fail "analyzer.py not found next to this script at: $ANALYZER"
    exit 1
fi

# ---------------------------------------------------------------------------
# Validate Python interpreter
# ---------------------------------------------------------------------------

step "Checking Python interpreter: $PYTHON_EXE"
if ! PY_VERSION=$("$PYTHON_EXE" --version 2>&1); then
    fail "Python interpreter '$PYTHON_EXE' not found or not executable."
    fail "Pass --python with the correct path or name (e.g. python3)."
    exit 1
fi
step "Found: $PY_VERSION"

# ---------------------------------------------------------------------------
# Validate and resolve input paths
# ---------------------------------------------------------------------------

step "Validating input paths..."
TARGET_ABS=$(resolve_required_path "$TARGET"   "TargetScriptPath")
LIB_ROOT_ABS=$(resolve_required_path "$LIB_ROOT" "InternalLibraryPath")

if [[ "$TARGET_ABS" != *.py ]]; then
    fail "--target must point to a .py file: $TARGET_ABS"
    exit 1
fi

step "Target script   : $TARGET_ABS"
step "Library path    : $LIB_ROOT_ABS"

# ---------------------------------------------------------------------------
# Resolve output paths
# ---------------------------------------------------------------------------

OUTPUT_ABS=$(abs_path "$OUTPUT")
OUTPUT_DIR=$(dirname "$OUTPUT_ABS")

if [ ! -d "$OUTPUT_DIR" ]; then
    step "Creating output directory: $OUTPUT_DIR"
    mkdir -p "$OUTPUT_DIR"
fi

step "Output dump     : $OUTPUT_ABS"

if [ -n "$DIAGNOSTICS_DIR" ]; then
    DIAGNOSTICS_ABS=$(abs_path "$DIAGNOSTICS_DIR")
    step "Diagnostics dir : $DIAGNOSTICS_ABS (dump_report.json, code_tree_full.txt, code_tree_pruned.txt)"
else
    DIAGNOSTICS_ABS=""
    step "Diagnostics     : disabled  (pass --diagnostics ./diag to enable)"
fi

# ---------------------------------------------------------------------------
# Build Python command
# ---------------------------------------------------------------------------

CMD=("$PYTHON_EXE" "$ANALYZER"
    --target   "$TARGET_ABS"
    --lib-root "$LIB_ROOT_ABS"
    --output   "$OUTPUT_ABS"
)

if [ -n "$DIAGNOSTICS_ABS" ]; then
    CMD+=(--diagnostics "$DIAGNOSTICS_ABS")
fi

if [ "$VERBOSE" -eq 1 ]; then
    CMD+=(--verbose)
fi

# ---------------------------------------------------------------------------
# Run analyzer
# ---------------------------------------------------------------------------

step "Running Python analyzer..."
step "Command: ${CMD[*]}"
echo ""

set +e
"${CMD[@]}"
EXIT_CODE=$?
set -e

echo ""

# ---------------------------------------------------------------------------
# Report outcome
# ---------------------------------------------------------------------------

if [ "$EXIT_CODE" -ne 0 ]; then
    fail "Analyzer failed with exit code $EXIT_CODE."
    exit "$EXIT_CODE"
fi

if [ -f "$OUTPUT_ABS" ]; then
    SIZE=$(wc -c < "$OUTPUT_ABS")
    step "SUCCESS — dump written: $OUTPUT_ABS ($SIZE bytes)"
else
    fail "Analyzer exited 0 but dump file was not created: $OUTPUT_ABS"
    exit 1
fi

if [ -n "$DIAGNOSTICS_ABS" ] && [ -d "$DIAGNOSTICS_ABS" ]; then
    step "Diagnostics written to: $DIAGNOSTICS_ABS"
    for f in "$DIAGNOSTICS_ABS"/*; do
        [ -e "$f" ] && step "  $(basename "$f")"
    done
fi

exit 0
