param(
  [string]$UserFile    = "backend\models\user.py",
  [string]$CoHostFile  = "backend\models\co_host.py",
  [string]$ModelsInit  = "backend\models\__init__.py",
  [switch]$Apply
)

function Read-Text($path) {
  if (!(Test-Path $path)) { throw ("File not found: {0}" -f $path) }
  Get-Content $path -Raw
}
function Backup-Once($path, $ext) {
  if (!(Test-Path ($path + $ext))) { Copy-Item $path ($path + $ext) }
}
function Save-Text($path, $text) { Set-Content -Path $path -Value $text -Encoding UTF8 }

# 1) Pata jina la class ndani ya co_host.py (mf. class CoHost(Base):)
$coText = Read-Text $CoHostFile
$m = [regex]::Match($coText, 'class\s+(\w+)\(Base\)\s*:', 'Singleline')
if (-not $m.Success) { throw ("Sikupata 'class <Name>(Base):' ndani ya {0}" -f $CoHostFile) }
$CoHostClass = $m.Groups[1].Value
Write-Host ("Detected class in co_host.py -> {0}" -f $CoHostClass)

# 2) Rekebisha user.py
$uText = Read-Text $UserFile
$before = $uText

# 2a) Badilisha target & annotations "CoHost" -> "$CoHostClass"
$uText = $uText -replace 'Mapped\[list\["CoHost"\]\]', ("Mapped[list[`"{0}`"]]" -f $CoHostClass)
$uText = $uText -replace 'relationship\(\s*"CoHost"\s*,', ("relationship(`"{0}`"," -f $CoHostClass)

# 2b) Badilisha foreign_keys lambda -> string
$uText = [regex]::Replace(
  $uText,
  'foreign_keys\s*=\s*lambda:\s*\[\s*CoHost\.(\w+)\s*\]',
  { param($m2) ("foreign_keys=`"{0}.{1}`"" -f $CoHostClass, $m2.Groups[1].Value) },
  'Singleline'
)

# 2c) Safisha mabaki ya "CoHost."
$uText = $uText -replace 'CoHost\.', ($CoHostClass + '.')

if ($uText -ne $before) {
  Backup-Once $UserFile ".cohostnamefix.bak"
  Save-Text $UserFile $uText
  Write-Host ("Patched {0}" -f $UserFile)
} else {
  Write-Host "No changes needed in user.py"
}

# 3) Hakikisha models/__init__.py ina import hiyo class
if (!(Test-Path $ModelsInit)) {
  New-Item -ItemType File -Path $ModelsInit -Force | Out-Null
}
$initText = Read-Text $ModelsInit
$importLine = ("from .co_host import {0}" -f $CoHostClass)
if ($initText -notmatch [regex]::Escape($importLine)) {
  Backup-Once $ModelsInit ".cohostnamefix.bak"
  $initText = $initText.TrimEnd() + "`n" + $importLine + "`n"
  Save-Text $ModelsInit $initText
  Write-Host ("Ensured import in {0} -> {1}" -f $ModelsInit, $importLine)
}

if ($Apply) {
  Write-Host "DONE. Backups created: *.cohostnamefix.bak" -ForegroundColor Green
} else {
  Write-Host "DRY-RUN complete (use -Apply to write changes)." -ForegroundColor Yellow
}
