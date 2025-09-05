param(
  [switch]$Apply,            # weka -Apply kuandika mabadiliko
  [switch]$CleanBackups      # (hiari) futa *.bak kabla ya kuandika
)

$Root = Join-Path $PSScriptRoot "..\models"
if (-not (Test-Path $Root)) { throw "Models directory not found: $Root" }

if ($CleanBackups) {
  Get-ChildItem $Root -Recurse -Filter *.bak | Remove-Item -Force -ErrorAction SilentlyContinue
  Write-Host "🧹 Deleted existing .bak files under $Root"
}

# --- vipande vya header tutakavyoweka/replace ---
$JsonHeader = @"
from sqlalchemy import JSON as SA_JSON
try:
    from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB  # type: ignore
    JSON_VARIANT = PG_JSONB
except Exception:  # pragma: no cover
    JSON_VARIANT = SA_JSON
"@

$NumericHeader = @"
from sqlalchemy import Numeric as SA_NUMERIC
try:
    from sqlalchemy.dialects.postgresql import NUMERIC as PG_NUMERIC  # type: ignore
    DECIMAL_TYPE = PG_NUMERIC(18, 2)
except Exception:  # pragma: no cover
    DECIMAL_TYPE = SA_NUMERIC(18, 2)
"@

# --- regex za kuondoa block mbovu ---
$BadJsonBlock = "(?s)try:\s*pass\s*`r?`nexcept\s+Exception:\s*#\s*auto-added\s+by\s+PS\s+fix.*?JSON_VARIANT\s*=\s*JSON\(\)\s*"
$BadNumericBlock = "(?s)try:\s*pass\s*`r?`nexcept\s+Exception:\s*#\s*auto-added\s+by\s+PS\s+fix.*?NUMERIC\s+as\s+PG_NUMERIC.*?except\s+Exception:.*?(PG_NUMERIC|Numeric)"

# tutatafuta pia import za JSON as-is ili tuwe na alias SA_JSON mara moja
$ImportJsonPlain = "from\s+sqlalchemy\s+import\s+JSON\b"
$NeedsJsonVariant = "(JSON_VARIANT\b|MutableDict\.as_mutable\(\s*JSON_VARIANT\s*\))"
$UsesPgNumeric = "PG_NUMERIC\s*\("
$UsesNumericType = "DECIMAL_TYPE\b"

$Files = Get-ChildItem $Root -Recurse -Filter *.py
if (-not $Files) { Write-Host "⚠️ No .py files found under $Root"; exit }

$changed = 0
foreach ($f in $Files) {
  $txt = Get-Content $f.FullName -Raw
  $orig = $txt
  $did = @()

  # 1) Ondoa block mbovu za JSON/NUMERIC
  if ($txt -match $BadJsonBlock) {
    $txt = [Regex]::Replace($txt, $BadJsonBlock, $JsonHeader)
    $did += "bad-json-block→fixed"
  }
  if ($txt -match $BadNumericBlock) {
    $txt = [Regex]::Replace($txt, $BadNumericBlock, $NumericHeader)
    $did += "bad-numeric-block→fixed"
  }

  # 2) Hakikisha tuna alias sahihi ya JSON_VARIANT tukiona inatumika
  if ($txt -match $NeedsJsonVariant -and $txt -notmatch "JSON_VARIANT\s*=") {
    # weka $JsonHeader mara baada ya imports ya juu (baada ya backend.db import au block ya imports)
    $insertionPoint = [Regex]::Match($txt, "(?m)^from\s+backend\.db\s+import\s+Base.*?$").Index
    if ($insertionPoint -eq 0 -and -not ($txt -match "(?m)^from\s+backend\.db\s+import\s+Base")) {
      # fallback: weka baada ya mwisho wa block ya 'from sqlalchemy' imports
      $m = [Regex]::Match($txt, "(?s)(from\s+sqlalchemy.*?)(?:`r?`n){2,}")
      if ($m.Success) { $insertionPoint = $m.Index + $m.Length } else { $insertionPoint = 0 }
    } else {
      $insertionPoint += [Regex]::Match($txt, "(?m)^from\s+backend\.db\s+import\s+Base.*?$").Length + 2
    }
    $txt = $txt.Insert($insertionPoint, "`r`n$JsonHeader`r`n")
    $did += "insert-json-header"
  }

  # 3) Kama kuna 'from sqlalchemy import JSON' badala ya alias, ibadilishe kuwa SA_JSON (hiari)
  if ($txt -match $ImportJsonPlain) {
    $txt = [Regex]::Replace($txt, $ImportJsonPlain, "from sqlalchemy import JSON as SA_JSON")
    $did += "normalize-json-import"
  }

  # 4) DECIMAL_TYPE: kama file linatumia PG_NUMERIC(...), ingiza NumericHeader kama halipo
  if ($txt -match $UsesPgNumeric) {
    if ($txt -notmatch "DECIMAL_TYPE\s*=") {
      # weka header mahali pazuri (baada ya JSON header kama upo, au baada ya imports za sqlalchemy)
      $anchor = [Regex]::Match($txt, "JSON_VARIANT\s*=").Success
      if ($anchor) {
        $txt = $txt -replace "(JSON_VARIANT\s*=.*?`r?`n)", "`$1$NumericHeader`r`n"
      } else {
        $m = [Regex]::Match($txt, "(?s)(from\s+sqlalchemy.*?)(?:`r?`n){2,}")
        if ($m.Success) {
          $txt = $txt.Insert($m.Index + $m.Length, "$NumericHeader`r`n")
        } else {
          $txt = "$NumericHeader`r`n$txt"
        }
      }
      $did += "insert-decimal-header"
    }
    # badilisha PG_NUMERIC(… , …) kuwa DECIMAL_TYPE
    $txt = [Regex]::Replace($txt, "PG_NUMERIC\s*\(\s*\d+\s*,\s*\d+\s*\)", "DECIMAL_TYPE")
    $did += "PG_NUMERIC→DECIMAL_TYPE"
  }

  # 5) Kama tayari kuna DECIMAL_TYPE lakini hakuna header, weka header
  if ($txt -match $UsesNumericType -and $txt -notmatch "DECIMAL_TYPE\s*=") {
    $m = [Regex]::Match($txt, "(?s)(from\s+sqlalchemy.*?)(?:`r?`n){2,}")
    if ($m.Success) {
      $txt = $txt.Insert($m.Index + $m.Length, "$NumericHeader`r`n")
    } else {
      $txt = "$NumericHeader`r`n$txt"
    }
    $did += "ensure-decimal-header"
  }

  if ($txt -ne $orig) {
    if ($Apply) {
      Copy-Item $f.FullName "$($f.FullName).bak" -Force
      Set-Content -Path $f.FullName -Value $txt -Encoding UTF8
      Write-Host "✅ Fixed $($f.Name): $($did -join ', ')"
    } else {
      Write-Host "🔎 DRY-RUN would fix $($f.Name): $($did -join ', ')"
    }
    $changed++
  }
}

if ($Apply) {
  Write-Host "✔ Done. Files changed: $changed"
  Write-Host "Tip: clear caches →  Get-ChildItem -Recurse -Include *.pyc,__pycache__ | Remove-Item -Recurse -Force"
} else {
  Write-Host "DRY-RUN complete. Use -Apply to write changes."
}
