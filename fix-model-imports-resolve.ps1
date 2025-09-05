param(
  [string]$Root = "E:\SmartBiz_Assistance\backend"
)

Write-Host "Resolving remaining barrel imports under $Root ..." -ForegroundColor Cyan

$ModelsDir = Join-Path $Root "models"
if (-not (Test-Path $ModelsDir)) { throw "Models dir not found: $ModelsDir" }

# Regex helpers
$reClassDef   = [regex]'(?m)^[ \t]*class[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]*\('
$reSingleLine = [regex]'^(?<indent>[ \t]*)from[ \t]+backend\.models[ \t]+import[ \t]+(?<items>.+?)\s*$'
$reGrouped    = [regex]'^(?<indent>[ \t]*)from\s+backend\.models\s+import\s*\((?<inner>[\s\S]*?)\)\s*$'

# ---> Build a fast index of Class -> module by scanning models/*.py
$Index = @{}
Get-ChildItem -Path $ModelsDir -Filter *.py | Where-Object { $_.Name -notmatch '^_' } | ForEach-Object {
  $module = [IO.Path]::GetFileNameWithoutExtension($_.Name)
  $txt = Get-Content -LiteralPath $_.FullName -Raw -ErrorAction SilentlyContinue
  if ($null -eq $txt) { return }
  foreach ($m in $reClassDef.Matches($txt)) {
    $cls = $m.Groups[1].Value
    if (-not $Index.ContainsKey($cls)) { $Index[$cls] = $module }
  }
}

function Resolve-ItemsToImports {
  param([string[]]$Items)
  $out = @()
  foreach ($raw in $Items) {
    $piece = ($raw -as [string])
    if (-not $piece) { continue }
    # toa comments na takataka
    $piece = ($piece -split '#',2)[0]
    $piece = ($piece -replace '\s+', ' ').Trim(' ',',')
    if (-not $piece) { continue }

    # support alias: "Name as Alias"
    $m = [regex]::Match($piece, '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:as\s+([A-Za-z_][A-Za-z0-9_]*))?\s*$')
    if (-not $m.Success) { continue }
    $name  = $m.Groups[1].Value
    $alias = $m.Groups[2].Value

    $module = $null
    if ($Index.ContainsKey($name)) {
      $module = $Index[$name]
    } else {
      # fallback: on-demand search (in case class wasn’t indexed earlier)
      $hit = Get-ChildItem -Path $ModelsDir -Filter *.py |
             Select-String -Pattern ("(?m)^\s*class\s+" + [regex]::Escape($name) + "\s*\(") -SimpleMatch |
             Select-Object -First 1
      if ($hit) {
        $module = [IO.Path]::GetFileNameWithoutExtension($hit.Path)
        $Index[$name] = $module
      }
    }

    if ($module) {
      if ($alias) {
        $out += "from backend.models.$module import $name as $alias"
      } else {
        $out += "from backend.models.$module import $name"
      }
    } else {
      # leave original if unresolved
      if ($alias) {
        $out += "from backend.models import $name as $alias"
      } else {
        $out += "from backend.models import $name"
      }
    }
  }
  return $out
}

# Gather files that still have barrel imports
$targets = Get-ChildItem -Path $Root -Recurse -Filter *.py |
  Where-Object { $_.FullName -notmatch '\\venv\\|\\__pycache__\\|\\alembic\\versions\\' } |
  Where-Object {
    (Get-Content -LiteralPath $_.FullName -Raw) -match 'from\s+backend\.models\s+import\s+'
  }

[int]$fixed = 0
foreach ($f in $targets) {
  $text = Get-Content -LiteralPath $f.FullName -Raw
  $orig = $text

  # 1) Grouped form
  $text = [regex]::Replace($text, $reGrouped,
    {
      param($m)
      $indent = $m.Groups['indent'].Value
      $inner  = $m.Groups['inner'].Value
      $items  = @()
      foreach ($p in ($inner -split ',')) {
        $v = ($p -replace '\r|\n',' ') -replace '\s+', ' '
        $v = $v.Trim(' ',',')
        if ($v) { $items += $v }
      }
      $lines = Resolve-ItemsToImports -Items $items
      ($lines | ForEach-Object { $indent + $_ }) -join "`n"
    },
    'Singleline, Multiline'
  )

  # 2) Single line form
  $text = [regex]::Replace($text, $reSingleLine,
    {
      param($m)
      $indent = $m.Groups['indent'].Value
      $items  = $m.Groups['items'].Value.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ }
      $lines  = Resolve-ItemsToImports -Items $items
      ($lines | ForEach-Object { $indent + $_ }) -join "`n"
    },
    'Multiline'
  )

  if ($text -ne $orig) {
    Set-Content -LiteralPath $f.FullName -Value $text -Encoding UTF8
    $fixed++
    Write-Host ("Resolved: " + $f.FullName) -ForegroundColor Green
  }
}

Write-Host ("Done. Resolved files: {0}" -f $fixed) -ForegroundColor Cyan
