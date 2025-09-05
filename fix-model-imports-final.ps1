# fix-model-imports-final.ps1
param(
  [string]$Root = "E:\SmartBiz_Assistance\backend"
)

Write-Host "Final resolving of leftover barrel imports..." -ForegroundColor Cyan

$ModelsDir = Join-Path $Root "models"
if (-not (Test-Path $ModelsDir)) { throw "Models dir not found: $ModelsDir" }

# Build Class->module index from models/*.py
$reClass = [regex]'(?m)^[ \t]*class[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]*\('
$Index = @{}
Get-ChildItem -Path $ModelsDir -Filter *.py | Where-Object { $_.Name -notmatch '^_' } | ForEach-Object {
  $mod = [IO.Path]::GetFileNameWithoutExtension($_.Name)
  $txt = Get-Content -LiteralPath $_.FullName -Raw -ErrorAction SilentlyContinue
  if ($null -eq $txt) { return }
  foreach ($m in $reClass.Matches($txt)) {
    $cls = $m.Groups[1].Value
    if (-not $Index.ContainsKey($cls)) { $Index[$cls] = $mod }
  }
}

# Aliased fixes: common misnames -> {RealClass, Module}
$AliasFix = @{
  # very likely renames
  "Recharge"           = @{ Class="RechargeTransaction"; Module="recharge_transaction" }
  "PlatformConnection" = @{ Class="ConnectedPlatform";   Module="connected_platform" }
  "ReferralClick"      = @{ Class="ReferralLog";         Module="referral_log" }
  "SupportMessage"     = @{ Class="SupportTicket";       Module="support" }
  # add any more as we confirm
}

function Make-Imports([string[]]$Items) {
  $out = @()
  foreach ($raw in $Items) {
    $piece = ($raw -as [string])
    if (-not $piece) { continue }
    $piece = ($piece -split '#',2)[0]
    $piece = ($piece -replace '\s+', ' ').Trim(' ',',')
    if (-not $piece) { continue }

    $m = [regex]::Match($piece, '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:as\s+([A-Za-z_][A-Za-z0-9_]*))?\s*$')
    if (-not $m.Success) { continue }
    $name  = $m.Groups[1].Value
    $alias = $m.Groups[2].Value

    $cls = $name
    $mod = $null

    if ($Index.ContainsKey($name)) {
      $mod = $Index[$name]
    } elseif ($AliasFix.ContainsKey($name)) {
      $cls = $AliasFix[$name].Class
      $mod = $AliasFix[$name].Module
    } else {
      # try snake_case guess
      $guess = ($name -replace '([a-z0-9])([A-Z])','$1_$2').ToLower()
      $guessPath = Join-Path $ModelsDir ($guess + ".py")
      if (Test-Path $guessPath) {
        # ensure class exists inside guess
        $txt = Get-Content -LiteralPath $guessPath -Raw
        if ($txt -match ("(?m)^\s*class\s+" + [regex]::Escape($name) + "\s*\(")) {
          $mod = $guess
        }
      }
    }

    if ($mod) {
      if ($alias) {
        $out += "from backend.models.$mod import $cls as $alias"
      } else {
        $out += "from backend.models.$mod import $cls"
      }
    } else {
      # leave original if unresolved
      if ($alias) { $out += "from backend.models import $name as $alias" }
      else        { $out += "from backend.models import $name" }
    }
  }
  return $out
}

$reGrouped = [regex]'^(?<indent>[ \t]*)from\s+backend\.models\s+import\s*\((?<inner>[\s\S]*?)\)\s*$'
$reSingle  = [regex]'^(?<indent>[ \t]*)from[ \t]+backend\.models[ \t]+import[ \t]+(?<items>.+?)\s*$'

$targets = Get-ChildItem -Path $Root -Recurse -Filter *.py |
  Where-Object { $_.FullName -notmatch '\\venv\\|\\__pycache__\\|\\alembic\\versions\\' } |
  Where-Object { (Get-Content -LiteralPath $_.FullName -Raw) -match 'from\s+backend\.models\s+import\s+' }

$fixed = 0
foreach ($f in $targets) {
  $text = Get-Content -LiteralPath $f.FullName -Raw
  $orig = $text

  $text = [regex]::Replace($text, $reGrouped, {
    param($m)
    $indent = $m.Groups['indent'].Value
    $inner  = $m.Groups['inner'].Value
    $items  = @()
    foreach ($p in ($inner -split ',')) {
      $v = ($p -replace '\r|\n',' ') -replace '\s+', ' '
      $v = $v.Trim(' ',',')
      if ($v) { $items += $v }
    }
    $lines = Make-Imports $items
    ($lines | ForEach-Object { $indent + $_ }) -join "`n"
  }, 'Singleline, Multiline')

  $text = [regex]::Replace($text, $reSingle, {
    param($m)
    $indent = $m.Groups['indent'].Value
    $items  = $m.Groups['items'].Value.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ }
    $lines  = Make-Imports $items
    ($lines | ForEach-Object { $indent + $_ }) -join "`n"
  }, 'Multiline')

  if ($text -ne $orig) {
    Set-Content -LiteralPath $f.FullName -Value $text -Encoding UTF8
    $fixed++
    Write-Host ("Fixed: " + $f.FullName) -ForegroundColor Green
  }
}

Write-Host ("Done. Patched files: {0}" -f $fixed) -ForegroundColor Cyan

# Show any leftovers to fix manually
$left = Get-ChildItem -Path $Root -Recurse -Filter *.py |
  Where-Object { $_.FullName -notmatch '\\venv\\|\\__pycache__\\|\\alembic\\versions\\' } |
  ForEach-Object { $t = Get-Content -LiteralPath $_.FullName -Raw; if ($t -match 'from\s+backend\.models\s+import\s+') { $_.FullName } }

if ($left) {
  Write-Host "Leftover barrel imports (manual check):" -ForegroundColor Yellow
  $left | ForEach-Object { Write-Host $_ -ForegroundColor Yellow }
} else {
  Write-Host "No barrel imports remain. ✅" -ForegroundColor Green
}
