"""Server-backed recovery/mirroring for durable Calibre book bindings."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from calibre_plugins.deluxe_sync.api import ApiError, DeluxeSyncApi


@dataclass
class BindingReconcileResult:
    bindings: dict[str, dict[str, Any]]
    cleared_tombstones: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)
    remote_enabled: bool = False
    mirrored_count: int = 0
    recovered_count: int = 0


@dataclass
class BindingRecoveryResult:
    bindings: dict[str, dict[str, Any]]
    warnings: list[str] = field(default_factory=list)
    remote_enabled: bool = False
    found_count: int = 0
    restored_count: int = 0
    already_present_count: int = 0
    conflict_count: int = 0
    suppressed_count: int = 0


def server_binding_supported(
    profile: dict[str, Any],
    capabilities_response: dict[str, Any],
) -> bool:
    if str(profile.get("auth_mode") or "") != "pairing":
        return False
    capabilities = capabilities_response.get("capabilities")
    return isinstance(capabilities, dict) and capabilities.get("calibre_bindings") is True


def _remote_binding(
    item: object,
    profile_id: str,
    library_uuid: str,
) -> tuple[str, dict[str, Any]] | None:
    if not isinstance(item, dict):
        return None
    book_uuid = str(item.get("calibre_book_uuid") or "").strip()
    document = str(item.get("server_document") or "").strip()
    item_library = str(item.get("calibre_library_uuid") or library_uuid).strip()
    if not book_uuid or not document or item_library != library_uuid:
        return None

    logical_id = item.get("logical_book_id")
    if not isinstance(logical_id, int) or isinstance(logical_id, bool) or logical_id <= 0:
        logical_id = None

    return book_uuid, {
        "server_profile_id": profile_id,
        "calibre_library_uuid": library_uuid,
        "calibre_book_uuid": book_uuid,
        "calibre_book_id": None,
        "server_document": document,
        "logical_book_id": logical_id,
        "enrolled": True,
        "auto_sync": False,
        "last_successful_sync": None,
        "last_change_cursor": None,
        "change_cursor_components": [],
        "last_calibre_metadata_fingerprint": None,
    }


def recover_server_bindings(
    api: DeluxeSyncApi,
    profile: dict[str, Any],
    profile_id: str,
    library_uuid: str,
    local_bindings: dict[str, dict[str, Any]],
    tombstones: set[str],
    capabilities_response: dict[str, Any],
) -> BindingRecoveryResult:
    """Restore missing local mappings from the server without modifying the server."""

    effective = {
        str(book_uuid): deepcopy(binding)
        for book_uuid, binding in local_bindings.items()
        if isinstance(book_uuid, str) and isinstance(binding, dict)
    }
    result = BindingRecoveryResult(bindings=effective)
    if not server_binding_supported(profile, capabilities_response):
        return result
    result.remote_enabled = True

    try:
        response = api.get_calibre_bindings(library_uuid, profile)
    except ApiError as error:
        result.warnings.append(f"Could not load server-backed Calibre bindings: {error}")
        return result

    remote: dict[str, dict[str, Any]] = {}
    for item in response.get("bindings") or []:
        normalized = _remote_binding(item, profile_id, library_uuid)
        if normalized is not None:
            remote[normalized[0]] = normalized[1]

    result.found_count = len(remote)

    for book_uuid in tombstones:
        if book_uuid in remote:
            result.suppressed_count += 1
        remote.pop(book_uuid, None)

    for book_uuid, remote_binding in remote.items():
        local = result.bindings.get(book_uuid)
        if local is None:
            result.bindings[book_uuid] = deepcopy(remote_binding)
            result.restored_count += 1
            continue

        local_document = str(local.get("server_document") or "").strip()
        remote_document = str(remote_binding.get("server_document") or "").strip()
        if local_document != remote_document:
            result.conflict_count += 1
            continue

        local["logical_book_id"] = remote_binding.get("logical_book_id")
        result.already_present_count += 1

    return result


def reconcile_server_bindings(
    api: DeluxeSyncApi,
    profile: dict[str, Any],
    profile_id: str,
    library_uuid: str,
    local_bindings: dict[str, dict[str, Any]],
    tombstones: set[str],
    capabilities_response: dict[str, Any],
) -> BindingReconcileResult:
    """Mirror/recover bindings without changing Calibre metadata or progress."""

    effective = {
        str(book_uuid): deepcopy(binding)
        for book_uuid, binding in local_bindings.items()
        if isinstance(book_uuid, str) and isinstance(binding, dict)
    }
    result = BindingReconcileResult(bindings=effective)
    if not server_binding_supported(profile, capabilities_response):
        return result
    result.remote_enabled = True

    for book_uuid in sorted(tombstones):
        try:
            api.delete_calibre_binding(library_uuid, book_uuid, profile)
            result.cleared_tombstones.add(book_uuid)
        except ApiError as error:
            result.warnings.append(
                f"Could not remove the server backup for Calibre book {book_uuid}: {error}"
            )

    try:
        response = api.get_calibre_bindings(library_uuid, profile)
    except ApiError as error:
        result.warnings.append(f"Could not load server-backed Calibre bindings: {error}")
        for book_uuid in tombstones:
            result.bindings.pop(book_uuid, None)
        return result

    remote: dict[str, dict[str, Any]] = {}
    for item in response.get("bindings") or []:
        normalized = _remote_binding(item, profile_id, library_uuid)
        if normalized is not None:
            remote[normalized[0]] = normalized[1]

    for book_uuid in tombstones:
        result.bindings.pop(book_uuid, None)
        remote.pop(book_uuid, None)

    # A local mapping is authoritative if both sides exist but disagree.
    for book_uuid, local in list(result.bindings.items()):
        document = str(local.get("server_document") or "").strip()
        if not document:
            continue
        remote_binding = remote.get(book_uuid)
        remote_document = (
            str(remote_binding.get("server_document") or "").strip()
            if remote_binding
            else ""
        )
        if remote_document != document:
            try:
                saved = api.put_calibre_binding(
                    library_uuid,
                    book_uuid,
                    document,
                    profile,
                ).get("binding")
                if isinstance(saved, dict):
                    logical_id = saved.get("logical_book_id")
                    local["logical_book_id"] = (
                        logical_id
                        if isinstance(logical_id, int)
                        and not isinstance(logical_id, bool)
                        and logical_id > 0
                        else None
                    )
                result.mirrored_count += 1
            except ApiError as error:
                result.warnings.append(
                    f"Could not back up the binding for Calibre book {book_uuid}: {error}"
                )
        elif remote_binding is not None:
            # Keep local-only enrollment state, but refresh the server-owned
            # current logical-book relationship.
            local["logical_book_id"] = remote_binding.get("logical_book_id")

    for book_uuid, remote_binding in remote.items():
        if book_uuid in result.bindings:
            continue
        result.bindings[book_uuid] = deepcopy(remote_binding)
        result.recovered_count += 1

    return result
