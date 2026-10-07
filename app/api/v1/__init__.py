from fastapi import APIRouter

from app.api.v1 import urls, users

api_router = APIRouter()
api_router.include_router(users.router)
api_router.include_router(urls.router)
