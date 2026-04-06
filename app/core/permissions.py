from fastapi import Depends, HTTPException, Header
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.user import User
from app.core.security import decode_token


def get_current_user(authorization: str = Header(None), db: Session = Depends(get_db)):
    if not authorization:
        raise HTTPException(401, "Not authenticated")

    token = authorization.split(" ")[-1]
    payload = decode_token(token)
    user_id = int(payload.get("sub", 0))

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(401, "User not found")

    return user


def require_permission(permission: str):
    def checker(user: User = Depends(get_current_user)):
        if user.role == "admin":
            return user

        perms = (user.permissions or "").split(",")

        if permission not in perms:
            raise HTTPException(403, "Permission denied")

        return user

    return checker