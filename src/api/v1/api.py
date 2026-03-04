from fastapi import APIRouter
from src.api.v1.endpoints import project, meeting

api_router = APIRouter()

api_router.include_router(project.router, prefix="/project", tags=["project"])
api_router.include_router(meeting.router, prefix="/meeting", tags=["meeting"])
