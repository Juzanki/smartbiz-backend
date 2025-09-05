"""
Access control module for SmartBiz Assistant.
Provides dependency-based plan restriction logic using FastAPI.
"""

from fastapi import Depends, HTTPException, status
from backend.auth import get_current_user
from backend.models.user import User
def require_plan(allowed_plans: list):
    """
    Dependency that checks if the current user has a valid subscription plan.
    Raises HTTP 403 if user's plan is not allowed.
    
    Args:
        allowed_plans (list): List of accepted plan names (e.g., ["Pro", "Business"])

    Returns:
        function: A dependency function for route protection
    """
    def checker(user: User = Depends(get_current_user)):
        if user.subscription_status not in allowed_plans:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: Upgrade your plan"
            )
        return user

    return checker
    
def require_plan(required_plans: list[str]):
    def _check_subscription(current_user: User = Depends(get_current_user)):
        if current_user.subscription_status not in required_plans:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"ðŸ”’ Feature allowed only for: {', '.join(required_plans)} plan users."
            )
        return current_user
    return _check_subscription
