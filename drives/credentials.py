"""Versioned encryption for provider credentials."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping

from cryptography.fernet import Fernet, InvalidToken


class CredentialVault:
    VERSION = 1

    def __init__(self, secret: str) -> None:
        material = secret.strip()
        if len(material) < 32:
            raise RuntimeError("Storage credential key must contain at least 32 characters")
        key = base64.urlsafe_b64encode(hashlib.sha256(material.encode("utf-8")).digest())
        self._fernet = Fernet(key)

    def seal(self, credentials: Mapping[str, object]) -> str:
        payload = {
            str(key): str(value)
            for key, value in credentials.items()
            if value is not None and str(value).strip()
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return self._fernet.encrypt(encoded.encode("utf-8")).decode("ascii")

    def open(self, ciphertext: str | None) -> dict[str, str]:
        if not ciphertext:
            return {}
        try:
            plaintext = self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise RuntimeError(
                "Stored drive credentials cannot be decrypted. Restore the configured storage credential key."
            ) from exc
        value = json.loads(plaintext)
        if not isinstance(value, dict):
            raise RuntimeError("Stored drive credentials have an invalid payload")
        return {str(key): str(item) for key, item in value.items() if item is not None}
