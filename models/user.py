# backend/models/user.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import hashlib
import datetime as dt
from typing import Optional, TYPE_CHECKING, List, Sequence

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import (
    Mapped,
    mapped_column,
    relationship,
    validates,
    synonym,
)
from sqlalchemy.event import listens_for
from sqlalchemy import inspect as _inspect

from backend.db import Base, engine

# ── Register a few core models early (avoid mapper import races)
import backend.models.setting  # noqa: F401
import backend.models.notification_preferences  # noqa: F401
import backend.models.customer_feedback as _cf  # noqa: F401

# Try import Support early (usipige crash kama haipo bado)
try:
    import backend.models.support as _sup_mod  # noqa: F401
except Exception:
    _sup_mod = None  # noqa: N816

# ---------- Robust lazy import helpers ----------
from importlib import import_module

def _try_getattr(mod: str, name: str):
    try:
        return getattr(import_module(mod), name)
    except Exception:
        return None

def _get_model(mod_candidates: Sequence[str], cls_name: str):
    for m in mod_candidates:
        obj = _try_getattr(m, cls_name)
        if obj is not None:
            return obj
    raise ModuleNotFoundError(
        f"Could not import {cls_name} from any of: {', '.join(mod_candidates)}"
    )

def _col(mod_candidates: Sequence[str], cls_name: str, col_name: str):
    """Return a Column object (e.g., Guest.user_id) via robust lazy import."""
    Model = _get_model(mod_candidates, cls_name)
    return getattr(Model, col_name)

def _fcol(mod_candidates: Sequence[str], cls_name: str, col_candidates: Sequence[str]):
    """
    Flexible column resolver: return the first existing column name on Model.
    Useful when different codebases use different FK column names.
    """
    Model = _get_model(mod_candidates, cls_name)
    for cname in col_candidates:
        if hasattr(Model, cname):
            return getattr(Model, cname)
    raise AttributeError(
        f"{cls_name} has none of the expected columns: {', '.join(col_candidates)}"
    )

# Optional shim: SupportTicket module might be support.py or support_ticket.py
_SUPPORT_MODS = ["backend.models.support", "backend.models.support_ticket"]

# Optional shim: Forgot password / password reset modules
_AUTH_MODS = [
    "backend.models.forgot_password",
    "backend.models.password_reset",
    "backend.models.password",
    "backend.models.auth",
    "backend.models.forgot_password_request",
]

# Optional shim: UserBot module names
_BOT_MODS = [
    "backend.models.user_bot",
    "backend.models.bot",
    "backend.models.bots",
]

# ---------- TYPE_CHECKING-only imports ----------
if TYPE_CHECKING:
    from .wallet import Wallet
    from .activity_score import ActivityScore
    from .smart_coin_wallet import SmartCoinWallet
    from .order import Order
    from .ad_earning import AdEarning
    from .ai_bot_settings import AIBotSettings
    from .audit_log import AuditLog
    from .auto_reply_training import AutoReplyTraining
    from .badge_history import BadgeHistory
    from .balance import Balance
    from .billing_log import BillingLog
    from .user_bot import UserBot
    from .chat import ChatMessage
    from .co_host import CoHost
    from .customer import Customer
    from .drone_mission import DroneMission
    from .error_log import ErrorLog
    from .gift_fly import GiftFly
    from .campaign import CampaignAffiliate
    from .live_session import LiveSession
    from .login_history import LoginHistory
    from .loyalty import LoyaltyPoint
    from .magic_link import MagicLink
    from .message_log import MessageLog
    from .notification_preferences import NotificationPreference
    from .notification import Notification
    from .payment import Payment
    from .platform_status import PlatformStatus
    from .connected_platform import ConnectedPlatform
    from .post_log import PostLog
    from .social_media_post import SocialMediaPost
    from .push_subscription import PushSubscription
    from .recharge_transaction import RechargeTransaction
    from .referral_bonus import ReferralBonus
    from .referral_log import ReferralLog
    from .scheduled_message import ScheduledMessage
    from .scheduled_task import ScheduledTask
    from .search_log import SearchLog
    from .setting import (
        UserSettings,
        UserDeviceSetting,
        SettingsAudit,
        UserKVSetting,
        DoNotDisturbWindow,
        NotificationSetting,
        FeatureFlagOverride,
    )
    from .smart_coin_transaction import SmartCoinTransaction
    from .subscription import UserSubscription
    from .support import SupportTicket
    from .smart_tags import Tag
    from .token_usage_log import TokenUsageLog
    from .top_contributor import TopContributor
    from .user_device import UserDevice
    from .video_comment import VideoComment
    from .video_view_stat import VideoViewStat
    from .webhook_delivery_log import WebhookDeliveryLog
    from .webhook_endpoint import WebhookEndpoint
    from .withdraw_request import WithdrawRequest
    from .guest import Guest
    from .goal import Goal
    from .leaderboard_notification import LeaderboardNotification
    from .like import Like
    from .post_live_notification import PostLiveNotification
    from .moderation_action import ModerationAction
    from .fan import Fan
    from .customer_feedback import CustomerFeedback
    from .gift_movement import GiftMovement
    from .gift_transaction import GiftTransaction
    from .message import Message
    from .share_activity import ShareActivity

# ----------- Dynamic password column resolver -----------
# SMARTBIZ_PWHASH_COL=hashed_password | password_hash
_PWHASH_COL = os.getenv("SMARTBIZ_PWHASH_COL", "").strip()

def _table_columns(table: str) -> set[str]:
    try:
        insp = _inspect(engine)
        return {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return set()

def _table_exists(table: str) -> bool:
    try:
        insp = _inspect(engine)
        return insp.has_table(table)
    except Exception:
        return False

if not _PWHASH_COL:
    cols = _table_columns("users")
    if "password_hash" in cols:
        _PWHASH_COL = "password_hash"
    elif "hashed_password" in cols:
        _PWHASH_COL = "hashed_password"
    else:
        _PWHASH_COL = "password_hash"

class User(Base):
    """Core user model."""
    __tablename__ = "users"
    __mapper_args__ = {"eager_defaults": True}

    # Identity
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    username: Mapped[Optional[str]] = mapped_column(String(80), index=True, default=None)
    full_name: Mapped[Optional[str]] = mapped_column(String(120), default=None)

    # ── Auth / status
    # Normalize hash column via synonym so code can use either name.
    if _PWHASH_COL == "hashed_password":
        hashed_password: Mapped[Optional[str]] = mapped_column("hashed_password", String(255), default=None)
        password_hash = synonym("hashed_password")
    else:
        password_hash: Mapped[Optional[str]] = mapped_column("password_hash", String(255), default=None)
        hashed_password = synonym("password_hash")

    role: Mapped[str] = mapped_column(String(32), default="user")
    is_active:   Mapped[bool] = mapped_column(Boolean, default=True,  nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    subscription_status: Mapped[Optional[str]] = mapped_column(String(32), default="free")

    # Timestamps
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint("length(email) >= 3", name="ck_user_email_len"),
        Index("ix_users_email_lower", func.lower(email)),
        Index("ix_users_username_lower", func.lower(username)),
        Index("ix_users_is_active_created", "is_active", "created_at"),
    )

    # ========= Relationships =========
    # NB: To prevent login from accidentally querying heavy/optional tables,
    # most collections below use lazy="noload".
    # You can flip to "selectin" where you *intentionally* preload.

    # —— One-to-one / simple
    activity_score: Mapped[Optional["ActivityScore"]] = relationship(
        "ActivityScore", back_populates="user", uselist=False, cascade="all, delete-orphan", lazy="noload",
    )
    ai_bot_settings: Mapped[Optional["AIBotSettings"]] = relationship(
        "AIBotSettings", back_populates="user", uselist=False, cascade="all, delete-orphan", lazy="noload",
    )
    balance: Mapped[Optional["Balance"]] = relationship(
        "Balance", back_populates="user", uselist=False, cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    wallet: Mapped[Optional["SmartCoinWallet"]] = relationship(
        "SmartCoinWallet", back_populates="user", uselist=False, cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    money_wallet: Mapped[Optional["Wallet"]] = relationship(
        "Wallet", back_populates="owner", uselist=False, cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload", doc="Unified fiat/coin wallet",
    )

    # —— Settings & prefs
    settings: Mapped[Optional["UserSettings"]] = relationship(
        "UserSettings", back_populates="user", uselist=False, cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    notification_setting: Mapped[Optional["NotificationSetting"]] = relationship(
        "NotificationSetting", back_populates="user", uselist=False, cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    notification_preferences: Mapped[Optional["NotificationPreference"]] = relationship(
        "NotificationPreference", back_populates="user", uselist=False, cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    kv_settings: Mapped[List["UserKVSetting"]] = relationship(
        "UserKVSetting", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    dnd_windows: Mapped[List["DoNotDisturbWindow"]] = relationship(
        "DoNotDisturbWindow", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    flag_overrides: Mapped[List["FeatureFlagOverride"]] = relationship(
        "FeatureFlagOverride", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )

    # ---------- Bots (AI assistants) ----------
    bots: Mapped[List["UserBot"]] = relationship(
        "UserBot",
        back_populates="user",
        foreign_keys=lambda: [
            _col(_BOT_MODS, "UserBot", "user_id")
        ],
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="noload",
    )

    # Badges (two FKs)
    badge_events_received: Mapped[List["BadgeHistory"]] = relationship(
        "BadgeHistory",
        back_populates="user",
        foreign_keys=lambda: [
            _col(["backend.models.badge_history", "backend.models.badges"], "BadgeHistory", "user_id")
        ],
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="noload",
    )
    badge_events_given: Mapped[List["BadgeHistory"]] = relationship(
        "BadgeHistory",
        back_populates="awarded_by",
        foreign_keys=lambda: [
            _col(["backend.models.badge_history", "backend.models.badges"], "BadgeHistory", "awarded_by_id")
        ],
        lazy="noload",
    )

    @property
    def badge_history(self) -> List["BadgeHistory"]:
        events = (self.badge_events_received or []) + (self.badge_events_given or [])
        base = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
        return sorted(events, key=lambda ev: getattr(ev, "awarded_at", None) or base, reverse=True)

    # Notifications (two FKs)
    notifications_received: Mapped[List["Notification"]] = relationship(
        "Notification",
        back_populates="recipient",
        foreign_keys=lambda: [
            _col(["backend.models.notification", "backend.models.notifications"], "Notification", "user_id")
        ],
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="noload",
    )
    notifications_sent: Mapped[List["Notification"]] = relationship(
        "Notification",
        back_populates="actor",
        foreign_keys=lambda: [
            _col(["backend.models.notification", "backend.models.notifications"], "Notification", "actor_user_id")
        ],
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="noload",
    )
    push_subscriptions: Mapped[List["PushSubscription"]] = relationship(
        "PushSubscription", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )

    # Guests / Co-hosts (two distinct FKs)
    guest_entries: Mapped[List["Guest"]] = relationship(
        "Guest",
        primaryjoin=lambda: _col(
            ["backend.models.guest", "backend.models.guests"], "Guest", "user_id"
        ) == User.id,
        foreign_keys=lambda: [
            _col(["backend.models.guest", "backend.models.guests"], "Guest", "user_id")
        ],
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    guest_approvals: Mapped[List["Guest"]] = relationship(
        "Guest",
        primaryjoin=lambda: _col(
            ["backend.models.guest", "backend.models.guests"], "Guest", "approved_by_user_id"
        ) == User.id,
        foreign_keys=lambda: [
            _col(["backend.models.guest", "backend.models.guests"], "Guest", "approved_by_user_id")
        ],
        back_populates="approved_by",
        cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    co_host_as_host: Mapped[List["CoHost"]] = relationship(
        "CoHost",
        back_populates="host",
        foreign_keys=lambda: [
            _col(["backend.models.co_host", "backend.models.cohost"], "CoHost", "host_user_id")
        ],
        cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    co_host_as_cohost: Mapped[List["CoHost"]] = relationship(
        "CoHost",
        back_populates="cohost",
        foreign_keys=lambda: [
            _col(["backend.models.co_host", "backend.models.cohost"], "CoHost", "cohost_user_id")
        ],
        cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )

    # Live sessions / history
    live_sessions: Mapped[List["LiveSession"]] = relationship(
        "LiveSession", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    login_history: Mapped[List["LoginHistory"]] = relationship(
        "LoginHistory", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    magic_links: Mapped[List["MagicLink"]] = relationship(
        "MagicLink", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )

    # Chat / messages
    messages: Mapped[List["Message"]] = relationship(
        "Message", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    message_logs: Mapped[List["MessageLog"]] = relationship(
        "MessageLog", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    scheduled_messages: Mapped[List["ScheduledMessage"]] = relationship(
        "ScheduledMessage", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    scheduled_tasks: Mapped[List["ScheduledTask"]] = relationship(
        "ScheduledTask", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )

    # Social / posts
    posts: Mapped[List["SocialMediaPost"]] = relationship(
        "SocialMediaPost", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    post_logs: Mapped[List["PostLog"]] = relationship(
        "PostLog", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    likes: Mapped[List["Like"]] = relationship(
        "Like", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    video_comments: Mapped[List["VideoComment"]] = relationship(
        "VideoComment", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )

    # View stats
    view_stats: Mapped[List["VideoViewStat"]] = relationship(
        "VideoViewStat",
        back_populates="viewer",
        primaryjoin="VideoViewStat.viewer_user_id == User.id",
        lazy="noload",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # Commerce
    orders: Mapped[List["Order"]] = relationship(
        "Order", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    payments: Mapped[List["Payment"]] = relationship(
        "Payment", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    recharges: Mapped[List["RechargeTransaction"]] = relationship(
        "RechargeTransaction", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )

    # ------------ Withdraws (two FKs to users) -------------
    withdraw_requests: Mapped[List["WithdrawRequest"]] = relationship(
        "WithdrawRequest",
        back_populates="user",
        primaryjoin=lambda: _col(
            ["backend.models.withdraw_request", "backend.models.withdrawrequests"],
            "WithdrawRequest",
            "user_id",
        ) == User.id,
        foreign_keys=lambda: [
            _col(
                ["backend.models.withdraw_request", "backend.models.withdrawrequests"],
                "WithdrawRequest",
                "user_id",
            )
        ],
        cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    withdraw_approvals: Mapped[List["WithdrawRequest"]] = relationship(
        "WithdrawRequest",
        back_populates="approved_by",
        primaryjoin=lambda: _col(
            ["backend.models.withdraw_request", "backend.models.withdrawrequests"],
            "WithdrawRequest",
            "approved_by_user_id",
        ) == User.id,
        foreign_keys=lambda: [
            _col(
                ["backend.models.withdraw_request", "backend.models.withdrawrequests"],
                "WithdrawRequest",
                "approved_by_user_id",
            )
        ],
        lazy="noload",
    )
    # -------------------------------------------------------

    # SmartCoin legacy
    smart_coin_transactions: Mapped[List["SmartCoinTransaction"]] = relationship(
        "SmartCoinTransaction", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    coin_transactions = synonym("smart_coin_transactions")

    # CRM: Customers
    customers: Mapped[List["Customer"]] = relationship(
        "Customer", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )

    # CRM: CustomerFeedback (two distinct FKs)
    customer_feedbacks: Mapped[List["CustomerFeedback"]] = relationship(
        "CustomerFeedback",
        back_populates="user",
        foreign_keys=lambda: [_cf.CustomerFeedback.user_id],
        cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    feedbacks_assigned: Mapped[List["CustomerFeedback"]] = relationship(
        "CustomerFeedback",
        back_populates="assignee",
        foreign_keys=lambda: [_cf.CustomerFeedback.assigned_to_user_id],
        lazy="noload",
    )

    # Drone / platform
    drone_missions: Mapped[List["DroneMission"]] = relationship(
        "DroneMission", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    platform_statuses: Mapped[List["PlatformStatus"]] = relationship(
        "PlatformStatus", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )

    # Devices + audits
    user_devices: Mapped[List["UserDevice"]] = relationship(
        "UserDevice", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    devices = synonym("user_devices")

    device_settings: Mapped[List["UserDeviceSetting"]] = relationship(
        "UserDeviceSetting", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    settings_audits: Mapped[List["SettingsAudit"]] = relationship(
        "SettingsAudit",
        back_populates="actor",
        foreign_keys=lambda: [
            _col(["backend.models.setting", "backend.models.settings"], "SettingsAudit", "actor_user_id")
        ],
        lazy="noload",
    )

    # Social graph
    following_hosts: Mapped[List["Fan"]] = relationship(
        "Fan",
        back_populates="fan",
        foreign_keys=lambda: [
            _col(["backend.models.fan", "backend.models.fans"], "Fan", "user_id")
        ],
        cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    host_followers: Mapped[List["Fan"]] = relationship(
        "Fan",
        back_populates="host",
        foreign_keys=lambda: [
            _col(["backend.models.fan", "backend.models.fans"], "Fan", "host_user_id")
        ],
        cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )

    # Gifts / coins
    gift_fly_events: Mapped[List["GiftFly"]] = relationship(
        "GiftFly", back_populates="user", passive_deletes=True, lazy="noload",
    )
    gift_movements_sent: Mapped[List["GiftMovement"]] = relationship(
        "GiftMovement",
        back_populates="sender",
        foreign_keys=lambda: [
            _col(["backend.models.gift_movement", "backend.models.giftmovement"], "GiftMovement", "sender_id")
        ],
        cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    gift_movements_received: Mapped[List["GiftMovement"]] = relationship(
        "GiftMovement",
        back_populates="host",
        foreign_keys=lambda: [
            _col(["backend.models.gift_movement", "backend.models.giftmovement"], "GiftMovement", "host_id")
        ],
        cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    gift_transactions_sent: Mapped[List["GiftTransaction"]] = relationship(
        "GiftTransaction",
        back_populates="sender",
        foreign_keys=lambda: [
            _col(["backend.models.gift_transaction", "backend.models.gifttransaction"], "GiftTransaction", "sender_id")
        ],
        cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    gift_transactions_received: Mapped[List["GiftTransaction"]] = relationship(
        "GiftTransaction",
        back_populates="recipient",
        foreign_keys=lambda: [
            _col(["backend.models.gift_transaction", "backend.models.gifttransaction"], "GiftTransaction", "recipient_id")
        ],
        cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )

    # Engagement
    leaderboard_notifications: Mapped[List["LeaderboardNotification"]] = relationship(
        "LeaderboardNotification", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    post_live_notifications: Mapped[List["PostLiveNotification"]] = relationship(
        "PostLiveNotification", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    goals: Mapped[List["Goal"]] = relationship(
        "Goal",
        back_populates="creator",
        foreign_keys=lambda: [
            _col(
                ["backend.models.goal", "backend.models.goals", "backend.models.goal_model"],
                "Goal",
                "creator_id",
            )
        ],
        cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    top_contributions: Mapped[List["TopContributor"]] = relationship(
        "TopContributor", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )

    # Loyalty / moderation / support
    loyalty_points: Mapped[List["LoyaltyPoint"]] = relationship(
        "LoyaltyPoint",
        back_populates="user",
        foreign_keys=lambda: [
            _col(
                ["backend.models.loyalty", "backend.models.loyalty_points", "backend.models.loyaltypoint"],
                "LoyaltyPoint",
                "user_id",
            )
        ],
        cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    moderations_received: Mapped[List["ModerationAction"]] = relationship(
        "ModerationAction",
        back_populates="target",
        foreign_keys=lambda: [
            _col(["backend.models.moderation_action", "backend.models.moderationaction"], "ModerationAction", "target_user_id")
        ],
        cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    moderations_taken: Mapped[List["ModerationAction"]] = relationship(
        "ModerationAction",
        back_populates="moderator",
        foreign_keys=lambda: [
            _col(["backend.models.moderation_action", "backend.models.moderationaction"], "ModerationAction", "moderator_id")
        ],
        cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )

    # ✅ SupportTicket relations (match support.py: assigned_to)
    support_tickets: Mapped[List["SupportTicket"]] = relationship(
        "SupportTicket",
        back_populates="user",
        foreign_keys=lambda: [
            _fcol(_SUPPORT_MODS, "SupportTicket", ["user_id"])
        ],
        cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    support_tickets_assigned: Mapped[List["SupportTicket"]] = relationship(
        "SupportTicket",
        back_populates="assignee",
        foreign_keys=lambda: [
            _fcol(
                _SUPPORT_MODS,
                "SupportTicket",
                ["assigned_to", "assigned_to_user_id", "assignee_user_id", "assigned_user_id", "agent_user_id", "staff_user_id", "assigned_id"],
            )
        ],
        lazy="noload",
    )

    # ✅ Forgot Password / Password Reset
    forgot_password_requests: Mapped[List["ForgotPasswordRequest"]] = relationship(
        "ForgotPasswordRequest",
        back_populates="user",
        foreign_keys=lambda: [
            _fcol(_AUTH_MODS, "ForgotPasswordRequest", ["user_id", "owner_user_id", "account_user_id"])
        ],
        cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )

    # Ads / earnings
    ad_earnings: Mapped[List["AdEarning"]] = relationship(
        "AdEarning", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )

    # Referrals
    referrals_made: Mapped[List["ReferralLog"]] = relationship(
        "ReferralLog",
        back_populates="referrer",
        foreign_keys=lambda: [
            _col(["backend.models.referral_log", "backend.models.referrallog"], "ReferralLog", "referrer_id")
        ],
        cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    referrals = synonym("referrals_made")
    referrals_received: Mapped[List["ReferralLog"]] = relationship(
        "ReferralLog",
        back_populates="referred_user",
        foreign_keys=lambda: [
            _col(["backend.models.referral_log", "backend.models.referrallog"], "ReferralLog", "referred_user_id")
        ],
        cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    referral_bonuses_made: Mapped[List["ReferralBonus"]] = relationship(
        "ReferralBonus",
        back_populates="referrer",
        foreign_keys=lambda: [
            _col(["backend.models.referral_bonus", "backend.models.referralbonus"], "ReferralBonus", "referrer_id")
        ],
        cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    referral_bonuses_received: Mapped[List["ReferralBonus"]] = relationship(
        "ReferralBonus",
        back_populates="referred_user",
        foreign_keys=lambda: [
            _col(["backend.models.referral_bonus", "backend.models.referralbonus"], "ReferralBonus", "referred_user_id")
        ],
        cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    referral_bonuses = synonym("referral_bonuses_made")
    referral_bonuses_given = synonym("referral_bonuses_made")

    # Telemetry / webhooks / tokens
    search_logs: Mapped[List["SearchLog"]] = relationship(
        "SearchLog", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    error_logs: Mapped[List["ErrorLog"]] = relationship(
        "ErrorLog", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    token_usage_logs: Mapped[List["TokenUsageLog"]] = relationship(
        "TokenUsageLog", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    webhook_endpoints: Mapped[List["WebhookEndpoint"]] = relationship(
        "WebhookEndpoint", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    webhooks = synonym("webhook_endpoints")

    # ⚠️ Key change: set to noload to avoid accidental SELECTs during login
    webhook_delivery_logs: Mapped[List["WebhookDeliveryLog"]] = relationship(
        "WebhookDeliveryLog",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="noload",  # ← this prevents implicit DB load (and avoids errors if table missing)
    )

    subscriptions: Mapped[List["UserSubscription"]] = relationship(
        "UserSubscription", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )
    campaign_affiliations: Mapped[List["CampaignAffiliate"]] = relationship(
        "CampaignAffiliate", back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )

    # Optional: share activities
    share_activities: Mapped[List["ShareActivity"]] = relationship(
        "ShareActivity", cascade="all, delete-orphan",
        passive_deletes=True, lazy="noload",
    )

    # —— Helpers
    def set_password(self, raw: str) -> None:
        try:
            from backend.utils.security import get_password_hash  # type: ignore
            h = get_password_hash(raw)
        except Exception:
            h = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if hasattr(self, "password_hash"):
            self.password_hash = h
        else:
            self.hashed_password = h

    def verify_password(self, raw: str) -> bool:
        hashed = None
        if hasattr(self, "password_hash"):
            hashed = self.password_hash
        if not hashed and hasattr(self, "hashed_password"):
            hashed = self.hashed_password
        hashed = hashed or ""
        if not hashed:
            return False
        try:
            from backend.utils.security import verify_password  # type: ignore
            return bool(verify_password(raw, hashed))
        except Exception:
            return hashlib.sha256(raw.encode("utf-8")).hexdigest() == hashed

    @property
    def name(self) -> str:
        return self.full_name or self.username or self.email

    @property
    def has_password(self) -> bool:
        if hasattr(self, "password_hash"):
            return bool(self.password_hash)
        return bool(self.hashed_password)

    @property
    def is_owner(self) -> bool:
        return (self.role or "").lower() == "owner"

    @property
    def is_admin(self) -> bool:
        return (self.role or "").lower() == "admin"

    @property
    def is_staff(self) -> bool:
        return self.is_admin or self.is_owner

    def has_role(self, *roles: str) -> bool:
        r = (self.role or "").lower()
        return any(r == x.lower() for x in roles)

    @property
    def referred_logs(self):
        return self.referrals_received

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User id={self.id} email={self.email} role={self.role} active={self.is_active}>"

# —— Normalization hooks
@validates("email")
def _validate_email_lower(cls, value: str) -> str:  # type: ignore[override]
    return (value or "").strip().lower()

@validates("username")
def _validate_username_lower(cls, value: Optional[str]) -> Optional[str]:  # type: ignore[override]
    if value is None:
        return None
    v = (value or "").strip()
    v = " ".join(v.split())
    return v.lower() or None

@listens_for(User, "before_insert")
def _user_before_insert(_mapper, _connection, target: User) -> None:
    if target.email:
        target.email = target.email.strip().lower()
    if target.username:
        target.username = " ".join(target.username.strip().split()).lower()

@listens_for(User, "before_update")
def _user_before_update(_mapper, _connection, target: User) -> None:
    if target.email:
        target.email = target.email.strip().lower()
    if target.username:
        target.username = " ".join(target.username.strip().split()).lower()
