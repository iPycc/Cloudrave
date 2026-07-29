"""API v1 router aggregation."""

from fastapi import APIRouter

from api.v1.admin_mount import router as admin_mount_router
from api.v1.admin_guest import router as admin_guest_router
from api.v1.admin_policy import router as admin_policy_router
from api.v1.admin_sync import router as admin_sync_router
from api.v1.explorer import router as explorer_router
from api.v1.mount import router as mount_router
from api.v1.recycle import router as recycle_router
from api.v1.session import router as session_router
from api.v1.shared import router as shared_router
from api.v1.share import router as share_router
from api.v1.site import router as site_router
from api.v1.user import router as user_router


router = APIRouter()
router.include_router(session_router)
router.include_router(user_router)
router.include_router(explorer_router)
router.include_router(mount_router)
router.include_router(share_router)
router.include_router(shared_router)
router.include_router(site_router)
router.include_router(admin_policy_router)
router.include_router(admin_guest_router)
router.include_router(admin_mount_router)
router.include_router(admin_sync_router)
router.include_router(recycle_router)
