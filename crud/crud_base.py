from __future__ import annotations
# backend/crud/crud_base.py
from typing import Any, Dict, Generic, List, Optional, Sequence, Tuple, Type, TypeVar
from sqlalchemy.orm import Session
from sqlalchemy import select
from pydantic import BaseModel

ModelType = TypeVar("ModelType")
CreateSchemaType = TypeVar("CreateSchemaType", bound=BaseModel)
UpdateSchemaType = TypeVar("UpdateSchemaType", bound=BaseModel)

class CRUDBase(Generic[ModelType, CreateSchemaType, UpdateSchemaType]):
    def __init__(self, model: Type[ModelType]):
        self.model = model

    # ----- READ -----
    def get(self, db: Session, id: Any) -> Optional[ModelType]:
        return db.get(self.model, id)

    def list(
        self, db: Session, *, offset: int = 0, limit: int = 50, filters: Optional[Dict[str, Any]] = None
    ) -> Tuple[List[ModelType], int]:
        stmt = select(self.model)
        if filters:
            for name, value in filters.items():
                if value is None:
                    continue
                col = getattr(self.model, name, None)
                if col is not None:
                    stmt = stmt.where(col == value)
        total = db.execute(stmt).unique().scalars().all()
        items = total[offset : offset + limit]
        return items, len(total)

    # ----- CREATE -----
    def create(self, db: Session, *, obj_in: CreateSchemaType) -> ModelType:
        data = obj_in.model_dump() if hasattr(obj_in, "model_dump") else obj_in.dict()
        db_obj = self.model(**data)
        db.add(db_obj)
        db.commit()
        db.refresh(db_obj)
        return db_obj

    # ----- UPDATE (partial) -----
    def update(self, db: Session, *, db_obj: ModelType, obj_in: UpdateSchemaType | Dict[str, Any]) -> ModelType:
        if isinstance(obj_in, BaseModel):
            update_data = obj_in.model_dump(exclude_unset=True)
        else:
            update_data = {k: v for k, v in obj_in.items() if v is not None}
        for field, value in update_data.items():
            if hasattr(db_obj, field):
                setattr(db_obj, field, value)
        db.add(db_obj)
        db.commit()
        db.refresh(db_obj)
        return db_obj

    # ----- DELETE -----
    def delete(self, db: Session, *, id: Any) -> None:
        obj = self.get(db, id)
        if obj is None:
            return
        db.delete(obj)
        db.commit()
