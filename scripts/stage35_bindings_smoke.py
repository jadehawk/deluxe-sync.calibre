"""Deterministic Stage 3.5 server-backed binding smoke test inside Calibre."""

from __future__ import annotations

from copy import deepcopy

from calibre.customize.ui import find_plugin


plugin = find_plugin("Deluxe Sync")
if plugin is None:
    raise RuntimeError("Deluxe Sync is not installed")
plugin.load_actual_plugin(None)

from calibre_plugins.deluxe_sync import bindings as bindings_module  # noqa: E402
from calibre_plugins.deluxe_sync.api import ApiError, DeluxeSyncApi  # noqa: E402
from calibre_plugins.deluxe_sync.binding_sync import (  # noqa: E402
    reconcile_server_bindings,
    recover_server_bindings,
    server_binding_supported,
)


class FakePrefs(dict):
    def __init__(self):
        super().__init__()
        self.defaults = {}


class FakeBindingApi:
    def __init__(self, remote=None, *, delete_error=False):
        self.remote = deepcopy(remote or [])
        self.delete_error = delete_error
        self.calls = []

    def get_calibre_bindings(self, library_uuid, profile):
        self.calls.append(("get", library_uuid))
        return {
            "protocol_version": 1,
            "library_uuid": library_uuid,
            "bindings": deepcopy(
                [
                    item
                    for item in self.remote
                    if item.get("calibre_library_uuid") == library_uuid
                ]
            ),
        }

    def put_calibre_binding(
        self,
        library_uuid,
        book_uuid,
        server_document,
        profile,
    ):
        self.calls.append(("put", library_uuid, book_uuid, server_document))
        existing = next(
            (
                item
                for item in self.remote
                if item.get("calibre_library_uuid") == library_uuid
                and item.get("calibre_book_uuid") == book_uuid
            ),
            None,
        )
        if existing is None:
            existing = {
                "calibre_library_uuid": library_uuid,
                "calibre_book_uuid": book_uuid,
            }
            self.remote.append(existing)
        existing["server_document"] = server_document
        existing.setdefault("logical_book_id", None)
        return {"protocol_version": 1, "binding": deepcopy(existing)}

    def delete_calibre_binding(self, library_uuid, book_uuid, profile):
        self.calls.append(("delete", library_uuid, book_uuid))
        if self.delete_error:
            raise ApiError("offline")
        before = len(self.remote)
        self.remote = [
            item
            for item in self.remote
            if not (
                item.get("calibre_library_uuid") == library_uuid
                and item.get("calibre_book_uuid") == book_uuid
            )
        ]
        return {"deleted": len(self.remote) != before}


PAIRING_PROFILE = {
    "id": "stage35-profile",
    "auth_mode": "pairing",
    "server_url": "https://example.invalid",
}
ENHANCED_CAPS = {
    "server_type": "enhanced",
    "capabilities": {"calibre_bindings": True},
}
STANDARD_CAPS = {
    "server_type": "standard",
    "capabilities": {"calibre_bindings": False},
}


def local_binding(library_uuid, book_uuid, document, *, logical_book_id=None):
    return {
        "server_profile_id": "stage35-profile",
        "calibre_library_uuid": library_uuid,
        "calibre_book_uuid": book_uuid,
        "calibre_book_id": 7,
        "server_document": document,
        "logical_book_id": logical_book_id,
        "enrolled": True,
        "auto_sync": True,
        "last_successful_sync": None,
        "last_change_cursor": None,
        "last_calibre_metadata_fingerprint": None,
    }


if not server_binding_supported(PAIRING_PROFILE, ENHANCED_CAPS):
    raise RuntimeError("Enhanced pairing profile did not enable server binding backup")
if server_binding_supported(PAIRING_PROFILE, STANDARD_CAPS):
    raise RuntimeError("Standard server incorrectly enabled server binding backup")
if server_binding_supported({"auth_mode": "kosync"}, ENHANCED_CAPS):
    raise RuntimeError("Plain KOSync auth incorrectly enabled server binding backup")

# Local binding is mirrored to an empty server backup.
api = FakeBindingApi()
result = reconcile_server_bindings(
    api,
    PAIRING_PROFILE,
    "stage35-profile",
    "library-a",
    {"book-a": local_binding("library-a", "book-a", "doc-a")},
    set(),
    ENHANCED_CAPS,
)
if result.mirrored_count != 1 or ("put", "library-a", "book-a", "doc-a") not in api.calls:
    raise RuntimeError("Local binding was not mirrored to the server")
if result.bindings["book-a"]["auto_sync"] is not True:
    raise RuntimeError("Server mirroring overwrote local-only auto-sync state")

# Remote-only binding is recovered locally.
api = FakeBindingApi(
    [
        {
            "calibre_library_uuid": "library-a",
            "calibre_book_uuid": "book-remote",
            "server_document": "doc-remote",
            "logical_book_id": 42,
        }
    ]
)
result = reconcile_server_bindings(
    api,
    PAIRING_PROFILE,
    "stage35-profile",
    "library-a",
    {},
    set(),
    ENHANCED_CAPS,
)
if result.recovered_count != 1:
    raise RuntimeError("Remote binding was not recovered")
recovered = result.bindings.get("book-remote")
if not recovered or recovered["server_document"] != "doc-remote":
    raise RuntimeError("Recovered binding identity is wrong")
if recovered["logical_book_id"] != 42 or recovered["auto_sync"] is not False:
    raise RuntimeError("Recovered binding did not preserve server/local authority split")

# Local mapping wins a disagreement and refreshes the server copy.
api = FakeBindingApi(
    [
        {
            "calibre_library_uuid": "library-a",
            "calibre_book_uuid": "book-a",
            "server_document": "stale-doc",
            "logical_book_id": 99,
        }
    ]
)
result = reconcile_server_bindings(
    api,
    PAIRING_PROFILE,
    "stage35-profile",
    "library-a",
    {"book-a": local_binding("library-a", "book-a", "current-doc")},
    set(),
    ENHANCED_CAPS,
)
if ("put", "library-a", "book-a", "current-doc") not in api.calls:
    raise RuntimeError("Local binding did not win a server conflict")
if result.bindings["book-a"]["server_document"] != "current-doc":
    raise RuntimeError("Local binding identity changed during conflict reconciliation")

# Separate libraries remain independent even when they point to the same raw document.
api = FakeBindingApi(
    [
        {
            "calibre_library_uuid": "library-b",
            "calibre_book_uuid": "book-b",
            "server_document": "shared-doc",
            "logical_book_id": 5,
        }
    ]
)
result_a = reconcile_server_bindings(
    api,
    PAIRING_PROFILE,
    "stage35-profile",
    "library-a",
    {"book-a": local_binding("library-a", "book-a", "shared-doc")},
    set(),
    ENHANCED_CAPS,
)
result_b = reconcile_server_bindings(
    api,
    PAIRING_PROFILE,
    "stage35-profile",
    "library-b",
    {},
    set(),
    ENHANCED_CAPS,
)
if set(result_a.bindings) != {"book-a"} or set(result_b.bindings) != {"book-b"}:
    raise RuntimeError("Separate Calibre library binding scopes leaked into each other")

# Successful pending delete clears its tombstone and cannot be restored.
api = FakeBindingApi(
    [
        {
            "calibre_library_uuid": "library-a",
            "calibre_book_uuid": "book-delete",
            "server_document": "doc-delete",
            "logical_book_id": None,
        }
    ]
)
result = reconcile_server_bindings(
    api,
    PAIRING_PROFILE,
    "stage35-profile",
    "library-a",
    {},
    {"book-delete"},
    ENHANCED_CAPS,
)
if "book-delete" not in result.cleared_tombstones:
    raise RuntimeError("Successful server binding deletion did not clear tombstone")
if "book-delete" in result.bindings:
    raise RuntimeError("Deleted server binding was restored locally")

# Failed delete keeps tombstone and still suppresses the stale remote copy.
api = FakeBindingApi(
    [
        {
            "calibre_library_uuid": "library-a",
            "calibre_book_uuid": "book-delete",
            "server_document": "doc-delete",
            "logical_book_id": None,
        }
    ],
    delete_error=True,
)
result = reconcile_server_bindings(
    api,
    PAIRING_PROFILE,
    "stage35-profile",
    "library-a",
    {},
    {"book-delete"},
    ENHANCED_CAPS,
)
if result.cleared_tombstones:
    raise RuntimeError("Failed server delete incorrectly cleared tombstone")
if "book-delete" in result.bindings:
    raise RuntimeError("Failed-delete tombstone allowed stale binding recovery")
if not result.warnings:
    raise RuntimeError("Failed server binding deletion did not surface a warning")

# Standard KOSync remains fully local and does not touch binding endpoints.
api = FakeBindingApi()
local = {"book-a": local_binding("library-a", "book-a", "doc-a")}
result = reconcile_server_bindings(
    api,
    {"auth_mode": "kosync"},
    "stage35-profile",
    "library-a",
    local,
    set(),
    STANDARD_CAPS,
)
if api.calls:
    raise RuntimeError("Standard KOSync attempted enhanced binding API calls")
if result.bindings != local or result.remote_enabled:
    raise RuntimeError("Standard KOSync changed local-only binding behavior")

# Local tombstone persistence survives reload and bind clears it.
original_prefs = bindings_module._PREFS
bindings_module._PREFS = FakePrefs()
bindings_module._PREFS.defaults["book_bindings"] = {}
bindings_module._PREFS.defaults["binding_tombstones"] = {}
try:
    bindings_module.save_binding_record(
        local_binding("library-a", "book-a", "doc-a"),
        "stage35-profile",
    )
    bindings_module.forget_binding(
        "library-a",
        "book-a",
        "stage35-profile",
        tombstone=True,
    )
    if "book-a" not in bindings_module.get_binding_tombstones(
        "library-a", "stage35-profile"
    ):
        raise RuntimeError("Binding tombstone did not persist")
    if bindings_module.get_binding("library-a", "book-a", "stage35-profile") is not None:
        raise RuntimeError("Forgotten binding remained locally")
    bindings_module.clear_binding_tombstone(
        "library-a", "book-a", "stage35-profile"
    )
    if bindings_module.get_binding_tombstones("library-a", "stage35-profile"):
        raise RuntimeError("Binding tombstone did not clear")
finally:
    bindings_module._PREFS = original_prefs

# Explicit recovery restores only missing local mappings, preserves conflicts, and
# suppresses records that the user already cleared locally.
api = FakeBindingApi(
    [
        {
            "calibre_library_uuid": "library-a",
            "calibre_book_uuid": "book-existing",
            "server_document": "doc-existing",
            "logical_book_id": 12,
        },
        {
            "calibre_library_uuid": "library-a",
            "calibre_book_uuid": "book-restore",
            "server_document": "doc-restore",
            "logical_book_id": 13,
        },
        {
            "calibre_library_uuid": "library-a",
            "calibre_book_uuid": "book-conflict",
            "server_document": "remote-doc",
            "logical_book_id": 14,
        },
        {
            "calibre_library_uuid": "library-a",
            "calibre_book_uuid": "book-cleared",
            "server_document": "doc-cleared",
            "logical_book_id": None,
        },
    ]
)
recovery = recover_server_bindings(
    api,
    PAIRING_PROFILE,
    "stage35-profile",
    "library-a",
    {
        "book-existing": local_binding(
            "library-a", "book-existing", "doc-existing", logical_book_id=1
        ),
        "book-conflict": local_binding(
            "library-a", "book-conflict", "local-doc", logical_book_id=2
        ),
    },
    {"book-cleared"},
    ENHANCED_CAPS,
)
if (
    recovery.found_count != 4
    or recovery.restored_count != 1
    or recovery.already_present_count != 1
    or recovery.conflict_count != 1
    or recovery.suppressed_count != 1
):
    raise RuntimeError(f"Unexpected explicit binding recovery counts: {recovery}")
if recovery.bindings["book-restore"]["server_document"] != "doc-restore":
    raise RuntimeError("Explicit recovery did not restore the missing binding")
if recovery.bindings["book-existing"]["logical_book_id"] != 12:
    raise RuntimeError("Explicit recovery did not refresh current logical membership")
if recovery.bindings["book-conflict"]["server_document"] != "local-doc":
    raise RuntimeError("Explicit recovery overwrote a conflicting local mapping")
if "book-cleared" in recovery.bindings:
    raise RuntimeError("Explicit recovery restored a locally cleared binding")
if any(call[0] in {"put", "delete"} for call in api.calls):
    raise RuntimeError("Explicit recovery unexpectedly modified the server")

# API helpers produce the expected method/path/payload shapes.
api = DeluxeSyncApi("https://sync.example", plugin_version="0.1.0.0")
calls = []


def request_json(path, *, method="GET", payload=None, profile=None, **_kwargs):
    calls.append((path, method, deepcopy(payload), profile))
    if method == "GET":
        return {"bindings": []}
    if method == "PUT":
        return {
            "binding": {
                "calibre_library_uuid": "library/a",
                "calibre_book_uuid": "book / one",
                "server_document": "doc-a",
            }
        }
    return {"deleted": True}


api._request_json = request_json
api.get_calibre_bindings("library/a", PAIRING_PROFILE)
api.put_calibre_binding("library/a", "book / one", "doc-a", PAIRING_PROFILE)
api.delete_calibre_binding("library/a", "book / one", PAIRING_PROFILE)

if calls[0][0] != "/api/v1/calibre-bindings?library_uuid=library%2Fa":
    raise RuntimeError(f"Unexpected binding-list path: {calls[0][0]}")
if calls[1][0] != "/api/v1/calibre-bindings/book%20%2F%20one" or calls[1][1] != "PUT":
    raise RuntimeError(f"Unexpected binding-save request: {calls[1]}")
if calls[1][2] != {"library_uuid": "library/a", "server_document": "doc-a"}:
    raise RuntimeError("Binding-save payload is wrong")
if (
    calls[2][0]
    != "/api/v1/calibre-bindings/book%20%2F%20one?library_uuid=library%2Fa"
    or calls[2][1] != "DELETE"
):
    raise RuntimeError(f"Unexpected binding-delete request: {calls[2]}")

print("Deluxe Sync Stage 3.5 binding persistence smoke test passed")
