"""Password hashing helpers."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


class PasswordService:
    def __init__(self, password_salt: str = "") -> None:
        self._hasher = PasswordHasher()
        self._password_salt = password_salt.strip()

    def hash_password(self, password: str) -> str:
        return self._hasher.hash(self._apply_salt(password))

    def verify_password(self, password: str, password_hash: str) -> bool:
        candidates = [self._apply_salt(password)]
        if self._password_salt:
            # Keep older hashes valid for installs created before the global salt existed.
            candidates.append(password)

        for candidate in candidates:
            try:
                return self._hasher.verify(password_hash, candidate)
            except VerifyMismatchError:
                continue
            except (InvalidHashError, VerificationError):
                return False

        return False

    def _apply_salt(self, password: str) -> str:
        if not self._password_salt:
            return password
        return f"{password}{self._password_salt}"
