"""Explorer route façade."""

from fastapi import APIRouter

from api.v1.explore.dep import _build_preview_service
from api.v1.explore.down import router as down_router
from api.v1.explore.node import router as node_router
from api.v1.explore.up import router as up_router
from api.v1.explore.view import router as view_router


router = APIRouter(prefix="/explorer", tags=["explorer"])
router.include_router(node_router)
router.include_router(up_router)
router.include_router(down_router)
router.include_router(view_router)

__all__ = ["router"]
