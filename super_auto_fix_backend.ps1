Write-Host "`n--- SUPER AUTO FIX SCRIPT: SmartBiz Backend ---" -ForegroundColor Cyan

# 1. Tengeneza venv kama haipo
$venvActivate = ".\venv\Scripts\Activate.ps1"
if (!(Test-Path $venvActivate)) {
    Write-Host "`n[INFO] Hakuna venv. Inaundwa sasa..." -ForegroundColor Yellow
    python -m venv venv
    if (!(Test-Path $venvActivate)) {
        Write-Host "[ERROR] Imeshindwa kuunda venv. Hakikisha Python iko kwenye PATH." -ForegroundColor Red
        exit
    }
    Write-Host "[OK] venv imeundwa." -ForegroundColor Green
}

# 2. Activate venv
Write-Host "`n[OK] Inafanya activation ya venv..." -ForegroundColor Green
. $venvActivate

# 3. Hakikisha requirements.txt ipo
$reqPath = Join-Path $PWD "requirements.txt"
if (!(Test-Path $reqPath)) {
    Write-Host "`n[ERROR] Hakuna 'requirements.txt'. Tengeneza kwa kutumia: pip freeze > requirements.txt" -ForegroundColor Red
    exit
}

# 4. Soma kila line ya requirements.txt na install kama haipo
Write-Host "`n[INFO] Kukagua na kuinstall modules zote zinazohitajika..." -ForegroundColor Cyan
$lines = Get-Content $reqPath | Where-Object { $_ -and ($_ -notmatch "^#") }

foreach ($line in $lines) {
    $package = $line -replace '==.*', '' -replace '\[.*?\]', '' -replace '~=.*', '' -replace '>=.*', '' -replace '<.*', ''
    $show = pip show $package 2>&1
    if ($show -like "*WARNING: Package(s) not found:*") {
        Write-Host "[INSTALL] $package haipo. Inawekwa sasa..." -ForegroundColor Yellow
        pip install $line
    } else {
        Write-Host "[OK] $package tayari ipo." -ForegroundColor Green
    }
}

# 5. Hakikisha main.py ipo
$mainFile = Join-Path $PWD "main.py"
if (!(Test-Path $mainFile)) {
    Write-Host "`n[ERROR] main.py haijapatikana kwenye folder hili!" -ForegroundColor Red
    exit
}

# 6. Run backend
Write-Host "`n[RUN] Kuanza backend kupitia venv yako..." -ForegroundColor Cyan
$venvPython = ".\venv\Scripts\python.exe"
& $venvPython -m uvicorn main:app --reload

Write-Host "`n[SUCCESS] Backend iko hewani! Kama kuna shida, angalia main.py au logs zako." -ForegroundColor Magenta
