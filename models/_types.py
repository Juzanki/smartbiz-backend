# -*- coding: utf-8 -*-
from __future__ import annotations

from sqlalchemy import JSON as SA_JSON, Numeric as SA_NUMERIC
from sqlalchemy.ext.mutable import MutableDict, MutableList

try:
    from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB, NUMERIC as PG_NUMERIC  # type: ignore
except Exception:  # pragma: no cover (dev bila PG)
    PG_JSONB = None
    PG_NUMERIC = None

# JSON portable: SQLite/others â†’ JSON, Postgres â†’ JSONB
JSON_VARIANT = SA_JSON().with_variant(PG_JSONB, "postgresql") if PG_JSONB else SA_JSON()

# NUMERIC portable (18,2)
DECIMAL_TYPE = (
    SA_NUMERIC(18, 2).with_variant(PG_NUMERIC(18, 2), "postgresql")
    if PG_NUMERIC else SA_NUMERIC(18, 2)
)

def as_mutable_json(column_type=JSON_VARIANT):
    """Tumia kwa column za dict/list ili changes zirekodiwe na SQLAlchemy."""
    return MutableDict.as_mutable(column_type)

