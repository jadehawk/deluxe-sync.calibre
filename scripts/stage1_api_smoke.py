"""Deterministic Stage 1 API smoke test inside Calibre's Python runtime."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from calibre.customize.ui import find_plugin


plugin = find_plugin("Deluxe Sync")
if plugin is None:
    raise RuntimeError("Deluxe Sync is not installed")
plugin.load_actual_plugin(None)

from calibre_plugins.deluxe_sync.api import (  # noqa: E402
    ApiError,
    AuthorizationError,
    DeluxeSyncApi,
)
from calibre_plugins.deluxe_sync.logger import REDACTED, log_path  # noqa: E402


LOG_FILE = Path(log_path())
LOG_START = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0


class Handler(BaseHTTPRequestHandler):
    redeemed = False
    revoked = False
    last_payload = None
    session_value = "stage1-" + "session"

    def log_message(self, format, *args):
        return

    def _send_json(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, status, message):
        data = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/v1/capabilities":
            self._send_json(
                200,
                {
                    "api_version": "v1",
                    "server_version": "test",
                    "server_family": "techy-notes",
                    "server_type": "enhanced",
                    "capabilities": {
                        "calibre_client_pairing": True,
                        "calibre_client_pairing_version": 1,
                        "document_metadata_write": True,
                        "document_metadata_write_version": 1,
                        "logical_library": True,
                        "change_journal": True,
                        "annotations": True,
                        "vocabulary_builder": True,
                    },
                },
            )
            return

        if self.path == "/api/v1/connected-clients/me":
            expected = "Bearer " + self.session_value
            if self.revoked or self.headers.get("Authorization") != expected:
                self._send_text(401, "Connected client authorization is invalid or revoked")
                return
            self._send_json(
                200,
                {
                    "protocol_version": 1,
                    "server_family": "techy-notes",
                    "server_type": "enhanced",
                    "server_version": "test",
                    "client": {
                        "id": 7,
                        "client_type": "calibre_deluxe_sync",
                        "scope": "calibre_sync_v1",
                        "client_uuid": "client-uuid",
                        "library_uuid": None,
                        "device_name": "Test Desktop",
                        "library_name": None,
                    },
                },
            )
            return

        self._send_text(404, "Not found")

    def do_POST(self):
        if self.path != "/api/v1/client-pairing/redeem":
            self._send_text(404, "Not found")
            return

        size = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(size).decode("utf-8"))
        type(self).last_payload = payload

        if payload.get("code") != "ABCD-EFGH" or type(self).redeemed:
            self._send_text(422, "Registration code is invalid or expired")
            return

        type(self).redeemed = True
        response = {
            "protocol_version": 1,
            "server_family": "techy-notes",
            "server_type": "enhanced",
            "server_version": "test",
            "client_type": "calibre_deluxe_sync",
            "scope": "calibre_sync_v1",
            "client_id": 7,
        }
        response["access_" + "token"] = self.session_value
        self._send_json(200, response)


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
thread = Thread(target=server.serve_forever, daemon=True)
thread.start()

try:
    api = DeluxeSyncApi(
        f"http://127.0.0.1:{server.server_port}",
        plugin_version="0.1.0.0",
        timeout=2,
    )

    capabilities = api.discover_capabilities()
    assert capabilities["server_family"] == "techy-notes"
    assert capabilities["capabilities"]["calibre_client_pairing_version"] == 1

    pairing = api.redeem_registration_code(
        "abcd-efgh",
        client_uuid="client-uuid",
        device_name="Test Desktop",
        calibre_version="9.14.0",
    )
    assert pairing.client_id == 7
    assert pairing.scope == "calibre_sync_v1"
    assert Handler.last_payload == {
        "code": "ABCD-EFGH",
        "client_uuid": "client-uuid",
        "device_name": "Test Desktop",
        "plugin_version": "0.1.0.0",
        "calibre_version": "9.14.0",
    }

    profile = api.pairing_profile(pairing)
    identity = api.get_connected_client(profile)
    assert identity["client"]["library_uuid"] is None

    try:
        api.redeem_registration_code(
            "ABCD-EFGH",
            client_uuid="client-uuid",
            device_name="Test Desktop",
            calibre_version="9.14.0",
        )
    except ApiError as error:
        assert error.status == 422
        assert "invalid or expired" in str(error).lower()
        assert "ABCD-EFGH" not in str(error)
    else:
        raise RuntimeError("Reused registration code was unexpectedly accepted")

    Handler.revoked = True
    try:
        api.get_connected_client(profile)
    except AuthorizationError as error:
        assert error.status == 401
        assert "invalid or revoked" in str(error).lower()
    else:
        raise RuntimeError("Revoked registration was unexpectedly accepted")

finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)

try:
    DeluxeSyncApi("not-a-url", plugin_version="0.1.0.0")
except ApiError as error:
    assert "http://" in str(error) and "https://" in str(error)
else:
    raise RuntimeError("Malformed server URL was unexpectedly accepted")

try:
    DeluxeSyncApi(
        "http://127.0.0.1:1",
        plugin_version="0.1.0.0",
        timeout=1,
    ).discover_capabilities()
except ApiError as error:
    assert "could not reach the server" in str(error).lower()
else:
    raise RuntimeError("Unreachable server was unexpectedly accepted")

with LOG_FILE.open("rb") as stream:
    stream.seek(LOG_START)
    appended_log = stream.read().decode("utf-8", errors="replace")

for marker in (
    f"registration_code={REDACTED}",
    f"client_uuid={REDACTED}",
    f"authorization={REDACTED}",
):
    if marker not in appended_log:
        raise RuntimeError(f"Missing redacted Stage 1 log marker: {marker}")

for sensitive_value in (
    "ABCD-EFGH",
    "client-uuid",
    Handler.session_value,
):
    if sensitive_value in appended_log:
        raise RuntimeError("Sensitive Stage 1 value leaked into deluxe-sync.log")

print("Deluxe Sync Stage 1 API smoke test passed")
