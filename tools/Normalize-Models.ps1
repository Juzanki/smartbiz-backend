# backend/tools/Normalize-Models.ps1
[CmdletBinding()]
param(
  [string]$Root = "backend\models",
  [switch]$NoQuoteFix   # weka hii kama hutaki kubadilisha “smart quotes/dashes”
)

$encUtf8Strict = New-Object System.Text.UTF8Encoding($false,$true)  # throw on invalid
$encUtf8       = New-Object System.Text.UTF8Encoding($false)        # no BOM
$enc1252       = [System.Text.Encoding]::GetEncoding(1252)
$encLatin1     = [System.Text.Encoding]::GetEncoding(28591)

# Ramani ya punctuation ya kawaida ya Windows-1252/Unicode -> ASCII
$mapFancy = @{
  "`u2018"="'"; "`u2019"="'"; "`u201A"=","; "`u201B"="'";
  "`u201C"='"'; "`u201D"='"'; "`u201E"='"';
  "`u2013"="-"; "`u2014"="-"; "`u2026"="..."
}

$files = Get-ChildItem -Path $Root -Recurse -Filter *.py
$conv = 0
foreach ($f in $files) {
  $bytes = [IO.File]::ReadAllBytes($f.FullName)
  $needs = $false
  try { [void]$encUtf8Strict.GetString($bytes) } catch { $needs = $true }

  if ($needs) {
    $text = $null
    foreach ($enc in @($enc1252, $encLatin1, [System.Text.Encoding]::Default)) {
      try { $text = $enc.GetString($bytes); break } catch {}
    }
    if (-not $text) { $text = [System.Text.Encoding]::ASCII.GetString($bytes) }

    if (-not $NoQuoteFix) {
      foreach ($k in $mapFancy.Keys) { $text = $text -replace $k, $mapFancy[$k] }
    }

    Copy-Item $f.FullName ($f.FullName + ".bak") -Force
    [IO.File]::WriteAllBytes($f.FullName, $encUtf8.GetBytes($text))
    Write-Host "Converted to UTF-8: $($f.FullName)" -ForegroundColor Green
    $conv++
  }
}
Write-Host "Encoding conversions: $conv" -ForegroundColor Cyan

# Patch try/except zisizo na mwili (ikiwa script ipo)
$fixPath = "backend\tools\Fix-EmptyTryBlocks.ps1"
if (Test-Path $fixPath) { & $fixPath } else { Write-Warning "Fix-EmptyTryBlocks.ps1 not found; skipping." }

# Safisha cache
Get-ChildItem -Recurse -Include *.pyc,__pycache__ | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# Unda na kimbiza ukaguzi wa AST unaoheshimu coding cookie
$chk = Join-Path $env:TEMP "ast_check_models.py"
@'
import pathlib, sys, ast, tokenize

root = pathlib.Path("backend/models")
enc_bad, bad = [], []

for p in root.rglob("*.py"):
    try:
        with tokenize.open(str(p)) as fh:  # respects "# -*- coding: ... -*-"
            src = fh.read()
    except (SyntaxError, UnicodeError) as e:
        enc_bad.append(f"{p}:{getattr(e, 'lineno', '?')}: ENCODING {e.__class__.__name__}: {e}")
        continue
    try:
        ast.parse(src, filename=str(p))
    except SyntaxError as e:
        bad.append(f"{p}:{e.lineno}:{e.msg}")

if enc_bad:
    print("ENCODING ISSUES:")
    for x in enc_bad: print("  -", x)
if bad:
    print("SYNTAX ERRORS:")
    for x in bad: print("  -", x)
    sys.exit(1)

print("All model files parse OK.")
'@ | Set-Content -LiteralPath $chk -Encoding UTF8

python $chk
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
