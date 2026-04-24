<#
.SYNOPSIS
    Runs the Python codebase dependency pruner to produce a minimal dump.txt.

.DESCRIPTION
    PowerShell wrapper around analyzer.py.
    Validates inputs, resolves paths, invokes the Python analyzer,
    captures output, and surfaces any errors clearly.

.PARAMETER TargetScriptPath
    Path to the main Python script to analyze (e.g. .\scripts\main.py).

.PARAMETER InternalLibraryPath
    Path to the directory containing the lib\ package, or to lib\ itself.

.PARAMETER OutputPath
    Destination for the generated dump.txt file. Defaults to .\dump.txt.

.PARAMETER ReportPath
    Optional path for the JSON diagnostics report. Defaults to .\dump_report.json.

.PARAMETER PythonExe
    Python interpreter to use. Defaults to 'python'. Use 'python3' or a
    full path (e.g. C:\envs\myenv\Scripts\python.exe) when needed.

.PARAMETER Verbose
    Pass -Verbose to enable debug-level logging from the Python analyzer.

.EXAMPLE
    .\run_analyzer.ps1 -TargetScriptPath .\scripts\main.py -InternalLibraryPath .\project

.EXAMPLE
    .\run_analyzer.ps1 -TargetScriptPath .\scripts\train.py `
                       -InternalLibraryPath .\project\lib `
                       -OutputPath .\output\dump.txt `
                       -ReportPath .\output\report.json `
                       -PythonExe python3 `
                       -Verbose
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$TargetScriptPath,

    [Parameter(Mandatory = $true)]
    [string]$InternalLibraryPath,

    [Parameter(Mandatory = $false)]
    [string]$OutputPath = ".\dump.txt",

    [Parameter(Mandatory = $false)]
    [string]$DiagnosticsDir = "",

    [Parameter(Mandatory = $false)]
    [string]$PythonExe = "python"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Write-Step {
    param([string]$Message)
    Write-Host "[analyzer] $Message" -ForegroundColor Cyan
}

function Write-Fail {
    param([string]$Message)
    Write-Host "[ERROR] $Message" -ForegroundColor Red
}

function Resolve-RequiredPath {
    param([string]$InputPath, [string]$Label)
    $abs = [System.IO.Path]::GetFullPath($InputPath)
    if (-not (Test-Path $abs)) {
        Write-Fail "$Label not found: $abs"
        exit 1
    }
    return $abs
}

# ---------------------------------------------------------------------------
# Locate this script's directory so we can find analyzer.py
# ---------------------------------------------------------------------------

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AnalyzerScript = Join-Path $ScriptDir "analyzer.py"

if (-not (Test-Path $AnalyzerScript)) {
    Write-Fail "analyzer.py not found next to this script at: $AnalyzerScript"
    exit 1
}

# ---------------------------------------------------------------------------
# Validate Python
# ---------------------------------------------------------------------------

Write-Step "Checking Python interpreter: $PythonExe"
try {
    $pyVersion = & $PythonExe --version 2>&1
    Write-Step "Found: $pyVersion"
}
catch {
    Write-Fail "Python interpreter '$PythonExe' not found or not executable."
    Write-Fail "Pass -PythonExe with the correct path or name (e.g. python3)."
    exit 1
}

# ---------------------------------------------------------------------------
# Validate input paths
# ---------------------------------------------------------------------------

Write-Step "Validating input paths..."
$TargetAbs  = Resolve-RequiredPath -InputPath $TargetScriptPath  -Label "TargetScriptPath"
$LibRootAbs = Resolve-RequiredPath -InputPath $InternalLibraryPath -Label "InternalLibraryPath"

# Quick sanity: target must be a .py file
if (-not $TargetAbs.EndsWith(".py")) {
    Write-Fail "TargetScriptPath must point to a .py file: $TargetAbs"
    exit 1
}

Write-Step "Target script   : $TargetAbs"
Write-Step "Library path    : $LibRootAbs"

# ---------------------------------------------------------------------------
# Resolve output paths
# ---------------------------------------------------------------------------

$OutputAbs = [System.IO.Path]::GetFullPath($OutputPath)

$OutputDir = Split-Path -Parent $OutputAbs
if (-not (Test-Path $OutputDir)) {
    Write-Step "Creating output directory: $OutputDir"
    New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
}

Write-Step "Output dump     : $OutputAbs"

if ($DiagnosticsDir -ne "") {
    $DiagnosticsAbs = [System.IO.Path]::GetFullPath($DiagnosticsDir)
    Write-Step "Diagnostics dir : $DiagnosticsAbs (dump_report.json, code_tree_full.txt, code_tree_pruned.txt)"
} else {
    $DiagnosticsAbs = ""
    Write-Step "Diagnostics     : disabled  (pass -DiagnosticsDir .\diag to enable)"
}

# ---------------------------------------------------------------------------
# Build Python command
# ---------------------------------------------------------------------------

$PythonArgs = @(
    $AnalyzerScript,
    "--target",   $TargetAbs,
    "--lib-root", $LibRootAbs,
    "--output",   $OutputAbs
)

if ($DiagnosticsAbs -ne "") {
    $PythonArgs += "--diagnostics"
    $PythonArgs += $DiagnosticsAbs
}

if ($VerbosePreference -eq "Continue") {
    $PythonArgs += "--verbose"
}

# ---------------------------------------------------------------------------
# Run analyzer
# ---------------------------------------------------------------------------

Write-Step "Running Python analyzer..."
Write-Step "Command: $PythonExe $($PythonArgs -join ' ')"
Write-Host ""

$Process = Start-Process `
    -FilePath $PythonExe `
    -ArgumentList $PythonArgs `
    -NoNewWindow `
    -PassThru `
    -Wait

$ExitCode = $Process.ExitCode

Write-Host ""

# ---------------------------------------------------------------------------
# Report outcome
# ---------------------------------------------------------------------------

if ($ExitCode -ne 0) {
    Write-Fail "Analyzer failed with exit code $ExitCode."
    exit $ExitCode
}

if (Test-Path $OutputAbs) {
    $Size = (Get-Item $OutputAbs).Length
    Write-Step "SUCCESS — dump written: $OutputAbs ($Size bytes)"
}
else {
    Write-Fail "Analyzer exited 0 but dump file was not created: $OutputAbs"
    exit 1
}

if ($DiagnosticsAbs -ne "" -and (Test-Path $DiagnosticsAbs)) {
    Write-Step "Diagnostics written to: $DiagnosticsAbs"
    Get-ChildItem $DiagnosticsAbs | ForEach-Object { Write-Step "  $_" }
}

exit 0