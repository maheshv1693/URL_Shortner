from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import generate_api_key, hash_api_key
from app.models import User


def create_user(db: Session, pepper: str) -> tuple[User, str]:
    api_key = generate_api_key()
    user = User(api_key_hash=hash_api_key(api_key, pepper))
    db.add(user)
    db.commit()
    return user, api_key


def get_user_by_api_key(db: Session, api_key: str, pepper: str) -> User | None:
    return db.scalar(select(User).where(User.api_key_hash == hash_api_key(api_key, pepper)))
