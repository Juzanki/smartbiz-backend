param(
  [string]$Root = "E:\SmartBiz_Assistance\backend",
  [switch]$Apply
)

Write-Host "Scanning Python files under $Root ..." -ForegroundColor Cyan

# ------- 1) Ramani: ClassName -> module file -------
$Map = @{
  User="user"; UserDevice="user_device"; Customer="customer"; Guest="guests";
  Wallet="wallet"; WalletTransaction="wallet"; SmartCoinWallet="smart_coin_wallet"; SmartCoinTransaction="smart_coin_transaction";
  Product="product"; Order="order"; Payment="payment"; RechargeTransaction="recharge_transaction"; WithdrawRequest="withdraw_request";
  AdEarning="ad_earning";
  Gift="gift_model"; GiftTransaction="gift_transaction"; GiftMovement="gift_movement"; GiftFly="gift_fly"; GiftMarker="gift_marker";
  LiveStream="live_stream"; LiveSession="live_session"; LiveViewer="live_viewer"; RecordedStream="recorded_stream";
  ReplayEvent="replay_events"; ReplayHighlight="replay_highlight"; ReplaySummary="replay_summary"; ReplayCaption="replay_caption";
  ReplayTitle="replay_title"; ReplayAnalytics="replay_analytics";
  VideoPost="video_post"; VideoComment="video_comment"; VideoViewStat="view_stat"; PostLiveNotification="post_live_notification";
  UserBot="user_bot"; BotPackage="bot_package"; AIBotSettings="ai_bot_settings"; AutoReplyTraining="auto_reply_training";
  Campaign="campaign"; CampaignAffiliate="campaign"; ReferralBonus="referral_bonus"; ReferralLog="referral_log";
  Notification="notification"; NotificationPreference="notification_preferences"; PushSubscription="push_subscription";
  MessageLog="message_log"; ScheduledMessage="scheduled_message"; ChatMessage="chat";
  AnalyticsSnapshot="analytics"; BillingLog="billing_log"; ErrorLog="error_log"; TokenUsageLog="token_usage_log";
  InjectionLog="injection_log"; SearchLog="search_log"; PostLog="post_log"; CustomerFeedback="feedback"; LoginHistory="login_history";
  ScheduledTask="scheduled_task"; TaskFailureLog="task_failure_log";
  WebhookEndpoint="webhook"; WebhookDeliveryLog="webhook_delivery_log";
  Setting="setting"; Settings="setting"; APIKey="api_key"; AuditLog="audit_log"; PlatformStatus="platform_status";
  MagicLink="magic_link"; LoyaltyPoint="loyalty"; SubscriptionPlan="subscription"; UserSubscription="subscription";
  SupportTicket="support"; Tag="smart_tags"; SocialMediaPost="social_media_post"; Like="like_model";
  CoHost="co_host"; CoHostInvite="co_host_invite";
  ConnectedPlatform="connected_platform"; DroneMission="drone_mission";
  BadgeHistory="badge_history"; ActivityScore="activity_score";
  PasswordResetCode="password_reset";
  Balance="balance";
}

function New-ModelImportLines {
  param([string[]]$ClassNames)
  $lines = @()
  foreach ($cls in $ClassNames) {
    $trim = ($cls -as [string]).Trim()
    if (-not $trim) { continue }
    if ($Map.ContainsKey($trim)) {
      $mod = $Map[$trim]
      $lines += "from backend.models.$mod import $trim"
    } else {
      $lines += "# TODO: Unknown model '$trim' (left original below)"
      $lines += "# from backend.models import $trim"
    }
  }
  return ($lines -join "`n")
}

# ------- 2) Tafuta .py zote (epuka venv/__pycache__/alembic versions) -------
$files = Get-ChildItem -Path $Root -Recurse -Filter *.py |
  Where-Object { $_.FullName -notmatch '\\venv\\|\\__pycache__\\|\\alembic\\versions\\' }

# Regex patterns
$patternSingle = '^[ \t]*from[ \t]+backend\.models[ \t]+import[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]*(?:#.*)?$'
$patternMulti  = 'from\s+backend\.models\s+import\s*\((.*?)\)'

[int]$changed = 0
foreach ($f in $files) {
  # Soma faili salama (ikishindikana, ruka)
  try {
    $text = Get-Content -LiteralPath $f.FullName -Raw -ErrorAction Stop
  } catch {
    Write-Host "Skip (read error): $($f.FullName) -> $($_.Exception.Message)" -ForegroundColor DarkYellow
    continue
  }
  if ($null -eq $text) { $text = "" }   # linda dhidi ya $null

  $orig = $text

  # Badili grouped imports (singleline)
  $text = [System.Text.RegularExpressions.Regex]::Replace(
    $text,
    $patternMulti,
    {
      param($m)
      $inner = $m.Groups[1].Value
      $classes = $inner -split ','
      "`n" + (New-ModelImportLines $classes) + "`n"
    },
    [System.Text.RegularExpressions.RegexOptions]::Singleline
  )

  # Badili single-name imports (multiline)
  $text = [System.Text.RegularExpressions.Regex]::Replace(
    $text,
    $patternSingle,
    {
      param($m)
      $cls = $m.Groups[1].Value
      New-ModelImportLines @($cls)
    },
    [System.Text.RegularExpressions.RegexOptions]::Multiline
  )

  if ($text -ne $orig) {
    $changed++
    if ($Apply) {
      $bak = "$($f.FullName).bak_modelsfix"
      if (-not (Test-Path $bak)) { Copy-Item -LiteralPath $f.FullName -Destination $bak -ErrorAction SilentlyContinue }
      # andika kwa UTF-8 (isiyo na BOM) ili usiharibu herufi
      Set-Content -LiteralPath $f.FullName -Value $text -Encoding UTF8
      Write-Host "Updated: $($f.FullName)" -ForegroundColor Green
    } else {
      Write-Host "Will update: $($f.FullName)" -ForegroundColor Yellow
    }
  }
}

if ($Apply) {
  Write-Host "Done. Files changed: $changed" -ForegroundColor Cyan
} else {
  Write-Host "Preview complete. Files that will change: $changed" -ForegroundColor Cyan
  Write-Host "Re-run with -Apply to write changes." -ForegroundColor Magenta
}
