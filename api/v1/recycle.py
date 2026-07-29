"""Recycle bin routes."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Request

from web.mid.auth import get_current_user
from models.tables import User
from schemas.node import NodeDeleteRequest, NodeRead, NodeRestoreRequest
from services.explorer.recycle import RecycleBinService


router = APIRouter(prefix="/explorer/recycle", tags=["recycle"])


@router.get("", response_model=list[NodeRead])
async def list_recycle_nodes(request: Request, current_user: User = Depends(get_current_user)) -> list[NodeRead]:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = RecycleBinService(
            session=session,
            nodes=dep.get_node_repository(session),
            mounts=dep.get_mount_repository(session),
            policies=dep.get_policy_repository(session),
            objects=dep.get_object_repository(session),
            audits=dep.get_audit_repository(session),
            storage_manager=dep.storage_manager,
        )
        return await service.list_deleted(current_user)


@router.post("/restore", response_model=list[NodeRead])
async def restore_recycle_nodes(
    request: Request,
    payload: NodeRestoreRequest = Body(...),
    current_user: User = Depends(get_current_user),
) -> list[NodeRead]:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = RecycleBinService(
            session=session,
            nodes=dep.get_node_repository(session),
            mounts=dep.get_mount_repository(session),
            policies=dep.get_policy_repository(session),
            objects=dep.get_object_repository(session),
            audits=dep.get_audit_repository(session),
            storage_manager=dep.storage_manager,
        )
        return await service.restore_nodes(request, current_user, payload)


@router.delete("", response_model=dict[str, list[int]])
async def hard_delete_recycle_nodes(
    request: Request,
    payload: NodeDeleteRequest = Body(...),
    current_user: User = Depends(get_current_user),
) -> dict[str, list[int]]:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = RecycleBinService(
            session=session,
            nodes=dep.get_node_repository(session),
            mounts=dep.get_mount_repository(session),
            policies=dep.get_policy_repository(session),
            objects=dep.get_object_repository(session),
            audits=dep.get_audit_repository(session),
            storage_manager=dep.storage_manager,
        )
        return await service.permanently_delete_nodes(request, current_user, payload)
