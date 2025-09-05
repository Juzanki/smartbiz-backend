# backend/models/goal.py
# -*- coding: utf-8 -*-
"""
Compat shim: re-export Goal model kutoka goal_model.py
Ili kuruhusu import za zamani `backend.models.goal` bila kubadilisha sehemu zingine za code.
"""

from __future__ import annotations
import warnings

from .goal_model import Goal, GoalType, GoalStatus, GoalUnit  # hakikisha majina yapo sahihi

__all__ = [
    "Goal",
    "GoalType",
    "GoalStatus",
    "GoalUnit",
]

warnings.warn(
    "backend.models.goal imepitwa — tumia backend.models.goal_model moja kwa moja.",
    category=DeprecationWarning,
    stacklevel=2,
)
