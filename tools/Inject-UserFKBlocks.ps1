# === tools/Inject-UserFKBlocks.ps1 ===
param(
  [string]$UserFile = "backend\models\user.py"
)

if (-not (Test-Path $UserFile)) { throw "File not found: $UserFile" }

$block = @'
    # === AMBIGFIX: disambiguated relationships ===

    # CoHost
    co_host_as_host: Mapped[list["CoHost"]] = relationship(
        "CoHost", back_populates="host",
        foreign_keys="CoHost.host_user_id",
        cascade="all, delete-orphan", passive_deletes=True, lazy="selectin",
    )
    co_host_as_cohost: Mapped[list["CoHost"]] = relationship(
        "CoHost", back_populates="cohost",
        foreign_keys="CoHost.cohost_user_id",
        cascade="all, delete-orphan", passive_deletes=True, lazy="selectin",
    )

    # ReferralLog
    referrals: Mapped[list["ReferralLog"]] = relationship(
        "ReferralLog", back_populates="referrer",
        foreign_keys="ReferralLog.referrer_id",
        cascade="all, delete-orphan", passive_deletes=True, lazy="selectin",
    )
    referrals_received: Mapped[list["ReferralLog"]] = relationship(
        "ReferralLog", back_populates="referred_user",
        foreign_keys="ReferralLog.referred_user_id",
        cascade="all, delete-orphan", passive_deletes=True, lazy="selectin",
    )

    # Support
    support_tickets: Mapped[list["SupportTicket"]] = relationship(
        "SupportTicket", back_populates="user",
        foreign_keys="SupportTicket.user_id",
        cascade="all, delete-orphan", passive_deletes=True, lazy="selectin",
    )
    support_assigned: Mapped[list["SupportTicket"]] = relationship(
        "SupportTicket", back_populates="assignee",
        foreign_keys="SupportTicket.assigned_to",
        lazy="selectin",
    )
    support_authored: Mapped[list["SupportTicket"]] = relationship(
        "SupportTicket", back_populates="author",
        foreign_keys="SupportTicket.author_id",
        lazy="selectin",
    )

    # Loyalty
    loyalty_points: Mapped[list["LoyaltyPoint"]] = relationship(
        "LoyaltyPoint", back_populates="user",
        foreign_keys="LoyaltyPoint.user_id",
        cascade="all, delete-orphan", passive_deletes=True, lazy="selectin",
    )
    loyalty_awards_given: Mapped[list["LoyaltyPoint"]] = relationship(
        "LoyaltyPoint", back_populates="awarded_by",
        foreign_keys="LoyaltyPoint.awarded_by_user_id",
        lazy="selectin",
    )

    # WebhookEndpoint
    webhooks_reviewed: Mapped[list["WebhookEndpoint"]] = relationship(
        "WebhookEndpoint", back_populates="reviewer",
        foreign_keys="WebhookEndpoint.reviewed_by", lazy="selectin",
    )
    webhooks_approved: Mapped[list["WebhookEndpoint"]] = relationship(
        "WebhookEndpoint", back_populates="approver",
        foreign_keys="WebhookEndpoint.approved_by", lazy="selectin",
    )
    webhooks_processed: Mapped[list["WebhookEndpoint"]] = relationship(
        "WebhookEndpoint", back_populates="processor",
        foreign_keys="WebhookEndpoint.processed_by", lazy="selectin",
    )

    # WithdrawRequest
    withdraw_requests: Mapped[list["WithdrawRequest"]] = relationship(
        "WithdrawRequest", back_populates="user",
        foreign_keys="WithdrawRequest.user_id",
        cascade="all, delete-orphan", passive_deletes=True, lazy="selectin",
    )
    withdraw_approvals: Mapped[list["WithdrawRequest"]] = relationship(
        "WithdrawRequest", back_populates="approver",
        foreign_keys="WithdrawRequest.approved_by", lazy="selectin",
    )
    # === /AMBIGFIX ===
'@

# soma file na uweke block mwisho wa class User kabla ya decorator inayofuata au EOF
$code = Get-Content $UserFile -Raw
if ($code -notmatch 'class\s+User\s*\(') { throw "Could not find 'class User(' in $UserFile" }

$lines = $code -split "`r?`n"
$classIdx = ($lines | Select-String -Pattern 'class\s+User\s*\(').LineNumber - 1
# tafuta mahali class inayofuata inaanza (indent 0 'class '), au EOF
$nextClass = ($lines | Select-String -Pattern '^(class\s+\w+\s*\()' | Where-Object { $_.LineNumber -gt ($classIdx + 1) } | Select-Object -First 1).LineNumber
if (-not $nextClass) { $nextClass = $lines.Length + 1 }

$insertAt = $nextClass - 2
$already = $code -match '=== AMBIGFIX: disambiguated relationships ==='
if (-not $already) {
  $new = $lines[0..$insertAt] + ($block -split "`r?`n") + $lines[($insertAt+1)..($lines.Length-1)]
  Set-Content $UserFile -Value ($new -join "`r`n") -Encoding UTF8
  Write-Host "✅ Injected AMBIGFIX block into $UserFile" -ForegroundColor Green
} else {
  Write-Host "ℹ️ AMBIGFIX block already present. Skipping." -ForegroundColor Yellow
}
