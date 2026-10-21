"""Credential rotation and reauthorization state shared by foreground/background IO."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from threading import RLock

from sqlalchemy import select, update

from db.base import session_scope
from drives.remote_drive.common import RemoteAuthenticationError
from models.tables import StoragePolicy


AUTH_KEY = "_remote_auth"
PROVIDERS = {"drive_139": "移动云盘", "drive_189": "天翼云盘", "drive_wo": "联通云盘"}


def credential_fingerprint(ciphertext: str | None) -> str:
    return hashlib.sha256((ciphertext or "").encode()).hexdigest()


def reauthorization_message(provider: str, name: str = "") -> str:
    label = PROVIDERS.get(provider, "运营商云盘")
    target = f"{label}「{name}」" if name else label
    return f"{target}登录信息已失效，无法自动恢复。请在运营商网页重新登录，并在 Cloudrave 的“设置 → 存储”中编辑该云盘、更新登录信息后重新验证。"


@dataclass
class _State:
    ciphertext: str | None
    requires_reauthorization: bool
    revision: int = 0
    persisted_revision: int = 0
    lock: RLock = field(default_factory=RLock)
    operation_lock: RLock = field(default_factory=RLock)


class RemoteCredentialRuntime:
    def __init__(self, vault) -> None:
        self.vault = vault
        self._states: dict[tuple[int, str | None], _State] = {}
        self._lock = RLock()

    def _state(self, policy) -> _State:
        key = (policy.id, policy.credential_ciphertext)
        with self._lock:
            if key not in self._states:
                marker = (policy.extra or {}).get(AUTH_KEY) or {}
                if not isinstance(marker, dict):
                    marker = {}
                invalid = (
                    marker.get("credential_fingerprint") == credential_fingerprint(key[1])
                    and marker.get("requires_reauthorization") is True
                )
                self._states[key] = _State(key[1], invalid)
            return self._states[key]

    def status(self, policy) -> bool:
        return self._state(policy).requires_reauthorization

    def synchronize(self, policy) -> None:
        """Save successful rotations with the caller's transaction on its thread."""
        state = self._state(policy)
        with state.lock:
            if state.requires_reauthorization or state.ciphertext == policy.credential_ciphertext:
                return
            with self._lock:
                self._states[(policy.id, state.ciphertext)] = state
            policy.credential_ciphertext = state.ciphertext

    def invoke(self, adapter, method: str, policy, context, args, kwargs):
        state = self._state(policy)
        with state.operation_lock:
            if state.requires_reauthorization:
                raise RemoteAuthenticationError(reauthorization_message(adapter.provider_type, policy.name))
            context.credentials = self.vault.open(state.ciphertext)
            before = dict(context.credentials)
            try:
                return getattr(adapter, method)(*args, **kwargs)
            except RemoteAuthenticationError as exc:
                with state.lock:
                    state.requires_reauthorization = True
                    state.revision += 1
                raise RemoteAuthenticationError(
                    reauthorization_message(adapter.provider_type, policy.name), code=exc.code
                ) from exc
            finally:
                if context.credentials != before:
                    with state.lock:
                        state.ciphertext = self.vault.seal(context.credentials)
                        state.revision += 1

    async def persist(self, session_factory) -> None:
        # Commit outside the failed request transaction, which may have rolled
        # back. Compare credentials so a stale operation cannot overwrite a
        # user's newly authorized account.
        with self._lock:
            states = list(self._states.items())
        for (policy_id, original), state in states:
            with state.lock:
                if state.revision == state.persisted_revision:
                    continue
                revision, ciphertext, invalid = state.revision, state.ciphertext, state.requires_reauthorization
            async with session_scope(session_factory) as session:
                policy = await session.scalar(select(StoragePolicy).where(StoragePolicy.id == policy_id).with_for_update())
                if policy is not None and policy.credential_ciphertext in (original, ciphertext):
                    expected_ciphertext = policy.credential_ciphertext
                    extra = dict(policy.extra or {})
                    if invalid:
                        extra[AUTH_KEY] = {
                            "credential_fingerprint": credential_fingerprint(ciphertext),
                            "requires_reauthorization": True,
                        }
                    else:
                        extra.pop(AUTH_KEY, None)
                    await session.execute(
                        update(StoragePolicy).where(
                            StoragePolicy.id == policy_id,
                            StoragePolicy.credential_ciphertext == expected_ciphertext,
                        ).values(credential_ciphertext=ciphertext, extra=extra)
                        .execution_options(synchronize_session=False)
                    )
            with state.lock:
                state.persisted_revision = revision
