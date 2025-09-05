param(
  [string]$Path = "backend/models",   # dir ya kuchanganua
  [switch]$Apply,                     # andika mabadiliko
  [switch]$Backup = $true,            # tengeneza .bak kabla ya kuandika
  [switch]$CleanBak                   # futa .bak baada ya mafanikio
)

# -- Utils
function Get-PyFiles($root) {
  Get-ChildItem -Path $root -Recurse -Include *.py -File -ErrorAction SilentlyContinue |
    Where-Object { -not $_.FullName.EndsWith(".bak") }
}

$pattern = '(?m)^(?<indent>[ \t]*)(?<hdr>except[^\r\n]*:\s*)\r?\n(?!\k<indent>[ \t]+)'
# Ufafanuzi:
# - inakamata mstari wa 'except ...:' na indentation yake
# - (?!\k<indent>[ \t]+) huhakikisha mstari unaofuata HAUNA indentation zaidi (yaani hakuna mwili) → tuingize 'pass'

$totalFiles = 0
$totalFixes = 0
$filesChanged = @()

$files = Get-PyFiles -root $Path
if (-not $files) {
  Write-Host "ℹ️ Hakuna .py kupatikana kwenye '$Path'."
  exit 0
}

foreach ($f in $files) {
  $text = Get-Content -Path $f.FullName -Raw -ErrorAction Stop
  $fixCount = 0

  # Tumia evaluator kudunga 'pass' kwa kila match ukihifadhi indentation
  $evaluator = {
    param($m)
    $script:fixCount++
    $indent = $m.Groups['indent'].Value
    $hdr    = $m.Groups['hdr'].Value
    # weka mstari wa pass wenye indentation + 4 spaces
    return "$indent$hdr`r`n$indent    pass`r`n"
  }

  $newText = [regex]::Replace($text, $pattern, $evaluator, [System.Text.RegularExpressions.RegexOptions]::Multiline)

  if ($fixCount -gt 0) {
    $totalFiles++
    $totalFixes += $fixCount
    $filesChanged += @{ Path = $f.FullName; Fixes = $fixCount }

    if ($Apply) {
      if ($Backup) {
        Copy-Item -Path $f.FullName -Destination ($f.FullName + ".bak") -Force
      }
      # andika faili
      [System.IO.File]::WriteAllText($f.FullName, $newText, [System.Text.Encoding]::UTF8)
      Write-Host ("✔️  {0}  (+{1} fix{2})" -f $f.FullName, $fixCount, ($(if($fixCount -gt 1){'es'}else{''})))
    } else {
      Write-Host ("🔎 (Preview) {0}  →  ingeongezwa 'pass' mara {1}" -f $f.FullName, $fixCount)
    }
  }
}

if ($totalFiles -eq 0) {
  Write-Host "✅ Hakuna 'except:' tupu zilizopatikana ndani ya '$Path'."
} else {
  if ($Apply -and $CleanBak) {
    $bak = Get-ChildItem -Path $Path -Recurse -Include *.bak -File -ErrorAction SilentlyContinue
    if ($bak) {
      $bak | Remove-Item -Force
      Write-Host "🧹 .bak zote zimefutwa."
    }
  }
  Write-Host ("==> Jumla: files {0}, fixes {1}" -f $totalFiles, $totalFixes)
}

# Exit code nzuri kwa CI
if (-not $Apply) {
  Write-Host "ℹ️ Hii ilikuwa PREVIEW. Tumia -Apply kuandika mabadiliko."
}
