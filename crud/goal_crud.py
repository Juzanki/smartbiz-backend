from __future__ import annotations
﻿from backend.schemas.user import UserOut
from sqlalchemy.orm import Session
from backend.models.goal_model import Goal
from backend.schemas.goal import GoalCreate

def create_goal(db: Session, goal: GoalCreate):
    new_goal = Goal(**goal.dict())
    db.add(new_goal)
    db.commit()
    db.refresh(new_goal)
    return new_goal

def get_all_goals(db: Session):
    return db.query(Goal).all()

def update_goal_progress(db: Session, goal_id: int, amount: float):
    goal = db.query(Goal).filter(Goal.id == goal_id).first()
    if goal:
        goal.current_value += amount
        db.commit()
        db.refresh(goal)
    return goal

def delete_goal(db: Session, goal_id: int):
    goal = db.query(Goal).filter(Goal.id == goal_id).first()
    if goal:
        db.delete(goal)
        db.commit()
    return goal

