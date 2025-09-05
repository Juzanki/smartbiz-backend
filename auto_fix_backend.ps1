Write-Host "`n🔧 AUTO FIX SCRIPT: SmartBiz Backend Environment" -ForegroundColor Cyan

# 1. Angalia kama venv ipo
$venvActivate = Join-Path $PWD "venv\Scripts\Activate.ps1"
if (!(Test-Path $venvActivate)) {
    Write-Host "`n❌ Virtual environment haijapatikana (venv). Tafadhali tengeneza kwa kutumia 'python -m venv venv'" -ForegroundColor Red
    exit
}

# 2. Activate venv
Write-Host "`n✅ Inafanya activation ya venv..." -ForegroundColor Green
. $venvActivate

# 3. Hakikisha fastapi ipo
Write-Host "`n🔍 Kukagua kama 'fastapi' ipo..." -ForegroundColor Cyan
$fastapiCheck = python -c "import fastapi" 2>&1
if ($fastapiCheck -like "*ModuleNotFoundError*") {
    Write-Host "⚠️ 'fastapi' haipo. Inawekwa sasa..." -ForegroundColor Yellow
    pip install fastapi
} else {
    Write-Host "✅ 'fastapi' tayari ipo." -ForegroundColor Green
}

# 4. Hakikisha uvicorn ipo
Write-Host "`n🔍 Kukagua kama 'uvicorn' ipo..." -ForegroundColor Cyan
$uvicornCheck = python -c "import uvicorn" 2>&1
if ($uvicornCheck -like "*ModuleNotFoundError*") {
    Write-Host "⚠️ 'uvicorn' haipo. Inawekwa sasa..." -ForegroundColor Yellow
    pip install uvicorn[standard]
} else {
    Write-Host "✅ 'uvicorn' tayari ipo." -ForegroundColor Green
}

# 5. Hakikisha apscheduler ipo
Write-Host "`n🔍 Kukagua kama 'apscheduler' ipo..." -ForegroundColor Cyan
$apschedulerCheck = python -c "import apscheduler" 2>&1
if ($apschedulerCheck -like "*ModuleNotFoundError*") {
    Write-Host "⚠️ 'apscheduler' haipo. Inawekwa sasa..." -ForegroundColor Yellow
    pip install apscheduler
} else {
    Write-Host "✅ 'apscheduler' tayari ipo." -ForegroundColor Green
}

# 6. Angalia kama main.py ipo
$mainPath = Join-Path $PWD "main.py"
if (!(Test-Path $mainPath)) {
    Write-Host "`n❌ main.py haijapatikana katika folder hili!" -ForegroundColor Red
    exit
}

# 7. Run backend
Write-Host "`n🚀 Kuanza backend kupitia venv yako..." -ForegroundColor Cyan
$venvPython = ".\venv\Scripts\python.exe"
& $venvPython -m uvicorn main:app --reload

Write-Host "`n✅ Kazi imekamilika. Backend yako iko Live kama kila kitu kiko sawa." -ForegroundColor Magenta
