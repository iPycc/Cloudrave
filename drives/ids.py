"""Stable identifiers for built-in drives."""

LOCAL_FS = "local_fs"
TENCENT_COS = "tencent_cos"
ALIYUN_OSS = "aliyun_oss"
DRIVE_139 = "drive_139"
DRIVE_189 = "drive_189"
DRIVE_WO = "drive_wo"

BUILTIN_DRIVER_IDS = (
    LOCAL_FS,
    TENCENT_COS,
    ALIYUN_OSS,
    DRIVE_139,
    DRIVE_189,
    DRIVE_WO,
)

# SQLAlchemy's legacy ``Enum(StoragePolicyType)`` column persisted enum member
# names (for example ``TENCENT_COS``), not their lower-case values. Keep the
# compatibility mapping at the drive-ID boundary so an already-upgraded
# database remains readable until the normalization migration is applied.
_LEGACY_PROVIDER_IDS = {driver_id.upper(): driver_id for driver_id in BUILTIN_DRIVER_IDS}


def provider_id(value: object) -> str:
    """Normalize legacy enum values and current string provider IDs."""

    raw = getattr(value, "value", value)
    normalized = str(raw or "").strip()
    return _LEGACY_PROVIDER_IDS.get(normalized, normalized)
