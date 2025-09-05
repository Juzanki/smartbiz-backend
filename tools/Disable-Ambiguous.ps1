param(
  [string]$Path = "backend\models",         # folda ya models
  [string]$TargetTable = "users",           # jedwali la mzazi (default: users)
  [switch]$Apply,                           # bila -Apply => dry-run tu
  [switch]$Revert,                          # rudisha .bak
  [string]$BackupExt = ".ambigfix.bak",     # kiendelezi cha backup
  [string]$Tag = "AMBIGFIX"                 # tag kwenye comments
)

function Get-Files { Get-ChildItem -Path $Path -Filter *.py -Recurse -ErrorAction SilentlyContinue }

function Find-MultiFKFiles {
  param($files)
  $out = @()
  foreach ($f in $files) {
    $txt = Get-Content $f.FullName -Raw
    $m = [regex]::Matches($txt, 'ForeignKey\(\s*["'']([\w]+)\.([\w]+)["'']')
    if ($m.Count -gt 0) {
      $groups = $m | Group-Object { $_.Groups[1].Value }
      foreach ($g in $groups) {
        if ($g.Name -eq $TargetTable -and $g.Count -ge 2) {
          $out += $f
          break
        }
      }
    }
  }
  $out | Sort-Object FullName -Unique
}

function Find-RelationshipBlocks {
  param([string]$text)

  $results = @()
  $idx = 0
  while (($idx = $text.IndexOf("relationship(", $idx, [StringComparison]::Ordinal)) -ge 0) {
    # mwanzo wa line
    $lineStart = $text.LastIndexOf("`n", $idx)
    if ($lineStart -lt 0) { $lineStart = 0 } else { $lineStart++ }
    # kata block kwa kufuatilia mabano
    $i = $idx + "relationship(".Length
    $depth = 1
    while ($i -lt $text.Length -and $depth -gt 0) {
      $ch = $text[$i]
      if ($ch -eq '(') { $depth++ }
      elseif ($ch -eq ')') { $depth-- }
      $i++
    }
    $callEnd = $i
    while ($callEnd -lt $text.Length -and $text[$callEnd] -ne "`n") { $callEnd++ }
    $block = $text.Substring($lineStart, $callEnd - $lineStart)
    $results += [pscustomobject]@{
      Start = $lineStart
      End   = $callEnd
      Block = $block
    }
    $idx = $callEnd
  }
  return $results
}

function Comment-Block {
  param([string]$text, [int]$start, [int]$end, [string]$tag)
  $before = $text.Substring(0, $start)
  $mid    = $text.Substring($start, $end - $start)
  $after  = $text.Substring($end)
  $midLines = $mid -split "`r?`n"
  # TUMIA ${tag} ili kuepuka $tag: (scope)
  $midLines = $midLines | ForEach-Object { "# ${tag}: $_" }
  return $before + ($midLines -join "`n") + $after
}

if ($Revert) {
  $files = Get-Files | Where-Object { Test-Path ($_.FullName + $BackupExt) }
  foreach ($f in $files) {
    Copy-Item -Force ($f.FullName + $BackupExt) $f.FullName
    Write-Host "Reverted: $($f.FullName)" -ForegroundColor Yellow
  }
  exit 0
}

$files = Get-Files
$targets = Find-MultiFKFiles -files $files
if (-not $targets) {
  Write-Host "Hakuna faili lenye FKs ≥2 kwenda '$TargetTable'." -ForegroundColor Green
  exit 0
}

# Epuka "$TargetTable:" => tumia $() au string format
Write-Host ("Faili zenye FKs nyingi -> {0}" -f $TargetTable) -ForegroundColor Cyan
$targets | ForEach-Object { Write-Host (" - {0}" -f $_.FullName) }

$changed = @()
foreach ($f in $targets) {
  $text = Get-Content $f.FullName -Raw
  $blocks = Find-RelationshipBlocks -text $text

  # chukua zile zinazohusu "User" na hazina foreign_keys=
  $ambiguous = @()
  foreach ($b in $blocks) {
    $blk = $b.Block
    if ($blk -match 'relationship\(\s*["'']User["'']' -and $blk -notmatch 'foreign_keys\s*=') {
      $ambiguous += $b
    }
  }
  if (-not $ambiguous) { continue }

  Write-Host "`n$($f.FullName)" -ForegroundColor Magenta
  $ambiguous | ForEach-Object {
    $snip = $_.Block
    if ($snip.Length -gt 180) { $snip = $snip.Substring(0,180) + "..." }
    Write-Host ("  · Ambiguous: {0}" -f $snip)
  }

  if ($Apply) {
    if (-not (Test-Path ($f.FullName + $BackupExt))) {
      Copy-Item $f.FullName ($f.FullName + $BackupExt)
    }
    $ambiguous = $ambiguous | Sort-Object Start -Descending
    foreach ($b in $ambiguous) {
      $text = Comment-Block -text $text -start $b.Start -end $b.End -tag $Tag
    }
    Set-Content -Path $f.FullName -Value $text -Encoding UTF8
    $changed += $f.FullName
  }
}

if ($Apply) {
  if ($changed.Count -gt 0) {
    Write-Host "`nIme-comment ambiguous relationships (temporary hotfix):" -ForegroundColor Yellow
    $changed | Sort-Object -Unique | ForEach-Object { Write-Host (" - {0}" -f $_) }
    Write-Host "`nUnaweza kurudisha backups ukitumia:  .\backend\tools\Disable-Ambiguous.ps1 -Revert" -ForegroundColor DarkGray
  } else {
    Write-Host "`nHakuna block iliyo-commentiwa." -ForegroundColor Green
  }
} else {
  Write-Host "`n(DRY-RUN) Hakuna mabadiliko yaliyofanywa. Kimbiza tena na -Apply kufanya comment." -ForegroundColor DarkGray
}
