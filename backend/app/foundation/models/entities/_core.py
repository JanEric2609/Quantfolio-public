from datetime import UTC, datetime
from uuid import uuid4


def uuid_pk() -> str:
    return str(uuid4())


def now_utc() -> datetime:
    return datetime.now(UTC)


ASSET_TYPES = [
    "equities",
    "bonds",
    "cash",
    "crypto",
    "real_estate",
    "commodities",
]
