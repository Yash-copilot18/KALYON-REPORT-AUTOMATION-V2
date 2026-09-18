# setup_backend.ps1  -  Kalyon Nigde 130 MW backend bootstrap
#
# One command to (re)create the virtual environment, install dependencies, and start the API.
# Run from the Backend folder:
#
#     cd C:\Users\servicescada32\Desktop\Kalyon-Nigde130\KALYON-REPORT-AUTOMATION\Backend
#     powershell -ExecutionPolicy Bypass -File .\setup_backend.ps1
#
# Options:
#     -NoRun      Set up the venv + install deps, but do NOT start uvicorn.
#     -Recreate   Force-delete an existing venv and rebuild it from scratch.
#
# IMPORTANT (SentinelOne): this machine's EDR (mitigation policy "remediateThreat") has been
# deleting Python's standard library seconds after install. The script verifies Python is
# actually healthy BEFORE doing anything and stops with a clear message if it is not, so it
# never builds a venv on top of a broken interpreter.

[CmdletBinding()]
param(
    [switch]$NoRun,
    [switch]$Recreate
)

# NOT 'Stop': under 'Stop' a native command (python/pip) writing to stderr throws a
# NativeCommandError and aborts unpredictably. We check $LASTEXITCODE explicitly instead.
$ErrorActionPreference = 'Continue'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

function Write-Step($m) { Write-Host ""; Write-Host ("==> " + $m) -ForegroundColor Cyan }
function Write-Ok($m)   { Write-Host ("    " + $m) -ForegroundColor Green }
function Write-Err($m)  { Write-Host ("    " + $m) -ForegroundColor Red }

# ---- 1. Locate a Python 3.13 interpreter ------------------------------------
Write-Step "Locating Python 3.13"
$py = $null
$candidates = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313-64\python.exe")
)
foreach ($c in $candidates) { if (Test-Path $c) { $py = $c; break } }
if (-not $py) {
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) { try { $py = (& py -3.13 -c "import sys; print(sys.executable)") 2>$null } catch { } }
}
if (-not $py -or -not (Test-Path $py)) {
    Write-Err "No Python 3.13 interpreter found. Install Python 3.13 from python.org first."
    exit 1
}
Write-Ok ("Found: " + $py)

# ---- 2. Verify the stdlib is intact (not stripped by SentinelOne) -----------
Write-Step "Verifying Python standard library is intact"
$pyRoot = Split-Path -Parent $py
$mustExist = @("Lib\os.py", "Lib\encodings\__init__.py", "DLLs\_socket.pyd")
$missing = @($mustExist | Where-Object { -not (Test-Path (Join-Path $pyRoot $_)) })

if ($missing.Count -gt 0) {
    Write-Err ("Python is present at: " + $py)
    Write-Err "...but its standard library is BROKEN / has been removed."
    Write-Err ("Missing: " + ($missing -join ", "))
    Write-Err ""
    Write-Err "This is the known SentinelOne 'remediateThreat' behaviour - the Lib and DLLs files"
    Write-Err "are deleted after install. The backend cannot run until IT adds a SentinelOne path"
    Write-Err ("exclusion for: " + $pyRoot)
    Write-Err "then reinstalls Python 3.13. Re-run this script once this works and STAYS working:"
    Write-Err "    py -3.13 -c `"import encodings; print('ok')`""
    exit 1
}

$import = (& $py -c "import encodings, ssl, sqlite3, ctypes; print('HEALTHY')" 2>&1) -join "`n"
if ($import -notmatch "HEALTHY") {
    Write-Err "Python stdlib files exist but the interpreter failed to import them:"
    Write-Err ("    " + (($import -split "`n") | Select-Object -First 1))
    Write-Err "Reinstall Python 3.13 (with the SentinelOne exclusion in place), then re-run."
    exit 1
}
Write-Ok "Standard library OK."
& $py --version | ForEach-Object { Write-Ok $_ }

# ---- 3. (Re)create the virtual environment ----------------------------------
$venv = Join-Path $ScriptDir "venv"
$venvPy = Join-Path $venv "Scripts\python.exe"
if ($Recreate -and (Test-Path $venv)) {
    Write-Step "Removing existing venv (-Recreate)"
    Remove-Item -Recurse -Force $venv
}
if (-not (Test-Path $venvPy)) {
    Write-Step ("Creating virtual environment: " + $venv)
    if (Test-Path $venv) { Remove-Item -Recurse -Force $venv }
    & $py -m venv $venv
    if (-not (Test-Path $venvPy)) { Write-Err "venv creation failed."; exit 1 }
    Write-Ok "Created."
} else {
    Write-Ok "Reusing existing venv."
}

# ---- 4. Install dependencies ------------------------------------------------
Write-Step "Upgrading pip"
& $venvPy -m pip install --upgrade pip --quiet
Write-Ok "pip ready."

Write-Step "Installing requirements.txt"
$req = Join-Path $ScriptDir "requirements.txt"
if (-not (Test-Path $req)) { Write-Err "requirements.txt not found next to this script."; exit 1 }
& $venvPy -m pip install -r $req
if ($LASTEXITCODE -ne 0) { Write-Err "Dependency install failed (see output above)."; exit 1 }
Write-Ok "Dependencies installed."

# ---- 5. App-import sanity check (does not start the server) ------------------
Write-Step "Sanity-checking the app import"
$imp = (& $venvPy -c "import main; print('app import OK')" 2>&1) -join "`n"
if ($imp -notmatch "app import OK") {
    Write-Err "App import failed (likely a config/.env or DB-driver issue, NOT the venv):"
    ($imp -split "`n") | ForEach-Object { Write-Err ("    " + $_) }
    Write-Err "Fix the above, then run:  .\venv\Scripts\Activate.ps1 ; uvicorn main:app --reload"
    exit 1
}
Write-Ok "app import OK"

# ---- 6. Launch the backend --------------------------------------------------
if ($NoRun) {
    Write-Step "Setup complete. -NoRun set, not starting the server."
    Write-Ok "To start it:  .\venv\Scripts\Activate.ps1 ; uvicorn main:app --reload"
    exit 0
}
Write-Step "Starting uvicorn. Press Ctrl+C to stop."
& $venvPy -m uvicorn main:app --reload
