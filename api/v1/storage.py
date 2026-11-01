"""Authenticated storage driver discovery."""

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status

from models.tables import User
from schemas.driver import StorageDriverInspectRead, StorageDriverInspectRequest, StorageDriverRead
from store.call import call_adapter
from drives.remote_drive.common import RemoteAuthenticationError
from drives.remote_drive.auth import reauthorization_message
from web.mid.auth import get_current_user


router = APIRouter(prefix="/storage", tags=["storage"])


@router.get("/drivers", response_model=list[StorageDriverRead])
async def list_storage_drivers(
    request: Request,
    _current_user: User = Depends(get_current_user),
) -> list[StorageDriverRead]:
    return [StorageDriverRead.model_validate(item) for item in request.app.state.dep.storage_manager.catalog()]


@router.post("/drivers/{provider}/inspect", response_model=StorageDriverInspectRead)
async def inspect_storage_driver(
    provider: str,
    request: Request,
    payload: StorageDriverInspectRequest = Body(...),
    _current_user: User = Depends(get_current_user),
) -> StorageDriverInspectRead:
    manager = request.app.state.dep.storage_manager
    try:
        definition = manager.definition_for(provider)
        credentials = definition.validate_credentials(payload.credentials, require=True)
        adapter = manager.adapters.get(definition.id)
        if adapter is None or not hasattr(adapter, "inspect_credentials"):
            raise ValueError("该存储驱动不支持连接检测")
        account = await call_adapter(adapter, "inspect_credentials", credentials)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Storage drive not found") from exc
    except RemoteAuthenticationError as exc:
        raise RemoteAuthenticationError(reauthorization_message(definition.id), code=exc.code) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"连接验证失败：{exc}",
        ) from exc
    return StorageDriverInspectRead(
        provider=definition.id,
        account_id=account.account_id,
        display_name=account.display_name,
        root_id=account.root_id,
        used_bytes=account.used_bytes,
        total_bytes=account.total_bytes,
        free_bytes=account.free_bytes,
    )
