param(
  [string]$ModelsRoot = "backend\models"
)

function Read-Text($p){ Get-Content $p -Raw }
$rx = [regex]'(?ms)^[ \t]*([A-Za-z_]\w*)[ \t]*(?::[ \t]*Mapped\[[^\]]*\])?[ \t]*=[ \t]*(?:mapped_column|Column)\([^)]*ForeignKey\([ \t]*["'']users\.[^"'']+["'']'

$rows = @()
$files = Get-ChildItem -Path $ModelsRoot -Recurse -Filter *.py -ErrorAction SilentlyContinue
foreach($f in $files){
  $t = Read-Text $f.FullName
  $m = $rx.Matches($t)
  if($m.Count -gt 0){
    $fks = @()
    foreach($mm in $m){ $fks += $mm.Groups[1].Value }
    $rows += [pscustomobject]@{ File=$f.FullName; UserFKs=($fks | Sort-Object -Unique -CaseSensitive -ErrorAction SilentlyContinue) -join ", " }
  }
}

if($rows.Count -eq 0){ Write-Host "Hakuna user-FK zilizopatikana." -ForegroundColor Yellow; exit }
$rows | Sort-Object File | Format-Table -AutoSize
