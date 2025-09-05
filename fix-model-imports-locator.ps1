param(
  [string]$Root = "E:\SmartBiz_Assistance\backend"
)

Write-Host "Locator+Fixer: resolving remaining 'from backend.models import ...'" -ForegroundColor Cyan

$ModelsDir = Join-Path $Root "models"
if (-not (Test-Path $ModelsDir)) { throw "Models dir not found: $ModelsDir" }

# 1) Kusanya mistari yote iliyobaki ya barrel-import
$targets = Get-ChildItem -Path $Root -Recurse -Filter *.py |
  Where-Object { $_.FullName -notmatch '\\venv\\|\\__pycache__\\|\\alembic\\versions\\' } |
  ForEach-Object {
    $t = Get-Content -LiteralPath $_.FullName -Raw
    if ($t -match 'from\s+backend\.models\s+import\s+') { $_.FullName }
  } | Sort-Object -Unique

if (-not $targets) {
  Write-Host "No remaining barrel imports. ✅" -ForegroundColor Green
  exit 0
}

# 2) Msaidizi: tafuta 'class Name' kwenye repo nzima na urudishe module ikiwa ipo ndani ya backend/models
function Find-ClassModule {
  param([string]$ClassName)
  $regex = "(?m)^\s*class\s+" + [regex]::Escape($ClassName) + "\s*(?:[\(:])"
  $hits = Get-ChildItem -Path $Root -Recurse -Filter *.py |
          Where-Object { $_.FullName -notmatch '\\venv\\|\\__pycache__\\' } |
          Select-String -Pattern $regex -SimpleMatch:$false
  if (-not $hits) { return @{ Found=$false } }

  # chukua hit ya kwanza
  $hit = $hits | Select-Object -First 1
  $path = $hit.Path
  $inModels = $path -like (Join-Path $ModelsDir "*")
  if ($inModels) {
    $modName = [IO.Path]::GetFileNameWithoutExtension($path)
    return @{ Found=$true; InModels=$true; Module=$modName; File=$path }
  } else {
    # ipo nje ya models (mf schemas)
    return @{ Found=$true; InModels=$false; File=$path }
  }
}

# 3) Fanya replacements kwa kila faili
$reSingle  = [regex]'^(?<indent>[ \t]*)from[ \t]+backend\.models[ \t]+import[ \t]+(?<items>.+?)\s*$'
$reGrouped = [regex]'^(?<indent>[ \t]*)from\s+backend\.models\s+import\s*\((?<inner>[\s\S]*?)\)\s*$'

$report = @()

function Build-Lines {
  param([string[]]$items, [string]$filePath)

  $out = @()
  foreach ($raw in $items) {
    $piece = ($raw -split '#',2)[0]
    $piece = ($piece -replace '\s+', ' ').Trim(' ',',')
    if (-not $piece) { continue }

    $m = [regex]::Match($piece, '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:as\s+([A-Za-z_][A-Za-z0-9_]*))?\s*$')
    if (-not $m.Success) { continue }
    $name  = $m.Groups[1].Value
    $alias = $m.Groups[2].Value

    $res = Find-ClassModule $name
    if ($res.Found -and $res.InModels) {
      $line = "from backend.models.$($res.Module) import $name"
      if ($alias) { $line += " as $alias" }
      $out += $line
      $report += [pscustomobject]@{ File=$filePath; Class=$name; Action="fixed"; Module=$res.Module }
    } else {
      # acha mstari (tunaongeza kwenye report tu)
      $keep = "from backend.models import $name"
      if ($alias) { $keep += " as $alias" }
      $out += $keep
      $action = if (-not $res.Found) { "NOT_FOUND" } elseif (-not $res.InModels) { "FOUND_OUTSIDE_MODELS" } else { "UNKNOWN" }
      $report += [pscustomobject]@{ File=$filePath; Class=$name; Action=$action; Module=($res.Module) ; Where=($res.File) }
    }
  }
  return $out
}

[int]$fixed = 0
foreach ($file in $targets) {
  $text = Get-Content -LiteralPath $file -Raw
  $orig = $text

  # grouped form: from backend.models import (A, B as X, C)
  $text = [regex]::Replace($text, $reGrouped, {
    param($m)
    $indent = $m.Groups['indent'].Value
    $inner  = $m.Groups['inner'].Value
    $items  = ($inner -split ',') | ForEach-Object { ($_ -replace '\r|\n',' ') -replace '\s+', ' ' } | ForEach-Object { $_.Trim(' ',',') } | Where-Object { $_ }
    $lines  = Build-Lines -items $items -filePath $file
    ($lines | ForEach-Object { $indent + $_ }) -join "`n"
  }, 'Singleline, Multiline')

  # single-line form
  $text = [regex]::Replace($text, $reSingle, {
    param($m)
    $indent = $m.Groups['indent'].Value
    $items  = $m.Groups['items'].Value.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ }
    $lines  = Build-Lines -items $items -filePath $file
    ($lines | ForEach-Object { $indent + $_ }) -join "`n"
  }, 'Multiline')

  if ($text -ne $orig) {
    Set-Content -LiteralPath $file -Value $text -Encoding UTF8
    $fixed++
    Write-Host ("Fixed (where possible): " + $file) -ForegroundColor Green
  }
}

Write-Host ("Done. Files touched: {0}" -f $fixed) -ForegroundColor Cyan

# 4) Onyesha ripoti fupi ya walioshindikana
$left = $report | Where-Object { $_.Action -ne "fixed" } | Sort-Object File, Class
if ($left) {
  Write-Host "`n== Review these ==" -ForegroundColor Yellow
  $left | Format-Table -AutoSize
} else {
  Write-Host "All barrel imports resolved. ✅" -ForegroundColor Green
}
