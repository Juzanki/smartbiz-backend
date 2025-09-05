param(
  # (hiari) ukipita -Root itatumika; vinginevyo tutagundua kiotomatiki
  [string]$Root
)

# ------------- Resolve Root -------------
function Resolve-ModelsPath {
  param([string]$RootArg)

  if ($RootArg) { return (Resolve-Path -LiteralPath $RootArg -ErrorAction SilentlyContinue) }

  # Ukimbiza script: $PSScriptRoot ni dir ya script (backend/tools)
  if ($PSScriptRoot) {
    $maybe = Join-Path $PSScriptRoot "..\models"
    $rp = Resolve-Path -LiteralPath $maybe -ErrorAction SilentlyContinue
    if ($rp) { return $rp }
  }

  # Backups: jaribu relative to CWD
  $cwdMaybe = ".\backend\models"
  $rp2 = Resolve-Path -LiteralPath $cwdMaybe -ErrorAction SilentlyContinue
  if ($rp2) { return $rp2 }

  throw "Could not resolve models path. Try: pwsh backend/tools/Find-DuplicateTables.ps1 -Root '.\backend\models'"
}

$ModelsPath = Resolve-ModelsPath -RootArg $Root

# ------------- Scan -------------
$rxTablename = [regex]'__tablename__\s*=\s*["''](?<name>[A-Za-z0-9_]+)["'']'
$rxTableCall = [regex]'Table\(\s*["''](?<name>[A-Za-z0-9_]+)["'']'

$hits = @()

Get-ChildItem $ModelsPath -Include *.py -Recurse |
  Where-Object { $_.FullName -notmatch 'bak|backup|legacy|old|tmp|__old|__disabled' } |
  ForEach-Object {
    $line = 0
    Get-Content -LiteralPath $_.FullName | ForEach-Object {
      $line++
      foreach ($m in $rxTablename.Matches($_)) {
        $hits += [pscustomobject]@{ Table=$m.Groups['name'].Value; Path=$_.FullName; Line=$line; Snippet=$_ }
      }
      foreach ($m in $rxTableCall.Matches($_)) {
        $hits += [pscustomobject]@{ Table=$m.Groups['name'].Value; Path=$_.FullName; Line=$line; Snippet=$_ }
      }
    }
  }

$dups = $hits | Group-Object Table | Where-Object { $_.Count -gt 1 }

if (-not $dups) {
  Write-Host "✅ Hakuna duplicate za meza." -ForegroundColor Green
  Write-Host "Scanned: $ModelsPath"
  exit 0
}

Write-Host "`n🚨 Duplicate tables detected (path: $ModelsPath):" -ForegroundColor Yellow
foreach ($g in $dups) {
  Write-Host "`n==== $($g.Name) (count=$($g.Count)) ====" -ForegroundColor Red
  $g.Group | Sort-Object Path, Line | Format-Table Path, Line, @{L='Snippet';E={$_.Snippet.Trim()}}
}
exit 1
