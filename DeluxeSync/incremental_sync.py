"""Stage 9 change-journal helpers for targeted server -> Calibre refreshes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from calibre_plugins.deluxe_sync.api import ApiError


COMPONENT_PROGRESS = "progress"
COMPONENT_METADATA = "metadata"
COMPONENT_ANNOTATIONS = "annotations"
COMPONENT_VOCABULARY = "vocabulary"

ALL_CHANGE_COMPONENTS = frozenset(
    {
        COMPONENT_PROGRESS,
        COMPONENT_METADATA,
        COMPONENT_ANNOTATIONS,
        COMPONENT_VOCABULARY,
    }
)
_CHANGE_PAGE_LIMIT = 200
_STALE_CURSOR_STATUSES = frozenset({400, 409, 410, 422})


@dataclass(frozen=True)
class ChangeWindow:
    """One validated slice of the account change journal."""

    next_cursor: int
    changes: tuple[dict[str, Any], ...] = ()
    full_refresh: bool = False
    fallback_reason: str = ""


def normalize_change_cursor(value: Any) -> int | None:
    """Return a persisted cursor when it is a usable non-negative integer."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        clean = value.strip()
        if clean.isdigit():
            return int(clean)
    return None


def initialized_change_components(binding: Mapping[str, Any] | None) -> frozenset[str]:
    """Return components that have completed at least one successful refresh."""

    if not isinstance(binding, Mapping):
        return frozenset()
    raw = binding.get("change_cursor_components")
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(
        str(component)
        for component in raw
        if str(component) in ALL_CHANGE_COMPONENTS
    )


def update_change_tracking(
    binding: Mapping[str, Any],
    *,
    cursor: int,
    successful_components: Iterable[str] = (),
    failed_components: Iterable[str] = (),
) -> dict[str, Any]:
    """Return a binding with durable cursor/component state updated safely."""

    normalized_cursor = normalize_change_cursor(cursor)
    if normalized_cursor is None:
        raise ValueError("Change cursor must be a non-negative integer.")

    updated = dict(binding)
    initialized = set(initialized_change_components(binding))
    initialized.update(
        component
        for component in successful_components
        if component in ALL_CHANGE_COMPONENTS
    )
    initialized.difference_update(
        component
        for component in failed_components
        if component in ALL_CHANGE_COMPONENTS
    )
    updated["last_change_cursor"] = normalized_cursor
    updated["change_cursor_components"] = sorted(initialized)
    return updated


def _validated_page(response: Mapping[str, Any], after: int) -> tuple[tuple[dict[str, Any], ...], int]:
    raw_changes = response.get("changes")
    next_cursor = response.get("next_cursor")
    if not isinstance(raw_changes, list):
        raise ApiError("Server change-journal response is missing changes.")
    if (
        not isinstance(next_cursor, int)
        or isinstance(next_cursor, bool)
        or next_cursor < after
    ):
        raise ApiError("Server change-journal response has an invalid next cursor.")

    changes: list[dict[str, Any]] = []
    previous_id = after
    for raw in raw_changes:
        if not isinstance(raw, dict):
            raise ApiError("Server change-journal response contains an invalid change.")
        change_id = raw.get("id")
        if (
            not isinstance(change_id, int)
            or isinstance(change_id, bool)
            or change_id <= previous_id
        ):
            raise ApiError("Server change-journal response is not monotonic.")
        previous_id = change_id
        changes.append(dict(raw))

    if changes and next_cursor != changes[-1]["id"]:
        raise ApiError("Server change-journal cursor does not match the returned changes.")
    if not changes and next_cursor != after:
        raise ApiError("Server change-journal cursor advanced without returning a change.")
    return tuple(changes), next_cursor


def _drain_changes(
    api: Any,
    profile: Mapping[str, Any],
    after: int,
    *,
    collect: bool,
) -> tuple[tuple[dict[str, Any], ...], int]:
    cursor = after
    collected: list[dict[str, Any]] = []
    while True:
        response = api.get_changes(cursor, dict(profile), limit=_CHANGE_PAGE_LIMIT)
        page, next_cursor = _validated_page(response, cursor)
        if collect:
            collected.extend(page)
        if not page:
            return tuple(collected), cursor
        if next_cursor <= cursor:
            raise ApiError("Server change-journal pagination did not advance.")
        cursor = next_cursor
        if len(page) < _CHANGE_PAGE_LIMIT:
            return tuple(collected), cursor


def _baseline_window(
    api: Any,
    profile: Mapping[str, Any],
    *,
    reason: str,
) -> ChangeWindow:
    _changes, next_cursor = _drain_changes(api, profile, 0, collect=False)
    return ChangeWindow(
        next_cursor=next_cursor,
        changes=(),
        full_refresh=True,
        fallback_reason=reason,
    )


def read_change_window(
    api: Any,
    profile: Mapping[str, Any],
    persisted_cursor: Any,
) -> ChangeWindow:
    """Read deltas, with a full-refresh fallback for missing/invalid/stale cursors."""

    cursor = normalize_change_cursor(persisted_cursor)
    if cursor is None:
        return _baseline_window(api, profile, reason="missing-or-invalid-cursor")

    if cursor > 0:
        try:
            probe_response = api.get_changes(cursor - 1, dict(profile), limit=1)
            probe, _probe_cursor = _validated_page(probe_response, cursor - 1)
        except ApiError as error:
            if error.status in _STALE_CURSOR_STATUSES:
                return _baseline_window(api, profile, reason="stale-cursor")
            raise
        if not probe or probe[0].get("id") != cursor:
            return _baseline_window(api, profile, reason="stale-cursor")

    try:
        changes, next_cursor = _drain_changes(api, profile, cursor, collect=True)
    except ApiError as error:
        if error.status in _STALE_CURSOR_STATUSES:
            return _baseline_window(api, profile, reason="stale-cursor")
        raise
    return ChangeWindow(next_cursor=next_cursor, changes=changes)


def _binding_documents(
    binding: Mapping[str, Any],
    member_documents: Iterable[str] = (),
) -> frozenset[str]:
    documents = {
        str(binding.get("server_document") or "").strip(),
        *(
            str(document or "").strip()
            for document in member_documents
        ),
    }
    documents.discard("")
    return frozenset(documents)


def change_components_for_binding(
    change: Mapping[str, Any],
    binding: Mapping[str, Any],
    *,
    member_documents: Iterable[str] = (),
    effective_title: str = "",
    vocabulary_title: str = "",
) -> frozenset[str]:
    """Map one journal row to the affected component(s) of one binding."""

    entity_type = str(change.get("entity_type") or "").strip()
    entity_key = str(change.get("entity_key") or "").strip()
    operation = str(change.get("operation") or "").strip()
    payload = change.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    documents = _binding_documents(binding, member_documents)

    if entity_type == "progress" and entity_key in documents:
        return frozenset({COMPONENT_PROGRESS})

    if entity_type == "document" and entity_key in documents:
        if operation == "delete":
            return ALL_CHANGE_COMPONENTS
        return frozenset({COMPONENT_METADATA})

    if entity_type == "annotation":
        document = str(payload.get("document") or "").strip()
        if document in documents:
            return frozenset({COMPONENT_ANNOTATIONS})

    if entity_type == "vocabulary":
        left = str(effective_title or "").strip().casefold()
        right = str(vocabulary_title or "").strip().casefold()
        if left and right and left == right:
            return frozenset({COMPONENT_VOCABULARY})

    if entity_type == "logical_book":
        logical_id = binding.get("logical_book_id")
        if (
            isinstance(logical_id, int)
            and not isinstance(logical_id, bool)
            and logical_id > 0
            and entity_key == str(logical_id)
        ):
            if operation == "progress":
                return frozenset({COMPONENT_PROGRESS})
            if operation == "presentation":
                return frozenset({COMPONENT_METADATA})
            return ALL_CHANGE_COMPONENTS

    return frozenset()


def refresh_components_for_binding(
    window: ChangeWindow,
    binding: Mapping[str, Any],
    requested_components: Iterable[str],
    *,
    member_documents: Iterable[str] = (),
    effective_title: str = "",
    vocabulary_titles: Mapping[str, str] | None = None,
) -> frozenset[str]:
    """Return only requested components that need a server refresh."""

    requested = {
        component
        for component in requested_components
        if component in ALL_CHANGE_COMPONENTS
    }
    if not requested:
        return frozenset()
    if window.full_refresh:
        return frozenset(requested)

    dirty = requested.difference(initialized_change_components(binding))
    titles = vocabulary_titles if isinstance(vocabulary_titles, Mapping) else {}
    for change in window.changes:
        vocabulary_title = ""
        if str(change.get("entity_type") or "").strip() == "vocabulary":
            vocabulary_title = str(titles.get(str(change.get("entity_key") or "")) or "")
        dirty.update(
            change_components_for_binding(
                change,
                binding,
                member_documents=member_documents,
                effective_title=effective_title,
                vocabulary_title=vocabulary_title,
            ).intersection(requested)
        )
    return frozenset(dirty)
