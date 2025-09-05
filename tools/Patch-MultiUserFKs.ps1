param([switch]$Apply)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Read-Text($p){ Get-Content $p -Raw }
function Save-Text($p,$txt){ Set-Content -Path $p -Value $txt -Encoding UTF8 }
function Backup-Once($p,$ext){ if(!(Test-Path ($p+$ext))){ Copy-Item $p ($p+$ext) } }

# Ramani: faili -> (attr -> fk) kwa models zenye FK zaidi ya 1 kwenda 'users'
$Map = @{
  "backend\models\gift_transaction.py" = @{ sender = "sender_id";     recipient   = "recipient_id" };
  "backend\models\fan.py"              = @{ user   = "user_id";        host        = "host_id" };
  "backend\models\co_host_invite.py"   = @{ host   = "host_id";        invitee     = "invitee_id" };
  "backend\models\referral_log.py"     = @{ referrer = "referrer_id";  referred_user = "referred_user_id" };
}

function Inject-FK([string]$call,[string]$fk){
  if($call -match 'foreign_keys\s*='){ return $call }
  # jaribu kuingiza baada ya "User",
  $patched = [regex]::Replace($call,'relationship\(\s*["'']User["'']\s*,','relationship("User", foreign_keys="User.'+$fk+'", ',1)
  if($patched -ne $call){ return $patched }
  # fallback: ikiwa hakukuwa na koma baada ya "User"
  return [regex]::Replace($call,'relationship\(\s*["'']User["'']\s*','relationship("User", foreign_keys="User.'+$fk+'" ',1)
}

function Patch-Attr([string]$text,[string]$attr,[string]$fk){
  $attrEsc = [regex]::Escape($attr)
  $pattern = '(?ms)^[ \t]*{0}[ \t]*(?::[ \t]*Mapped\[[^\]]*\])?[ \t]*=[ \t]*relationship\([^)]*["'']User["''][^)]*\)' -f $attrEsc
  $m = [regex]::Match($text, $pattern)
  if(-not $m.Success){ return $null }
  $orig = $m.Value
  $patched = Inject-FK -call $orig -fk $fk
  if($patched -eq $orig){ return $null }
  return @{ Start=$m.Index; End=($m.Index + $m.Length - 1); New=$patched; Old=$orig }
}

$planned = @()

foreach($kvp in $Map.GetEnumerator()){
  $path = $kvp.Key
  if(-not (Test-Path $path)){ Write-Host "[SKIP] $path haipo" -ForegroundColor Yellow; continue }
  $text = Read-Text $path
  foreach($pair in $kvp.Value.GetEnumerator()){
    $attr = $pair.Key; $fk = $pair.Value
    $patch = Patch-Attr -text $text -attr $attr -fk $fk
    if($patch){
      $planned += [pscustomobject]@{ File=$path; Attr=$attr; FK=$fk; Start=$patch.Start; End=$patch.End; Old=$patch.Old; New=$patch.New }
    }
  }
}

if($planned.Count -eq 0){
  Write-Host "Hakuna kitu cha kubadilisha (labda foreign_keys= tayari zipo au relationships zime-commentiwa)." -ForegroundColor Green
  exit 0
}

Write-Host "Zitarekebishwa zifuatazo:" -ForegroundColor Cyan
$planned | ForEach-Object {
  Write-Host (" - {0} :: {1} -> foreign_keys=""User.{2}""" -f $_.File,$_.Attr,$_.FK)
}

if(-not $Apply){
  Write-Host "`n(DRY-RUN) Hakuna kilichoandikwa. Endesha tena na -Apply kuandika." -ForegroundColor Yellow
  exit 0
}

# APPLY (andika kutoka mwisho kurudi mwanzo file-by-file)
$byFile = $planned | Group-Object File
foreach($grp in $byFile){
  $path = $grp.Name
  $text = Read-Text $path
  $edits = $grp.Group | Sort-Object Start -Descending
  foreach($e in $edits){
    $text = $text.Substring(0,$e.Start) + $e.New + $text.Substring($e.End+1)
  }
  Backup-Once $path ".multifk.bak"
  Save-Text $path $text
  Write-Host ("Patched: {0}" -f $path) -ForegroundColor Green
}

Write-Host "DONE. Backups: *.multifk.bak" -ForegroundColor Green
