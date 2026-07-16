"""Admin user service helpers."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class RegistrationRoleDecision:
    role: str
    is_first_user: bool


class AdminUserService:
    """Small helper for role-related admin decisions.

    The actual user/account workflow will live in the auth module, but this
    helper keeps the storage/admin boundary close to Cloudrave's service split.
    """

    def resolve_registration_role(self, *, is_first_user: bool) -> RegistrationRoleDecision:
        return RegistrationRoleDecision(
            role="admin" if is_first_user else "user",
            is_first_user=is_first_user,
        )

