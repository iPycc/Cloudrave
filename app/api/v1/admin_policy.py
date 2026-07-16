"""Admin storage policy routes."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Request, status

from app.middleware.auth_context import get_current_user
from app.models.tables import User
from app.schemas.common import MessageResponse
from app.schemas.policy import StoragePolicyCreate, StoragePolicyRead, StoragePolicyUpdate
from app.security.rbac import require_admin
from app.services.admin.policy import SingleStoragePolicyService


router = APIRouter(prefix="/admin/policy", tags=["admin-policy"])


@router.get("", response_model=list[StoragePolicyRead])
async def list_policies(request: Request, current_user: User = Depends(get_current_user)) -> list[StoragePolicyRead]:
    require_admin(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = SingleStoragePolicyService(
            session=session,
            policies=dep.get_policy_repository(session),
            audits=dep.get_audit_repository(session),
        )
        return await service.list_policies()


@router.get("/{policy_id}", response_model=StoragePolicyRead)
async def get_policy(policy_id: int, request: Request, current_user: User = Depends(get_current_user)) -> StoragePolicyRead:
    require_admin(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = SingleStoragePolicyService(
            session=session,
            policies=dep.get_policy_repository(session),
            audits=dep.get_audit_repository(session),
        )
        return await service.get_policy(policy_id)


@router.post("", response_model=StoragePolicyRead, status_code=status.HTTP_201_CREATED)
async def create_policy(
    request: Request,
    payload: StoragePolicyCreate = Body(...),
    current_user: User = Depends(get_current_user),
) -> StoragePolicyRead:
    require_admin(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = SingleStoragePolicyService(
            session=session,
            policies=dep.get_policy_repository(session),
            audits=dep.get_audit_repository(session),
        )
        return await service.create_policy(request, current_user, payload)


@router.patch("/{policy_id}", response_model=StoragePolicyRead)
async def update_policy(
    policy_id: int,
    request: Request,
    payload: StoragePolicyUpdate = Body(...),
    current_user: User = Depends(get_current_user),
) -> StoragePolicyRead:
    require_admin(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = SingleStoragePolicyService(
            session=session,
            policies=dep.get_policy_repository(session),
            audits=dep.get_audit_repository(session),
        )
        return await service.update_policy(request, current_user, policy_id, payload)


@router.delete("/{policy_id}", response_model=MessageResponse)
async def delete_policy(policy_id: int, request: Request, current_user: User = Depends(get_current_user)) -> MessageResponse:
    require_admin(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = SingleStoragePolicyService(
            session=session,
            policies=dep.get_policy_repository(session),
            audits=dep.get_audit_repository(session),
        )
        await service.delete_policy(request, current_user, policy_id)
        return MessageResponse(message="Storage policy deleted")
