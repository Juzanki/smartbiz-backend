Write-Host "`n🔍 Kukagua Python environment na module 'apscheduler'..." -ForegroundColor Cyan

# 1. Angalia Python path (sahihi kwa PowerShell)
$pythonPath = python -c "import sys; print(sys.executable)"
Write-Host "`n🧠 Python inatumika: $pythonPath" -ForegroundColor Yellow

# 2. Angalia kama uko kwenye virtual environment
if ($pythonPath -like "*venv*") {
    Write-Host "✅ Uko kwenye virtual environment sahihi." -ForegroundColor Green
} else {
    Write-Host "❌ HAUPO kwenye virtualenv ya backend! Tafadhali run '.\venv\Scripts\activate'" -ForegroundColor Red
}

# 3. Jaribu ku-import apscheduler
Write-Host "`n🔎 Kukagua kama 'apscheduler' ipo..." -ForegroundColor Cyan
try {
    $apschedulerTest = python -c "import apscheduler; print('✅ apscheduler ipo')"
    Write-Host "$apschedulerTest" -ForegroundColor Green
} catch {
    Write-Host "❌ apscheduler haipo kwenye environment hii!" -ForegroundColor Red
}

# 4. Angalia kama main.py ipo
$mainPath = Join-Path $PWD "main.py"
if (Test-Path $mainPath) {
    Write-Host "`n📄 main.py imepatikana katika path: $mainPath" -ForegroundColor Green
    Write-Host "✅ Unaweza sasa kujaribu: uvicorn main:app --reload" -ForegroundColor Cyan
} else {
    Write-Host "`n⚠️ main.py haipo hapa. Je iko ndani ya folda nyingine?" -ForegroundColor Red
}

Write-Host "`n✅ Ukihitaji script ya kurekebisha haya yote kiotomatiki (auto-fix), niambie." -ForegroundColor Magenta
