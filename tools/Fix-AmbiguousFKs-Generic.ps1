param([switch]$Apply)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Read-Text($p){ Get-Content -Raw -Path $p }
function Save-Text($p,$t){ Set-Content -Path $p -Value $t -Encoding UTF8 }
function Backup-Once($p,$ext){ if(-not (Test-Path ($p+$ext))){ Copy-Item $p ($p+$ext) } }

# ===== Helpers =====

function Get-ModelName([string]$text){
  $m = [regex]::Match($text,'(?m)^\s*class\s+([A-Za-z_]\w*)\s*\(')
  if($m.Success){ return $m.Groups[1].Value } else { return $null }
}

function To-Snake([string]$name){
  $x = [regex]::Replace($name,'([a-z0-9])([A-Z])','$1_$2').ToLower()
  return $x
}

# Known mapping (ongeza hapa ukiona target mpya)
$ModelToTable = @{
  "User"             = "users"
  "LiveStream"       = "live_streams"
  "VideoPost"        = "video_posts"
  "ScheduledTask"    = "scheduled_tasks"
  "SmartCoinWallet"  = "smart_coin_wallets"
  "WebhookEndpoint"  = "webhook_endpoints"
  "SupportTicket"    = "support_tickets"
  "Campaign"         = "campaigns"
  "Order"            = "orders"
  "Product"          = "products"
  "Payment"          = "payments"
  "SearchLog"        = "search_logs"
  "LiveSession"      = "live_sessions"
  "ReplayEvent"      = "replay_events"
  "Superchat"        = "superchats"
}

function Guess-Table([string]$targetModel){
  if($ModelToTable.ContainsKey($targetModel)){ return $ModelToTable[$targetModel] }
  $snake = To-Snake $targetModel
  if($snake -notmatch 's$'){ $snake = $snake + 's' }  # cheap plural
  return $snake
}

# Rudisha: @{ <table> = @('col1','col2',...) }
function Parse-FKs([string]$text){
  $out = @{}
  # SQLAlchemy 2.0 style: mapped_column(ForeignKey("table.id"))
  $rx1 = '(?ms)^\s*([A-Za-z_]\w*)\s*(?::[^\n=]+)?=\s*mapped_column\([^)]*ForeignKey\(\s*["'']([a-z0-9_]+)\.id["'']\s*\)[^)]*\)'
  # classic style: Column(..., ForeignKey("table.id"), ...)
  $rx2 = '(?ms)^\s*([A-Za-z_]\w*)\s*=\s*Column\([^)]*ForeignKey\(\s*["'']([a-z0-9_]+)\.id["'']\s*\)[^)]*\)'
  foreach($rx in @($rx1,$rx2)){
    $m = [regex]::Match($text,$rx)
    while($m.Success){
      $col = $m.Groups[1].Value
      $tbl = $m.Groups[2].Value
      if(-not $out.ContainsKey($tbl)){ $out[$tbl] = New-Object System.Collections.ArrayList }
      [void]$out[$tbl].Add($col)
      $m = $m.NextMatch()
    }
  }
  return $out
}

# Rudisha list ya relationships zisizo na foreign_keys=
# item: @{ Attr='<attr>'; Target='<Model>'; Full='<whole call>'; Start=int; End=int }
function Parse-Relationships([string]$text){
  $list = @()
  $rx = '(?ms)^\s*([A-Za-z_]\w*)\s*(?::[^\n=]+)?=\s*relationship\(\s*["'']([A-Za-z_]\w*)["'']([^)]*)\)'
  $m = [regex]::Match($text,$rx)
  while($m.Success){
    $attr   = $m.Groups[1].Value
    $target = $m.Groups[2].Value
    $inside = $m.Groups[3].Value
    if($inside -notmatch 'foreign_keys\s*='){
      $list += @{ Attr=$attr; Target=$target; Full=$m.Value; Start=$m.Index; End=($m.Index + $m.Length - 1) }
    }
    $m = $m.NextMatch()
  }
  return $list
}

# Chagua FK kulingana na attr na list ya candidate cols
function Choose-FK([string]$attr, [string[]]$candidates){
  # 1) attr_id moja kwa moja
  $direct = $attr + "_id"
  if($candidates -contains $direct){ return $direct }

  # 2) Patterns
  $checks = @(
    @{ pat='sender';         try=@('sender_id') }
    @{ pat='recipient|receiver'; try=@('recipient_id','receiver_id') }
    @{ pat='^host';          try=@('host_id') }
    @{ pat='invitee';        try=@('invitee_id') }
    @{ pat='referrer';       try=@('referrer_id') }
    @{ pat='referred';       try=@('referred_user_id','referred_id') }
    @{ pat='moderator';      try=@('moderator_id') }
    @{ pat='assigned|assignee'; try=@('assigned_to_id','assigned_to') }
    @{ pat='approved';       try=@('approved_by_id','approved_by') }
    @{ pat='processed';      try=@('processed_by_id','processed_by') }
    @{ pat='updated';        try=@('updated_by_id','updated_by') }
    @{ pat='created|creator';try=@('created_by_id','created_by','creator_id') }
    @{ pat='author';         try=@('author_id') }
    @{ pat='clicked';        try=@('clicked_by_id','clicked_by') }
    @{ pat='target';         try=@('target_user_id','target_id') }
    @{ pat='^user$';         try=@('user_id') }
    @{ pat='^from|^source';  try=($candidates | Where-Object { $_ -like 'from_*' }) }
    @{ pat='^to$|dest';      try=($candidates | Where-Object { $_ -like 'to_*' }) }
  )
  foreach($rule in $checks){
    if($attr -match $rule.pat){
      foreach($name in $rule.try){
        if($candidates -contains $name){ return $name }
      }
    }
  }

  # 3) Zikiwa mbili, jaribu heuristic rahisi: chukua yenye 'user' au 'id' ya moja kwa moja
  $pick = $candidates | Where-Object { $_ -eq 'user_id' }
  if($pick){ return $pick[0] }

  if($candidates.Count -eq 1){ return $candidates[0] }

  return $null
}

function Inject-FK([string]$full,[string]$targetModel,[string]$modelName,[string]$fkCol){
  if(-not $fkCol){ return $null }
  # weka foreign_keys="<Model>.<col>" mara moja baada ya target model
  $patched = [regex]::Replace($full,'relationship\(\s*["'']'+[regex]::Escape($targetModel)+'["'']\s*,?',
                              'relationship("'+$targetModel+'", foreign_keys="'+$modelName+'.'+$fkCol+'", ',1)
  if($patched -ne $full){ return $patched }
  # fallback ikiwa hakukuwa na comma
  $patched = [regex]::Replace($full,'relationship\(\s*["'']'+[regex]::Escape($targetModel)+'["'']\s*',
                              'relationship("'+$targetModel+'", foreign_keys="'+$modelName+'.'+$fkCol+'" ',1)
  return $patched
}

# ===== Main =====
$root = Join-Path $PSScriptRoot "..\models"
$files = Get-ChildItem -Path $root -Filter *.py -Recurse |
