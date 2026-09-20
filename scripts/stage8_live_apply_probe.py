"""Live Stage 8 write acceptance for one explicitly selected Calibre book.

Usage:
    calibre-debug -e scripts/stage8_live_apply_probe.py <book_id>

The script writes only the mapped #ds_vocabulary field for the selected book,
then immediately reruns the same write to prove idempotence.
"""

from __future__ import annotations

import sys

from calibre.customize.ui import find_plugin
from calibre.db.legacy import LibraryDatabase
from calibre.utils.config import prefs


if len(sys.argv) < 2:
    raise RuntimeError("Pass one Calibre book id to the live Stage 8 apply probe.")

try:
    target_book_id = int(sys.argv[1])
except (TypeError, ValueError):
    raise RuntimeError("The Calibre book id must be an integer.") from None

plugin = find_plugin("Deluxe Sync")
if plugin is None:
    raise RuntimeError("Deluxe Sync is not installed")
plugin.load_actual_plugin(None)

from calibre_plugins.deluxe_sync.api import DeluxeSyncApi  # noqa: E402
from calibre_plugins.deluxe_sync.settings import (  # noqa: E402
    get_active_server_profile,
    get_column_mappings,
)
from calibre_plugins.deluxe_sync.vocabulary_sync import (  # noqa: E402
    apply_vocabulary_archive,
    render_vocabulary_archive,
)


profile = get_active_server_profile()
if not isinstance(profile, dict):
    raise RuntimeError("Deluxe Sync does not have an active server profile.")

server_url = str(profile.get("server_url") or "").strip()
if not server_url:
    raise RuntimeError("The active Deluxe Sync profile does not have a server URL.")

mappings = get_column_mappings()
vocabulary_lookup = str(mappings.get("vocabulary") or "").strip()
if vocabulary_lookup != "#ds_vocabulary":
    raise RuntimeError(
        f"Vocabulary mapping is not #ds_vocabulary (current={vocabulary_lookup!r})."
    )

library_path = str(prefs.get("library_path") or "").strip()
db = LibraryDatabase(library_path)
try:
    new_api = db.new_api
    all_ids = set(int(value) for value in new_api.all_book_ids())
    if target_book_id not in all_ids:
        raise RuntimeError(f"Calibre book id {target_book_id} does not exist in this library.")

    custom = db.custom_field_metadata()
    meta = custom.get(vocabulary_lookup)
    if not isinstance(meta, dict) or meta.get("datatype") != "comments":
        raise RuntimeError("The mapped vocabulary column is missing or is not a comments field.")

    library_uuid = str(db.library_id or "").strip()
    book_uuid = str(
        new_api.field_for("uuid", target_book_id, default_value="") or ""
    ).strip()
    title = str(
        new_api.field_for("title", target_book_id, default_value="") or ""
    ).strip()
    if not book_uuid or not title:
        raise RuntimeError("The selected Calibre book is missing its UUID or title.")

    api = DeluxeSyncApi(server_url, plugin_version="stage8-live-apply-probe")
    capabilities = api.discover_capabilities(profile)
    capability_values = capabilities.get("capabilities")
    capability_values = capability_values if isinstance(capability_values, dict) else {}
    if capability_values.get("vocabulary_builder") is not True:
        raise RuntimeError("The active server does not advertise vocabulary_builder.")

    bindings_payload = api.get_calibre_bindings(library_uuid, profile)
    binding = None
    for item in bindings_payload.get("bindings") or []:
        if (
            isinstance(item, dict)
            and str(item.get("calibre_book_uuid") or "").strip() == book_uuid
        ):
            binding = item
            break
    if binding is None:
        raise RuntimeError("The selected Calibre book does not have a server binding.")

    document = str(binding.get("server_document") or "").strip()
    if not document:
        raise RuntimeError("The selected binding does not have a server document.")

    server_library = None
    if capability_values.get("logical_library") is True:
        server_library = api.get_library(profile, capabilities_response=capabilities)

    payload = api.get_vocabulary_for_binding(
        document,
        title,
        profile,
        capabilities_response=capabilities,
        logical_book_id=binding.get("logical_book_id"),
        library_response=server_library,
        logical_cache={},
    )
    archive = render_vocabulary_archive(payload)
    if archive.count <= 0:
        raise RuntimeError("The selected bound book does not currently have server vocabulary.")

    before = new_api.field_for(vocabulary_lookup, target_book_id, default_value=None)
    first = apply_vocabulary_archive(
        db,
        target_book_id,
        vocabulary_lookup,
        archive,
    )
    stored = new_api.field_for(vocabulary_lookup, target_book_id, default_value=None)
    if str(stored or "") != archive.html:
        raise RuntimeError("The vocabulary archive was not stored exactly as rendered.")

    second = apply_vocabulary_archive(
        db,
        target_book_id,
        vocabulary_lookup,
        archive,
    )
    if second.changed:
        raise RuntimeError("The second identical vocabulary sync was not idempotent.")

    sample = []
    seen = set()
    for item in payload.get("entries") or []:
        if not isinstance(item, dict):
            continue
        word = " ".join(str(item.get("word") or "").split())
        identity = word.casefold()
        if word and identity not in seen:
            seen.add(identity)
            sample.append(word)
    sample.sort(key=lambda value: (value.casefold(), value))

    print(f"Book id: {target_book_id}")
    print(f"Calibre title: {title}")
    print(f"Resolved server title: {str(payload.get('title') or '').strip()}")
    print(f"Vocabulary words: {archive.count}")
    print(f"First sync changed row: {first.changed}")
    print(f"Second identical sync changed row: {second.changed}")
    print(f"Previous column was empty: {not bool(str(before or '').strip())}")
    print(f"Sample: {', '.join(sample[:10])}")
    print(
        "Stage 8 live write probe passed: one selected bound Calibre row received "
        "the real server vocabulary archive and the immediate rerun was idempotent."
    )
finally:
    db.close()
