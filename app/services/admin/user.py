"""Admin user service helpers."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class RegistrationRoleDecision:
    role: str
    is_first_user: bool


class AdminUserService:

    def resolve_registration_role(self, *, is_first_user: bool) -> RegistrationRoleDecision:
        return RegistrationRoleDecision(
            role="admin" if is_first_user else "user",
            is_first_user=is_first_user,
        )

