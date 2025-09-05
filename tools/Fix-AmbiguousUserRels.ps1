<#
.SYNOPSIS
  Rekebisha AmbiguousForeignKeys kwa kuboresha relationships ndani ya backend\models\user.py

.DESCRIPTION
  - Huingiza (kama hazipo) vipande vya: foreign_keys=lambda: [_col([...], "Model", "column")]
  - Inalenga attributes zifuatazo (ambazo kwa kawaida ndizo zenye FKs > 1 kwenda users.id):
      * customer_feedbacks        -> CustomerFeedback.user_id
      * feedbacks_assigned        -> CustomerFeedback.assigned_to_user_id
      * notifications_received    -> Notification.user_id
      * notifications_sent        -> Notification.actor_user_id
      * referrals_made (alias referrals) -> ReferralLog.referrer_id
      * referrals_received        -> ReferralLog.referred_user_id
      * referral_bonuses_made     -> ReferralBonus.referrer_id
      * referral_bonuses_received -> ReferralBonus.referred_user_id
      * withdraw_requests         -> WithdrawRequest.user_id
      * withdraw_approvals        -> WithdrawRequest.approved_by_user_id
      * co_host_as_host           -> CoHost.host_user_id
      * co_host_as_cohost         -> CoHost.cohost_user_id
      * following_hosts           -> Fan.user_id
      * host_followers            -> Fan.host_user_id
      * badge_events_received     -> BadgeHistory.user_id
      * badge_events_given        -> BadgeHistory.awarded_by_id
      * gift_movements_sent       -> GiftMovement.sender_id
      * gift_movements_received   -> GiftMovement.host_id
      * gift_transactions_sent    -> GiftTransaction.sender_id
      * gift_transactions_received-> GiftTransaction.recipient_id
      * goals                     -> Goal.creator_id
      * moderations_received      -> ModerationAction.target_user_id
      * moderations_taken         -> ModerationAction.moderator_id

  - Ikiwa jina la module si lile kwenye mfano, unaweza kubadilisha "Modules" kwenye mapping hapa chini.

.USAGE
  PS> cd E:\SmartBiz_Assistance
  PS> .\tools\Fix-AmbiguousUserRels.ps1 -UserFile 'backend\models\user.py'

  # Ongeza/rekebisha mapping bila kuhariri script:
  PS> .\tools\Fix-AmbiguousUserRels.ps1 -UserFile 'backend\models\user.py' `
        -OverrideMap @{ 'customer_feedbacks' = @{ Model='CustomerFeedback'; Column='user_id'; Modules=@('backend.models.customer_feedback','backend.models.feedback') } }
#>

param(
  [Parameter(Mandatory=$true)]
  [string]$UserFile,

  # Ruhusu kubadilisha/kuongeza entry fulani kwa haraka
  [hashtable]$OverrideMap
)

if (-not (Test-Path $UserFile)) {
  Write-Error "Faili haipatikani: $UserFile"
  exit 1
}

# --------- Helper: salama zaidi kuandika regex cross-lines ---------
function Add-FK-To-Relationship {
  param(
    [string]$Content,
    [string]$AttrName,      # e.g. 'customer_feedbacks'
    [string]$Model,         # e.g. 'CustomerFeedback'
    [string]$Column,        # e.g. 'user_id'
    [string[]]$Modules      # e.g. @('backend.models.customer_feedback','backend.models.feedback')
  )

  # Tafuta block ya attribute:
  #  customer_feedbacks: Mapped[List["CustomerFeedback"]] = relationship(
  #       "CustomerFeedback",
  #       back_populates="user",
  #       ...
  #  )
  $attrRx = [regex]::new("(?ms)^\s*$AttrName\s*:\s*Mapped\[.*?\]\s*=\s*relationship\(\s*[""']$Model[""'].*?\)", 'Multiline, Singleline')

  $m = $attrRx.Match($Content)
  if (-not $m.Success) { return $Content }  # attribute haipo; ruka

  $block = $m.Value
  # Kama tayari ina foreign_keys=… acha kama ilivyo
  if ($block -match 'foreign_keys\s*=') { return $Content }

  # Tengeneza kipande cha foreign_keys
  # mfano: foreign_keys=lambda: [_col(["backend.models.customer_feedback","backend.models.feedback"], "CustomerFeedback", "user_id")],
  $modsEsc = $Modules | ForEach-Object { '"{0}"' -f $_ } | -join ', '
  $fkLine = "        foreign_keys=lambda: [_col([$modsEsc], ""$Model"", ""$Column"")],`r`n"

  # weka kabla ya 'cascade=' ikiwa ipo, au kabla ya kufunga ')'
  if ($block -match '(\r?\n\s*)cascade\s*=\s*["'']') {
    # weka kabla ya cascade line
    $newBlock = $block -replace '(\r?\n\s*)cascade\s*=\s*["'']', ("`r`n$fkLine" + '        cascade="')
  } else {
    # weka kabla ya `)`
    $newBlock = $block -replace '\)\s*$', ("`r`n$fkLine    )")
  }

  # Badilisha content
  $start = $m.Index
  $len   = $m.Length
  $before = $Content.Substring(0, $start)
  $after  = $Content.Substring($start + $len)
  return ($before + $newBlock + $after)
}

# --------- Default mapping (unaweza kubadilisha via -OverrideMap) ----------
$Map = @{
  'customer_feedbacks'         = @{ Model='CustomerFeedback'; Column='user_id';              Modules=@('backend.models.customer_feedback','backend.models.feedback') }
  'feedbacks_assigned'         = @{ Model='CustomerFeedback'; Column='assigned_to_user_id';  Modules=@('backend.models.customer_feedback','backend.models.feedback') }

  'notifications_received'     = @{ Model='Notification';      Column='user_id';             Modules=@('backend.models.notification','backend.models.notifications') }
  'notifications_sent'         = @{ Model='Notification';      Column='actor_user_id';       Modules=@('backend.models.notification','backend.models.notifications') }

  'referrals_made'             = @{ Model='ReferralLog';       Column='referrer_id';         Modules=@('backend.models.referral_log') }
  'referrals_received'         = @{ Model='ReferralLog';       Column='referred_user_id';    Modules=@('backend.models.referral_log') }

  'referral_bonuses_made'      = @{ Model='ReferralBonus';     Column='referrer_id';         Modules=@('backend.models.referral_bonus') }
  'referral_bonuses_received'  = @{ Model='ReferralBonus';     Column='referred_user_id';    Modules=@('backend.models.referral_bonus') }

  'withdraw_requests'          = @{ Model='WithdrawRequest';   Column='user_id';             Modules=@('backend.models.withdraw_request','backend.models.withdrawrequests') }
  'withdraw_approvals'         = @{ Model='WithdrawRequest';   Column='approved_by_user_id'; Modules=@('backend.models.withdraw_request','backend.models.withdrawrequests') }

  'co_host_as_host'            = @{ Model='CoHost';            Column='host_user_id';        Modules=@('backend.models.co_host','backend.models.cohost') }
  'co_host_as_cohost'          = @{ Model='CoHost';            Column='cohost_user_id';      Modules=@('backend.models.co_host','backend.models.cohost') }

  'following_hosts'            = @{ Model='Fan';               Column='user_id';             Modules=@('backend.models.fan','backend.models.fans') }
  'host_followers'             = @{ Model='Fan';               Column='host_user_id';        Modules=@('backend.models.fan','backend.models.fans') }

  'badge_events_received'      = @{ Model='BadgeHistory';      Column='user_id';             Modules=@('backend.models.badge_history','backend.models.badges') }
  'badge_events_given'         = @{ Model='BadgeHistory';      Column='awarded_by_id';       Modules=@('backend.models.badge_history','backend.models.badges') }

  'gift_movements_sent'        = @{ Model='GiftMovement';      Column='sender_id';           Modules=@('backend.models.gift_movement') }
  'gift_movements_received'    = @{ Model='GiftMovement';      Column='host_id';             Modules=@('backend.models.gift_movement') }

  'gift_transactions_sent'     = @{ Model='GiftTransaction';   Column='sender_id';           Modules=@('backend.models.gift_transaction') }
  'gift_transactions_received' = @{ Model='GiftTransaction';   Column='recipient_id';        Modules=@('backend.models.gift_transaction') }

  'goals'                      = @{ Model='Goal';              Column='creator_id';          Modules=@('backend.models.goal','backend.models.goals') }

  'moderations_received'       = @{ Model='ModerationAction';  Column='target_user_id';      Modules=@('backend.models.moderation_action','backend.models.moderation') }
  'moderations_taken'          = @{ Model='ModerationAction';  Column='moderator_id';        Modules=@('backend.models.moderation_action','backend.models.moderation') }
}

# Override kama mtumiaji ametoa
if ($OverrideMap) {
  foreach ($k in $OverrideMap.Keys) {
    $Map[$k] = $OverrideMap[$k]
  }
}

# Soma content na tengeneza backup
$original = Get-Content $UserFile -Raw
$stamp = (Get-Date).ToString('yyyyMMddHHmmss')
Copy-Item $UserFile "$UserFile.bak-$stamp"

# Hakikisha helper _col upo. Kama haipo, wape maonyo tu; hatulazimishi hapa.
if ($original -notmatch '\bdef\s+_col\(') {
  Write-Warning "Helper _col(...) haikuonekana ndani ya user.py. Hakikisha imewekwa kama tulivyoelekeza awali."
}

# Pitisha kwenye kila entry ya ramani
$content = $original
foreach ($attr in $Map.Keys) {
  $def = $Map[$attr]
  $content = Add-FK-To-Relationship `
               -Content $content `
               -AttrName $attr `
               -Model $def.Model `
               -Column $def.Column `
               -Modules $def.Modules
}

# Andika kama kumeleta mabadiliko
if ($content -ne $original) {
  Set-Content -Path $UserFile -Value $content -Encoding UTF8
  Write-Host "✅ Mabadiliko yameandikwa: $UserFile (backup: user.py.bak-$stamp)" -ForegroundColor Green
} else {
  Write-Host "ℹ️  Hakuna kilichobadilika; labda foreign_keys tayari zipo au attributes hazikupatikana." -ForegroundColor Yellow
}
