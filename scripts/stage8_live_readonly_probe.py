"""Read-only Stage 8 live vocabulary probe.

This script intentionally performs no Calibre writes. It opens the configured
library, reads existing server bindings, and reports bound books that currently
have vocabulary on the active server profile.
"""

from __future__ import annotations

from calibre.customize.ui import find_plugin
from calibre.db.legacy import LibraryDatabase
from calibre.utils.config import prefs


plugin = find_plugin("Deluxe Sync")
if plugin is None:
    raise RuntimeError("Deluxe Sync is not installed")
plugin.load_actual_plugin(None)

from calibre_plugins.deluxe_sync.api import ApiError, CapabilityError, DeluxeSyncApi  # noqa: E402
from calibre_plugins.deluxe_sync.settings import get_active_server_profile  # noqa: E402
from calibre_plugins.deluxe_sync.vocabulary_sync import render_vocabulary_archive  # noqa: E402


profile = get_active_server_profile()
if not isinstance(profile, dict):
    raise RuntimeError("Deluxe Sync does not have an active server profile.")

server_url = str(profile.get("server_url") or "").strip()
if not server_url:
    raise RuntimeError("The active Deluxe Sync profile does not have a server URL.")

library_path = str(prefs.get("library_path") or "").strip()
if not library_path:
    raise RuntimeError("Calibre does not have a configured library path.")

db = LibraryDatabase(library_path)
try:
    library_uuid = str(db.library_id or "").strip()
    if not library_uuid:
        raise RuntimeError("The configured Calibre library does not have a library UUID.")

    new_api = db.new_api
    local_by_uuid = {}
    for book_id in new_api.all_book_ids():
        book_uuid = str(new_api.field_for("uuid", book_id, default_value="") or "").strip()
        if not book_uuid:
            continue
        title = str(new_api.field_for("title", book_id, default_value="") or "").strip()
        local_by_uuid[book_uuid] = (int(book_id), title)

    api = DeluxeSyncApi(server_url, plugin_version="stage8-live-readonly-probe")
    capabilities = api.discover_capabilities(profile)
    capability_values = capabilities.get("capabilities")
    capability_values = capability_values if isinstance(capability_values, dict) else {}
    if capability_values.get("vocabulary_builder") is not True:
        raise CapabilityError("The active server does not advertise vocabulary_builder.")

    bindings_payload = api.get_calibre_bindings(library_uuid, profile)
    bindings = bindings_payload.get("bindings")
    bindings = bindings if isinstance(bindings, list) else []

    server_library = None
    if capability_values.get("logical_library") is True:
        server_library = api.get_library(profile, capabilities_response=capabilities)

    logical_cache = {}
    checked = 0
    local_matches = 0
    with_vocabulary = []

    for item in bindings:
        if not isinstance(item, dict):
            continue
        book_uuid = str(item.get("calibre_book_uuid") or "").strip()
        document = str(item.get("server_document") or "").strip()
        if not book_uuid or not document:
            continue
        local = local_by_uuid.get(book_uuid)
        if local is None:
            continue

        local_matches += 1
        book_id, calibre_title = local
        checked += 1
        try:
            payload = api.get_vocabulary_for_binding(
                document,
                calibre_title,
                profile,
                capabilities_response=capabilities,
                logical_book_id=item.get("logical_book_id"),
                library_response=server_library,
                logical_cache=logical_cache,
            )
        except ApiError as error:
            print(
                f"WARN book_id={book_id} title={calibre_title!r}: "
                f"{type(error).__name__}: {error}"
            )
            continue

        archive = render_vocabulary_archive(payload)
        if archive.count <= 0:
            continue

        words = []
        entries = payload.get("entries")
        if isinstance(entries, list):
            seen = set()
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                word = " ".join(str(entry.get("word") or "").split())
                key = word.casefold()
                if word and key not in seen:
                    seen.add(key)
                    words.append(word)
        words.sort(key=lambda value: (value.casefold(), value))

        with_vocabulary.append(
            {
                "book_id": book_id,
                "calibre_title": calibre_title,
                "server_title": str(payload.get("title") or "").strip(),
                "count": archive.count,
                "sample": words[:8],
            }
        )

    print(f"Library: {library_path}")
    print(f"Library UUID: {library_uuid}")
    print(f"Server binding records: {len(bindings)}")
    print(f"Bindings present in this local library: {local_matches}")
    print(f"Bound books checked: {checked}")
    print(f"Bound books with vocabulary: {len(with_vocabulary)}")

    for candidate in with_vocabulary[:10]:
        sample = ", ".join(candidate["sample"])
        print(
            "CANDIDATE "
            f"book_id={candidate['book_id']} "
            f"calibre_title={candidate['calibre_title']!r} "
            f"server_title={candidate['server_title']!r} "
            f"words={candidate['count']} "
            f"sample=[{sample}]"
        )

    if not with_vocabulary:
        raise RuntimeError(
            "No already-bound books in the configured Calibre library currently have server vocabulary."
        )

    print(
        "Stage 8 live read-only probe passed: active profile authenticated, "
        "current library bindings resolved, and real vocabulary was isolated to bound books."
    )
finally:
    db.close()
