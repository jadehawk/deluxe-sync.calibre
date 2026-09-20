"""Stage 2.5 legacy Techy-Notes credential migration smoke test."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from calibre.customize.ui import find_plugin


plugin = find_plugin("Deluxe Sync")
if plugin is None:
    raise RuntimeError("Deluxe Sync is not installed")
plugin.load_actual_plugin(None)

from calibre_plugins.deluxe_sync.api import (  # noqa: E402
    DeluxeSyncApi,
    _legacy_portal_userkey,
    kosync_userkey,
)


USERNAME = "legacy-user"
LOGIN_PHRASE = "Test" + "1234"
STANDARD_KEY = kosync_userkey(LOGIN_PHRASE)
LEGACY_KEY = _legacy_portal_userkey(LOGIN_PHRASE)
EXPECTED_STANDARD = "2c9341ca" + "4cf3d87b" + "9e4eb905" + "d6a3ec45"
EXPECTED_LEGACY = "4117ea7c" + "2a734db0" + "f5d74c5c" + "2ff48bc5"

assert STANDARD_KEY == EXPECTED_STANDARD
assert LEGACY_KEY == EXPECTED_LEGACY


class Handler(BaseHTTPRequestHandler):
    migrated = False
    standard_attempts = 0
    migration_requests = 0

    def log_message(self, format, *args):
        return

    def _send_json(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/v1/capabilities":
            self._send_json(
                200,
                {
                    "server_family": "techy-notes",
                    "server_type": "enhanced",
                    "server_version": "0.2.0.3",
                    "capabilities": {
                        "progress_sync": True,
                        "logical_library": True,
                    },
                },
            )
            return

        if self.path == "/users/auth":
            if self.headers.get("X-Auth-User") != USERNAME:
                self._send_json(401, {"message": "invalid credentials"})
                return
            key = self.headers.get("X-Auth-Key", "")
            if key == STANDARD_KEY:
                type(self).standard_attempts += 1
                if self.migrated:
                    self._send_json(200, {"authorized": "OK"})
                else:
                    self._send_json(401, {"message": "invalid credentials"})
                return
            self._send_json(401, {"message": "invalid credentials"})
            return

        self._send_json(404, {"message": "not found"})

    def do_POST(self):
        if self.path != "/users/credential/migrate":
            self._send_json(404, {"message": "not found"})
            return

        type(self).migration_requests += 1
        if (
            self.headers.get("X-Auth-User") != USERNAME
            or self.headers.get("X-Auth-Key") != LEGACY_KEY
        ):
            self._send_json(401, {"message": "invalid credentials"})
            return

        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if payload.get("new_" + "userkey") != STANDARD_KEY:
            self._send_json(422, {"message": "invalid migration value"})
            return

        type(self).migrated = True
        self._send_json(200, {"migrated": True})


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
thread = Thread(target=server.serve_forever, daemon=True)
thread.start()
base_url = f"http://127.0.0.1:{server.server_port}"

try:
    api = DeluxeSyncApi(base_url, plugin_version="0.1.0.0", timeout=2)
    profile = api.authenticate_kosync_profile(USERNAME, LOGIN_PHRASE)

    assert Handler.migrated is True
    assert Handler.migration_requests == 1
    assert Handler.standard_attempts == 2
    assert profile["auth_mode"] == "kosync"
    assert profile["username"] == USERNAME
    assert profile["user" + "key"] == STANDARD_KEY
    assert LOGIN_PHRASE not in repr(profile)

    reused = api.authenticate_kosync_profile(USERNAME, "", profile)
    assert reused["user" + "key"] == STANDARD_KEY
    assert Handler.migration_requests == 1
    assert Handler.standard_attempts == 3

finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)

print("Deluxe Sync Stage 2.5 legacy Techy-Notes auth smoke test passed")
