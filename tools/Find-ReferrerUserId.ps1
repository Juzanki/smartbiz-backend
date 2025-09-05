<# 
  Find-ReferrerUserId.ps1
  - Huchunguza repo kutafuta 'referrer_user_id' (literal) na pattern zinazoweza kuwa kwenye
    ForeignKey/relationship/primaryjoin/Index/Constraint.
  - Hutumia Select-String (regex) + Context kuonyesha kipande cha muktadha.
  - Exit 1 ikiwa kuna hits, la sivyo 0.
#>

param(
  [string]$Root = (Get-Location).Path,
  [int]$Context = 2
)

$ErrorActionPreference = 'Stop'
Write-Host ("Scanning for 'referrer_user_id' under: {0}" -f $Root) -ForegroundColor Cyan

if (-not (Test-Path $Root)) {
  Write-Host ("Path not found: {0}" -f $Root) -ForegroundColor Red
  exit 2
}

# Patterns za kutafuta (regex)
$Patterns = @(
  'referrer_user_id',                                          # literal
  'ForeignKey\([^)]*referrer_user_id[^)]*\)',                  # FK(...)
  'foreign_keys\s*=\s*\[?[^\]]*referrer_user_id[^\]]*\]?',     # relationship(foreign_keys=...)
  'primaryjoin\s*=\s*".*referrer_user_id.*"',                  # primaryjoin="..."
  'primaryjoin\s*=\s*\'.*referrer_user_id.*\'',                # primaryjoin='...'
  'Index\([^)]*referrer_user_id[^)]*\)',                       # Index(...)
  'UniqueConstraint\([^)]*referrer_user_id[^)]*\)',            # UniqueConstraint(...)
  'CheckConstraint\([^)]*referrer_user_id[^)]*\)',             # CheckConstraint(...)
  'Column\([^)]*name\s*=\s*[\'"]referrer_user_id[\'"][^)]*\)'  # Column(name="referrer_user_id")
)

# Faili za kujumuisha/kupuuza
$IncludeExt = @('*.py','*.sql','*.ini','*.cfg','*.toml','*.json','*.yml','*.yaml','*.md','*.txt')
$ExcludeDirs = @(
  '\.git\','\.venv\','\venv\','__pycache__','\node_modules\',
  '\dist\','\build\','.pytest_cache','\.idea','\.vscode'
)

function Get-RepoFiles {
  param([string]$Base)

  $all = Get-ChildItem -Path $Base -Recurse -File -Include $IncludeExt -ErrorAction SilentlyContinue
  foreach ($f in $all) {
    $p = $f.FullName
    $skip = $false
    foreach ($d in $ExcludeDirs) {
      if ($p -match [regex]::Escape($d)) { $skip = $true; break }
    }
    if (-not $skip) { $f }
  }
}

$files = Get-RepoFiles -Base $Root
if (-not $files -or $files.Count -eq 0) {
  Write-Host "No files matched." -ForegroundColor Yellow
  exit 0
}

$hits = @()

foreach ($file in $files) {
  foreach ($pat in $Patterns) {
    # -CaseSensitive:$false => case-insensitive, -Context => muktadha, -Encoding UTF8 for safety
    $results = Select-String -Path $file.FullName -Pattern $pat -AllMatches -Context $Context,$Context -Encoding UTF8 -ErrorAction SilentlyContinue
    if ($results) {
      foreach ($r in $results) {
        # tengeneza snippet kutoka kwa Context
        $snippet = @()
        if ($r.Context) {
          foreach ($l in $r.Context.PreContext)  { $snippet += ("      {0,5}: {1}" -f ($l.LineNumber), $l.Line) }
          $snippet += ("  >>  {0,5}: {1}" -f ($r.LineNumber), $r.Line)
          foreach ($l in $r.Context.PostContext) { $snippet += ("      {0,5}: {1}" -f ($l.LineNumber), $l.Line) }
        } else {
          $snippet += ("  >>  {0,5}: {1}" -f ($r.LineNumber), $r.Line)
        }

        $hits += [pscustomobject]@{
          File    = $file.FullName
          Line    = $r.LineNumber
          Pattern = $pat
          Snippet = ($snippet -join "`n")
          Ext     = $file.Extension.ToLower()
          Dir     = Split-Path $file.FullName -Parent
        }
      }
    }
  }
}

if ($hits.Count -gt 0) {
  Write-Host ""
  Write-Host ("Found {0} reference(s) to 'referrer_user_id':" -f $hits.Count) -ForegroundColor Red
  $hits | Sort-Object File, Line | ForEach-Object {
    Write-Host ("— {0} : line {1}" -f $_.File, $_.Line) -ForegroundColor Yellow
    Write-Host $_.Snippet -ForegroundColor Gray
    Write-Host ""
  }

  Write-Host "Summary by extension:" -ForegroundColor Cyan
  $hits | Group-Object Ext | Sort-Object Count -Descending | ForEach-Object {
    "{0,3}  {1}" -f $_.Count, $_.Name
  } | Write-Host

  Write-Host "`nTop directories with hits:" -ForegroundColor Cyan
  $hits | Group-Object Dir | Sort-Object Count -Descending | Select-Object -First 10 | ForEach-Object {
    "{0,3}  {1}" -f $_.Count, $_.Name
  } | Write-Host

  $outFile = Join-Path $Root "referrer_user_id_hits.json"
  $hits | ConvertTo-Json -Depth 6 | Set-Content -Path $outFile -Encoding UTF8
  Write-Host ("JSON written to: {0}" -f $outFile) -ForegroundColor DarkCyan
  exit 1
}
else {
  Write-Host "No references to 'referrer_user_id' were found." -ForegroundColor Green
  exit 0
}
