"""One-way Stage 4 server progress -> Calibre custom-column synchronization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import Any


MAPPING_KEYS = ("progress", "status", "last_location", "last_sync")


@dataclass(frozen=True)
class ProgressSnapshot:
    """Normalized effective server reading state for one bound Calibre book."""

    progress: float
    status: str
    last_location: str
    last_sync: datetime | None


@dataclass(frozen=True)
class ApplyResult:
    """Result of applying one normalized snapshot to mapped Calibre columns."""

    changed_fields: tuple[str, ...]
    changed_book_ids: frozenset[int]


def _percentage_points(value: Any) -> float:
    try:
        fraction = float(value)
    except (TypeError, ValueError):
        fraction = 0.0
    if not isfinite(fraction):
        fraction = 0.0
    fraction = max(0.0, min(1.0, fraction))
    return fraction * 100.0


def _server_datetime(value: Any) -> datetime | None:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(timestamp) or timestamp <= 0:
        return None

    # KOSync timestamps are Unix seconds. Tolerate millisecond timestamps from
    # compatible servers without changing the Stage 4 wire contract.
    if timestamp >= 100_000_000_000:
        timestamp /= 1000.0

    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def snapshot_from_progress(payload: dict[str, Any] | None) -> ProgressSnapshot:
    """Normalize a KOSync progress response into Calibre-facing values.

    A missing standard-KOSync progress row is a legitimate "not started"
    state. Enhanced callers pass the already-resolved logical/raw effective
    state while the bound raw document remains the permanent identity.
    """

    source = payload if isinstance(payload, dict) else {}
    progress = _percentage_points(source.get("percentage"))
    reading_state = source.get("reading_state")
    manual_completion = (
        isinstance(reading_state, dict)
        and reading_state.get("manual_completion") is True
    )
    if manual_completion or progress >= 100.0:
        status = "Finished"
    elif progress <= 0:
        status = "Not started"
    else:
        status = "Reading"

    return ProgressSnapshot(
        progress=progress,
        status=status,
        last_location=str(source.get("progress") or "").strip(),
        last_sync=_server_datetime(source.get("timestamp")),
    )


def _same_value(current: Any, desired: Any) -> bool:
    if isinstance(current, datetime) and isinstance(desired, datetime):
        try:
            return current.astimezone(timezone.utc) == desired.astimezone(timezone.utc)
        except (OverflowError, ValueError):
            return current == desired

    if isinstance(current, (int, float)) and isinstance(desired, (int, float)):
        try:
            return abs(float(current) - float(desired)) < 1e-9
        except (TypeError, ValueError):
            return current == desired

    if desired == "" and current is None:
        return True
    return current == desired


def snapshot_values(snapshot: ProgressSnapshot) -> dict[str, Any]:
    return {
        "progress": snapshot.progress,
        "status": snapshot.status,
        "last_location": snapshot.last_location,
        "last_sync": snapshot.last_sync,
    }


def apply_progress_snapshot(
    db: Any,
    book_id: int,
    mappings: dict[str, str],
    snapshot: ProgressSnapshot,
) -> ApplyResult:
    """Write only changed mapped values for exactly one Calibre row."""

    api = getattr(db, "new_api", None)
    if api is None:
        raise RuntimeError("The active Calibre library does not expose the database API.")

    changed_fields: list[str] = []
    changed_ids: set[int] = set()
    values = snapshot_values(snapshot)

    for mapping_key in MAPPING_KEYS:
        lookup = str(mappings.get(mapping_key) or "").strip()
        if not lookup:
            continue

        desired = values[mapping_key]
        try:
            current = api.field_for(lookup, int(book_id), default_value=None)
        except TypeError:
            current = api.field_for(lookup, int(book_id))
        if _same_value(current, desired):
            continue

        affected = api.set_field(lookup, {int(book_id): desired})
        changed_fields.append(mapping_key)
        if affected:
            changed_ids.update(int(value) for value in affected)
        else:
            changed_ids.add(int(book_id))

    return ApplyResult(
        changed_fields=tuple(changed_fields),
        changed_book_ids=frozenset(changed_ids),
    )
