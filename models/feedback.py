# backend/models/feedback.py
# -*- coding: utf-8 -*-
"""
Compat shim: re-export CustomerFeedback and its enums from customer_feedback.

Hii inazuia double-mapping ya jedwali 'customer_feedbacks' (ambayo 
ingeleta sqlalchemy.exc.InvalidRequestError) huku ikiruhusu import 
za zamani kama `from backend.models.feedback import CustomerFeedback`.
"""

from __future__ import annotations
import warnings

# Re-export everything from the canonical module
from .customer_feedback import (
    CustomerFeedback,
    FeedbackType,
    FeedbackSource,
    FeedbackStatus,
    Sentiment,
)

# (optional) alias ya urafiki kama mtu alitumia jina "Feedback"
Feedback = CustomerFeedback  # backward-compat

__all__ = [
    "CustomerFeedback",
    "Feedback",            # alias
    "FeedbackType",
    "FeedbackSource",
    "FeedbackStatus",
    "Sentiment",
]

# Toa onyo la upole mara moja ili wahamie kwenye module mpya
warnings.warn(
    "backend.models.feedback imepitwa — tumia backend.models.customer_feedback.",
    category=DeprecationWarning,
    stacklevel=2,
)
