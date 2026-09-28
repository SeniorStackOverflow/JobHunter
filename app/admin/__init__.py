from app.admin.invite_routes import router as invite_admin_router
from app.admin.phone_routes import router as phone_admin_router
from app.admin.routes import router

router.include_router(phone_admin_router)
router.include_router(invite_admin_router)

__all__ = ["router"]
