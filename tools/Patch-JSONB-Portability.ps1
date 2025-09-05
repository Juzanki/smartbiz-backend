<#
.SYNOPSIS
  Badili matumizi yote ya JSONB/PG_NUMERIC kuwa portable (JSON_VARIANT/DECIMAL_TYPE).

.DESCRIPTION
  - Huongeza import: from backend.models._types import JSON_VARIANT, DECIMAL_TYPE, as_mutable_json
  - Hutoa import za moja kwa moja za JSONB/PG_JSONB/NUMERIC/PG_NUMERIC (au huzisafisha).
  - Hubadilisha matumizi ya JSONB -> JSON_VARIANT; PG_NUMERIC(18,2) -> DECIMAL_TYPE.
  - (Hiari) Hu-wrap JSON columns maalum kwa MutableDict.as_mutable(...).

.PARAMETER Root
  Mizizi ya project (default: .)

.PARAMETER Apply
  Ikiwa imewekwa, itaandika mabadiliko. Bila hivyo ni dry-run.

.PARAMETER UseMutable
  Ikiwa imewekwa, itajaribu ku-wrap columns za JSON za majina ya kawaida (meta, data, payload, extra, settings, config, info, details, context).

.EXAMPLE
  Set-ExecutionPolicy -Scope Process Bypass -Force
  .\backend\tools\Patch-JSONB-Portability.ps1            # Dry-run
  .\backend\tools\Patch-JSONB-Portability.ps1 -Apply     # Andika mabadiliko

#>
[CmdletBinding()]
param(
  [string]$Root = ".",
  [switch]$Apply,
  [switch]$UseMutable
)

$ErrorActionPreference = "Stop"

function Read-Text($path) {
  return [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
}
function Write-Text($path, $text) {
  [System.IO.File]::WriteAllText($path, $text, [System.Text.Encoding]::UTF8)
}
function Ensure-TypesModule {
  $typesPath = Join-Path $Root "backend\models\_types.py"
  if (-not (Test-Path $typesPath)) {
    Write-Host "➕ Creating $typesPath"
    $content = @'
from __future__ import annotations
from sqlalchemy import JSON as SA_JSON, Numeric as SA_NUMERIC
from sqlalchemy.ext.mutable import MutableDict, MutableList
try:
    from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB, NUMERIC as PG_NUMERIC  # type: ignore
except Exception:  # pragma: no cover
    PG_JSONB = None
    PG_NUMERIC = None
JSON_VARIANT = SA_JSON().with_variant(PG_JSONB, "postgresql") if PG_JSONB else SA_JSON()
DECIMAL_TYPE = SA_NUMERIC(18, 2).with_variant(PG_NUMERIC(18, 2), "postgresql") if PG_NUMERIC else SA_NUMERIC(18, 2)
def as_mutable_json(column_type=JSON_VARIANT):
    return MutableDict.as_mutable(column_type)
'@
    if ($Apply) { Write-Text $typesPath $content }
  }
}

function Patch-File($file) {
  $orig = Read-Text $file
  $text = $orig
  $changed = $false

  # 1) Safisha import za PG JSONB/NUMERIC — toa tokens tu, acha zingine (ARRAY, etc.)
  $text = [Regex]::Replace($text, '(?m)^\s*from\s+sqlalchemy\.dialects\.postgresql\s+import\s+([^\r\n]+)$', {
      param($m)
      $line = $m.Groups[1].Value
      $new = $line `
        -replace '(\bJSONB\b\s+as\s+\w+\s*,\s*)|(\bJSONB\b\s*,\s*)|(\s*,\s*\bJSONB\b\s*as\s+\w+)|(\s*,\s*\bJSONB\b)','' `
        -replace '(\bNUMERIC\b\s+as\s+\w+\s*,\s*)|(\bNUMERIC\b\s*,\s*)|(\s*,\s*\bNUMERIC\b\s*as\s+\w+)|(\s*,\s*\bNUMERIC\b)',''
      $new = $new.Trim().Trim(',')
      if ([string]::IsNullOrWhiteSpace($new)) { return "# patched: removed PG JSONB/NUMERIC import" }
      return "from sqlalchemy.dialects.postgresql import $new"
  })
  if ($text -ne $orig) { $changed = $true; $orig = $text }

  # 2) Toa assignment za ndani za JSON_VARIANT/DECIMAL_TYPE ambazo zinakingana
  $text = [Regex]::Replace($text, '(?m)^\s*JSON_VARIANT\s*=.*$', '# patched: JSON_VARIANT provided by backend.models._types')
  $text = [Regex]::Replace($text, '(?m)^\s*DECIMAL_TYPE\s*=.*$', '# patched: DECIMAL_TYPE provided by backend.models._types')
  if ($text -ne $orig) { $changed = $true; $orig = $text }

  # 3) Hakikisha tuna import ya _types
  if ($text -notmatch 'from\s+backend\.models\._types\s+import\s+JSON_VARIANT') {
    $text = [Regex]::Replace($text,
      '(?m)^(from\s+backend\.db\s+import\s+Base[^\r\n]*\r?\n)',
      '$1from backend.models._types import JSON_VARIANT, DECIMAL_TYPE, as_mutable_json' + "`r`n"
    )
    if ($text -ne $orig) { $changed = $true; $orig = $text }
  }

  # 4) Badili matumizi ya PG_JSONB/JSONB -> JSON_VARIANT
  $text = $text -replace '\bPG_JSONB\b', 'JSON_VARIANT'
  $text = $text -replace '\bJSONB\b', 'JSON_VARIANT'

  # 5) Badili PG_NUMERIC(18,2) / NUMERIC(18,2 dari PG) -> DECIMAL_TYPE
  $text = $text -replace '\bPG_NUMERIC\s*\(\s*18\s*,\s*2\s*\)', 'DECIMAL_TYPE'
  # wakati mwingine watu waliandika NUMERIC kutoka dialect ya PG:
  $text = [Regex]::Replace($text, '(?m)from\s+sqlalchemy\.dialects\.postgresql\s+import\s+.*\bNUMERIC\b.*', {
    param($m) "# patched: removed PG NUMERIC import"
  })
  # Substitutions yoyote iliyobaki
  $text = $text -replace '\bNUMERIC\s*\(\s*18\s*,\s*2\s*\)', 'DECIMAL_TYPE'

  # 6) (Hiari) Fanya JSON columns ziwe Mutable (kwa baadhi ya majina ya kawaida)
  if ($UseMutable) {
    $jsonNames = 'meta|data|payload|extra|details|settings|config|info|context'
    $pattern = "(?m)^(\s*)(\w+)\s*:\s*Mapped\[[^\]]*\]\s*=\s*mapped_column\(\s*JSON_VARIANT\s*\)"
    $text = [Regex]::Replace($text, $pattern, {
      param($m)
      $indent = $m.Groups[1].Value
      $name   = $m.Groups[2].Value
      if ($name -match $jsonNames) {
        return "$indent$name: Mapped[dict] = mapped_column(as_mutable_json(JSON_VARIANT))"
      } else {
        return $m.Value
      }
    })
  }

  if ($text -ne (Read-Text $file)) {
    if ($Apply) {
      $bak = "$file.bak"
      if (-not (Test-Path $bak)) { Copy-Item $file $bak }
      Write-Text $file $text
    }
    return @{ changed = $true; }
  }
  return @{ changed = $false; }
}

# --- Main ---
$modelsDir = Join-Path $Root "backend\models"
if (-not (Test-Path $modelsDir)) { throw "Models dir not found: $modelsDir" }

Ensure-TypesModule

$files = Get-ChildItem $modelsDir -Filter *.py -Recurse |
  Where-Object { $_.Name -notmatch '^__init__\.py$' -and $_.Name -notmatch '^_types\.py$' }

[int]$mod = 0
foreach ($f in $files) {
  $res = Patch-File $f.FullName
  if ($res.changed) {
    $mod++
    Write-Host ("✔ Patched: {0}" -f $f.FullName) -ForegroundColor Green
  } else {
    Write-Host ("· No change: {0}" -f $f.FullName) -ForegroundColor DarkGray
  }
}

Write-Host ""
if ($Apply) {
  Write-Host "Done. Patched files: $mod" -ForegroundColor Cyan
  Write-Host "Backups: *.bak (same folder)."
} else {
  Write-Host "DRY-RUN complete. To apply, run with -Apply" -ForegroundColor Yellow
}
