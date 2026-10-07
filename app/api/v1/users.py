from datetime import datetime

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel

from app.api.deps import CurrentUser, DbSession, limit_register_by_ip
from app.services.users import create_user

router = APIRouter(prefix="/users", tags=["users"])


class UserOut(BaseModel):
    id: int
    created_at: datetime


class UserCreated(UserOut):
    api_key: str


@router.post(
    "",
    response_model=UserCreated,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(limit_register_by_ip)],
)
def register_user(request: Request, db: DbSession) -> UserCreated:
    user, api_key = create_user(db, request.app.state.settings.api_key_pepper)
    return UserCreated(id=user.id, created_at=user.created_at, api_key=api_key)


@router.get("/me", response_model=UserOut)
def read_current_user(user: CurrentUser) -> UserOut:
    return UserOut(id=user.id, created_at=user.created_at)
