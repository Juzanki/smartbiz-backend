param(
  [string]$Path = "backend\models",          # wapi pa kutafuta models
  [string]$PrimaryTable = "users",           # jedwali la mzazi linalolengwa (default: users)
  [switch]$All,                              # ukitaka aangalie jedwali lolote (sio users tu)
  [string]$OutDir = ".\tmp\orm_audit"        # pa kuandikia ripoti
)

Write-Host "Scanning $Path ..." -ForegroundColor Cyan
$files = Get-ChildItem -Path $Path -Filter *.py -Recurse -ErrorAction SilentlyContinue
if (-not $files) { Write-Warning "Hakuna .py files kwenye $Path"; exit 1 }

$multiFkResults = New-Object System.Collections.Generic.List[object]
$missingFkRelResults = New-Object System.Collections.Generic.List[object]

foreach ($f in $files) {
  $text  = Get-Content $f.FullName -Raw
  $lines = $text -split "`r?`n"

  # Jaribu kupata __tablename__
  $mTable = [regex]::Match($text, "__tablename__\s*=\s*['""]([^'""]+)['""]")
  $modelName = if ($mTable.Success) { $mTable.Groups[1].Value } else { [IO.Path]::GetFileNameWithoutExtension($f.Name) }

  # --------- Tafuta ForeignKey("table.column") ---------
  $fkMatches = [regex]::Matches($text, 'ForeignKey\(\s*[''"]([\w]+)\.([\w]+)[''"]')
  if ($fkMatches.Count -gt 0) {
    $fkGroups = $fkMatches | Group-Object { $_.Groups[1].Value }   # group by table
    foreach ($g in $fkGroups) {
      $refTable = $g.Name
      $count = $g.Count
      if ($All -or $refTable -eq $PrimaryTable) {
        if ($count -ge 2) {
          # pata line numbers za FKs kwa table hiyo
          $lineNos = @()
          foreach ($m in $fkMatches) {
            if ($m.Groups[1].Value -eq $refTable) {
              $ln = (($text.Substring(0, $m.Index)) -split "`r?`n").Count
              $lineNos += $ln
            }
          }
          $multiFkResults.Add([pscustomobject]@{
            File    = $f.FullName
            Model   = $modelName
            RefTable= $refTable
            FKCount = $count
            FKLines = ($lineNos | Sort-Object -Unique) -join ","
          })
        }
      }
    }
  }

  # --------- Tafuta relationships zisizo na foreign_keys= ---------
  $relMatches = [regex]::Matches($text, 'relationship\(\s*[''"]([\w\.]+)[''"]([^)]*)\)')
  foreach ($m in $relMatches) {
    $params = $m.Groups[2].Value
    if ($params -notmatch "foreign_keys\s*=") {
      $ln = (($text.Substring(0, $m.Index)) -split "`r?`n").Count
      $snippet = $m.Value
      if ($snippet.Length -gt 140) { $snippet = $snippet.Substring(0,140) + "..." }
      $missingFkRelResults.Add([pscustomobject]@{
        File       = $f.FullName
        Model      = $modelName
        Line       = $ln
        Relationship = $snippet
      })
    }
  }
}

# --------- Onyesha matokeo ---------
Write-Host "`n=== Tables with multiple FKs to the same parent" `
  + ($(if($All){"(ALL tables)"}else{"('$PrimaryTable')"})) + " ===" -ForegroundColor Cyan
$multiFkResults | Sort-Object File, RefTable | Format-Table -AutoSize

Write-Host "`n=== Relationships missing 'foreign_keys=' (potentially ambiguous) ===" -ForegroundColor Yellow
$missingFkRelResults | Sort-Object File, Line | Format-Table File, Model, Line, Relationship -AutoSize

# --------- Hifadhi ripoti ---------
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$multiFkResults       | Export-Csv -NoTypeInformation -Encoding UTF8 -Path (Join-Path $OutDir "multi_fks_$stamp.csv")
$missingFkRelResults  | Export-Csv -NoTypeInformation -Encoding UTF8 -Path (Join-Path $OutDir "relationships_missing_fk_$stamp.csv")

Write-Host "`nRipoti zimehifadhiwa kwenye $OutDir" -ForegroundColor Green
