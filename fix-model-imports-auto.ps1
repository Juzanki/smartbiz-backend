# fix-model-imports-auto.ps1
param(
  [string]$Root = "E:\SmartBiz_Assistance\backend"
)

Write-Host "Auto-mapping models and fixing imports under $Root ..." -ForegroundColor Cyan

# 1) Jenga ramani: ClassName -> module (kwa kuscan backend\models\*.py)
$ModelsDir = Join-Path $Root "models"
if (-not (Test-Path $ModelsDir)) { throw "Models directory not found: $ModelsDir" }

# regex ya kupata class definitions: class Name(Base):
$clsRegex = [regex]'(?m)^[ \t]*class[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]*\('
$ClassToModule = @{}

Get-ChildItem -Path $ModelsDir -Filter *.py |
  Where-Object { $_.Name -notmatch '^_' } |
  ForEach-Object {
    $modName = [System.IO.Path]::GetFileNameWithoutExtension($_.Name)
    $txt = Get-Content -LiteralPath $_.FullName -Raw -ErrorAction SilentlyContinue
    if ($null -eq $txt) { return }
    foreach ($m in $clsRegex.Matches($txt)) {
      $class = $m.Groups[1].Value
      # weka ramani kama haipo tayari (kama kuna duplicate, acha ya kwanza)
      if (-not $ClassToModule.ContainsKey($class)) {
        $ClassToModule[$class] = $modName
      }
    }
  }

# 2) Helper: tengeneza mistari mipya (inashika alias "as Foo" pia)
function New-ImportLines {
  param([string[]]$Items)

  $out = @()
  foreach ($raw in $Items) {
    $piece = ($raw -as [string]).Trim()
    if (-not $piece) { continue }

    # toa comments za mwisho kama zipo
    $piece = ($piece -split '#',2)[0].Trim()

    # tambua alias: "Name as Alias"
    $aliasMatch = [regex]::Match($piece, '^\s*([A-Za-z_][A-Za-z0-9_]*)\s+(?:as)\s+([A-Za-z_][A-Za-z0-9_]*)\s*$')
    if ($aliasMatch.Success) {
      $name  = $aliasMatch.Groups[1].Value
      $alias = $aliasMatch.Groups[2].Value
    } else {
      # inaweza kuwa "Name" tu
      $name  = ($piece -replace '[\s,]','')
      $alias = $null
    }

    if ($ClassToModule.ContainsKey($name)) {
      $mod = $ClassToModule[$name]
      if ($alias) {
        $out += "from backend.models.$mod import $name as $alias"
      } else {
        $out += "from backend.models.$mod import $name"
      }
    } else {
      # Kama haijapatikana module, acha mstari wa zamani kwa usalama
      if ($alias) {
        $out += "from backend.models import $name as $alias"
      } else {
        $out += "from backend.models import $name"
      }
    }
  }
  return ($out -join "`n")
}

# 3) Patterns za kubadilisha
$patternSingle = '^(?<indent>[ \t]*)from[ \t]+backend\.models[ \t]+import[ \t]+(?<item>[A-Za-z_][A-Za-z0-9_]*\s*(?:as\s*[A-Za-z_][A-Za-z0-9_]*)?)\s*(?:#.*)?$'
$patternMulti  = '^(?<indent>[ \t]*)from\s+backend\.models\s+import\s*\((?<inner>[\s\S]*?)\)\s*$'

[int]$touched = 0
[int]$errors  = 0

# 4) Pitisha .py zote (epuka venv/__pycache__/alembic/versions)
$files = Get-ChildItem -Path $Root -Recurse -Filter *.py |
  Where-Object { $_.FullName -notmatch '\\venv\\|\\__pycache__\\|\\alembic\\versions\\' }

foreach ($f in $files) {
  try { $text = Get-Content -LiteralPath $f.FullName -Raw -ErrorAction Stop } catch {
    Write-Host "Read error: $($f.FullName)" -ForegroundColor DarkYellow
    continue
  }
  if ($null -eq $text) { continue }
  $orig = $text

  # --- grouped "(A, B as X, C)" (tumia Singleline + ushike indent) ---
  $text = [regex]::Replace($text, $patternMulti,
    {
      param($m)
      $indent = $m.Groups['indent'].Value
      $inner  = $m.Groups['inner'].Value

      # gawanya kwa comma, safisha whitespaces/newlines
      $items = @()
      foreach ($p in ($inner -split ',')) {
        $val = ($p -replace '\r|\n',' ').Trim()
        if ($val) { $items += $val }
      }

      $new = New-ImportLines -Items $items
      # hifadhi indentation
      $new -split "`n" | ForEach-Object { $indent + $_ } | Out-String
    },
    'Singleline, Multiline'
  )

  # --- single "from backend.models import Name [as Alias]" (hifadhi indent) ---
  $text = [regex]::Replace($text, $patternSingle,
    {
      param($m)
      $indent = $m.Groups['indent'].Value
      $item   = $m.Groups['item'].Value
      $new = New-ImportLines -Items @($item)
      ($new -split "`n" | ForEach-Object { $indent + $_ }) -join "`n"
    },
    'Multiline'
  )

  if ($text -ne $orig) {
    try {
      Set-Content -LiteralPath $f.FullName -Value $text -Encoding UTF8
      $touched++
      Write-Host ("Fixed: " + $f.FullName) -ForegroundColor Green
    } catch {
      $errors++
      Write-Host "Write error: $($f.FullName) -> $($_.Exception.Message)" -ForegroundColor Red
    }
  }
}

Write-Host ("Done. Files fixed: {0}, Errors: {1}" -f $touched, $errors) -ForegroundColor Cyan
