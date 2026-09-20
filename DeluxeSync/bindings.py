"""Durable per-library Calibre ↔ remote document bindings."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from calibre.utils.config import JSONConfig

from calibre_plugins.deluxe_sync.models import CalibreBook, MatchCandidate
from calibre_plugins.deluxe_sync.settings import (
    DEFAULT_PROFILE_ID,
    get_active_server_profile_id,
)


_PREFS = JSONConfig("plugins/deluxe_sync")
_PREFS.defaults["book_bindings"] = {}
_PREFS.defaults["binding_tombstones"] = {}


def _profile_id(profile_id: str | None = None) -> str:
    clean = str(profile_id or get_active_server_profile_id() or "").strip()
    return clean or DEFAULT_PROFILE_ID


def _pref_dict(key: str) -> dict[str, Any]:
    value = _PREFS.get(key, {})
    return deepcopy(value) if isinstance(value, dict) else {}


def _library_map(
    key: str,
    library_uuid: str,
    profile_id: str | None = None,
) -> dict[str, Any]:
    profiles = _pref_dict(key)
    profile = profiles.get(_profile_id(profile_id), {})
    if not isinstance(profile, dict):
        return {}
    library = profile.get(str(library_uuid or "").strip(), {})
    return deepcopy(library) if isinstance(library, dict) else {}


def get_library_bindings(
    library_uuid: str,
    profile_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Return bindings for one Calibre library on one server profile."""

    return {
        str(book_uuid): deepcopy(binding)
        for book_uuid, binding in _library_map(
            "book_bindings", library_uuid, profile_id
        ).items()
        if isinstance(book_uuid, str) and isinstance(binding, dict)
    }


def get_binding(
    library_uuid: str,
    book_uuid: str,
    profile_id: str | None = None,
) -> dict[str, Any] | None:
    value = get_library_bindings(library_uuid, profile_id).get(
        str(book_uuid or "").strip()
    )
    return deepcopy(value) if isinstance(value, dict) else None


def get_binding_tombstones(
    library_uuid: str,
    profile_id: str | None = None,
) -> set[str]:
    return {
        str(book_uuid)
        for book_uuid, enabled in _library_map(
            "binding_tombstones", library_uuid, profile_id
        ).items()
        if isinstance(book_uuid, str) and bool(enabled)
    }


def _set_library_value(
    key: str,
    library_uuid: str,
    book_uuid: str,
    value: Any,
    profile_id: str | None = None,
) -> None:
    library_key = str(library_uuid or "").strip()
    book_key = str(book_uuid or "").strip()
    if not library_key or not book_key:
        return

    profiles = _pref_dict(key)
    profile_key = _profile_id(profile_id)
    profile = profiles.setdefault(profile_key, {})
    if not isinstance(profile, dict):
        profile = {}
        profiles[profile_key] = profile
    library = profile.setdefault(library_key, {})
    if not isinstance(library, dict):
        library = {}
        profile[library_key] = library
    library[book_key] = deepcopy(value)
    _PREFS[key] = profiles


def _remove_library_value(
    key: str,
    library_uuid: str,
    book_uuid: str,
    profile_id: str | None = None,
) -> None:
    profiles = _pref_dict(key)
    profile_key = _profile_id(profile_id)
    profile = profiles.get(profile_key)
    if not isinstance(profile, dict):
        return

    library_key = str(library_uuid or "").strip()
    library = profile.get(library_key)
    if not isinstance(library, dict):
        return

    library.pop(str(book_uuid or "").strip(), None)
    if not library:
        profile.pop(library_key, None)
    if not profile:
        profiles.pop(profile_key, None)
    _PREFS[key] = profiles


def mark_binding_tombstone(
    library_uuid: str,
    book_uuid: str,
    profile_id: str | None = None,
) -> None:
    """Prevent a pending server-side binding from being restored locally."""

    _set_library_value(
        "binding_tombstones",
        library_uuid,
        book_uuid,
        True,
        profile_id,
    )


def clear_binding_tombstone(
    library_uuid: str,
    book_uuid: str,
    profile_id: str | None = None,
) -> None:
    _remove_library_value(
        "binding_tombstones",
        library_uuid,
        book_uuid,
        profile_id,
    )


def save_binding_record(
    binding: dict[str, Any],
    profile_id: str | None = None,
) -> dict[str, Any]:
    """Persist an already-normalized binding, including reconciliation updates."""

    saved = deepcopy(binding)
    library_uuid = str(saved.get("calibre_library_uuid") or "").strip()
    book_uuid = str(saved.get("calibre_book_uuid") or "").strip()
    document = str(saved.get("server_document") or "").strip()
    if not library_uuid or not book_uuid or not document:
        raise ValueError(
            "Binding requires Calibre library/book UUIDs and a raw server document."
        )

    profile_key = _profile_id(
        profile_id or str(saved.get("server_profile_id") or "")
    )
    saved["server_profile_id"] = profile_key
    saved["calibre_library_uuid"] = library_uuid
    saved["calibre_book_uuid"] = book_uuid
    saved["server_document"] = document
    saved["enrolled"] = bool(saved.get("enrolled", True))
    saved["auto_sync"] = bool(saved.get("auto_sync", False))

    _set_library_value(
        "book_bindings",
        library_uuid,
        book_uuid,
        saved,
        profile_key,
    )
    return deepcopy(saved)


def bind_server_document(
    local: CalibreBook,
    document: str,
    profile_id: str | None = None,
) -> dict[str, Any]:
    """Enroll a Calibre book against an exact raw server document."""

    clean_document = str(document or "").strip()
    if not clean_document:
        raise ValueError("A raw server document is required for binding.")

    existing = get_binding(local.library_uuid, local.book_uuid, profile_id) or {}
    binding = {
        "server_profile_id": _profile_id(profile_id),
        "calibre_library_uuid": local.library_uuid,
        "calibre_book_uuid": local.book_uuid,
        "calibre_book_id": local.book_id,
        "server_document": clean_document,
        "logical_book_id": None,
        "enrolled": True,
        "auto_sync": bool(existing.get("auto_sync", False)),
        "last_successful_sync": existing.get("last_successful_sync"),
        "last_change_cursor": existing.get("last_change_cursor"),
        "change_cursor_components": list(existing.get("change_cursor_components") or []),
        "last_calibre_metadata_fingerprint": existing.get(
            "last_calibre_metadata_fingerprint"
        ),
    }
    saved = save_binding_record(binding, profile_id)
    clear_binding_tombstone(local.library_uuid, local.book_uuid, profile_id)
    return saved


def bind_candidate(
    local: CalibreBook,
    candidate: MatchCandidate,
    profile_id: str | None = None,
) -> dict[str, Any]:
    """Approve a candidate locally; server mirroring is handled separately."""

    remote = candidate.remote
    if not remote.document:
        raise ValueError(
            "This candidate does not expose a raw server document and cannot be bound."
        )

    existing = get_binding(local.library_uuid, local.book_uuid, profile_id) or {}
    binding = {
        "server_profile_id": _profile_id(profile_id),
        "calibre_library_uuid": local.library_uuid,
        "calibre_book_uuid": local.book_uuid,
        "calibre_book_id": local.book_id,
        "server_document": remote.document,
        "logical_book_id": remote.logical_book_id,
        "enrolled": True,
        "auto_sync": bool(existing.get("auto_sync", False)),
        "last_successful_sync": existing.get("last_successful_sync"),
        "last_change_cursor": existing.get("last_change_cursor"),
        "change_cursor_components": list(existing.get("change_cursor_components") or []),
        "last_calibre_metadata_fingerprint": existing.get(
            "last_calibre_metadata_fingerprint"
        ),
    }
    saved = save_binding_record(binding, profile_id)
    clear_binding_tombstone(local.library_uuid, local.book_uuid, profile_id)
    return saved


def forget_binding(
    library_uuid: str,
    book_uuid: str,
    profile_id: str | None = None,
    *,
    tombstone: bool = False,
) -> None:
    _remove_library_value(
        "book_bindings",
        library_uuid,
        book_uuid,
        profile_id,
    )
    if tombstone:
        mark_binding_tombstone(library_uuid, book_uuid, profile_id)
