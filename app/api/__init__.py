from fastapi import APIRouter

from app.api.employer_routes import router as employer_router
from app.api.routes import router as core_router

router = APIRouter()
router.include_router(core_router)
router.include_router(employer_router, prefix="/api/v1")

__all__ = ["router"]
