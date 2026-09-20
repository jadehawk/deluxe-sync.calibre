"""Deterministic Stage 2.5 standard-KOSync authentication smoke test."""

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
    AuthorizationError,
    DeluxeSyncApi,
    kosync_userkey,
)


USERNAME = "standard-user"
LOGIN_PHRASE = "standard-smoke-" + "phrase"
EXPECTED_KEY = kosync_userkey(LOGIN_PHRASE)


class Handler(BaseHTTPRequestHandler):
    auth_requests = 0
    document_requests = 0
    migration_requests = 0
    last_user = ""
    last_key = ""

    def log_message(self, format, *args):
        return

    def _send_json(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        type(self).last_user = self.headers.get("X-Auth-User", "")
        type(self).last_key = self.headers.get("X-Auth-Key", "")
        return self.last_user == USERNAME and self.last_key == EXPECTED_KEY

    def do_GET(self):
        if self.path == "/users/auth":
            type(self).auth_requests += 1
            if not self._authorized():
                self._send_json(401, {"message": "invalid credentials"})
                return
            self._send_json(200, {"authorized": "OK"})
            return

        if self.path == "/api/v1/capabilities":
            self._send_json(404, {"message": "not found"})
            return

        if self.path == "/syncs/documents":
            type(self).document_requests += 1
            if not self._authorized():
                self._send_json(401, {"message": "invalid credentials"})
                return
            self._send_json(
                200,
                {
                    "documents": [
                        {
                            "document": "standard-book",
                            "filename": "Standard Book.epub",
                            "title": "Standard Book",
                            "authors": "Example Author",
                            "progress": "/body/DocFragment[2]",
                            "percentage": 0.42,
                            "timestamp": 123,
                        }
                    ]
                },
            )
            return

        self._send_json(404, {"message": "not found"})

    def do_POST(self):
        type(self).migration_requests += 1
        self._send_json(404, {"message": "not found"})


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
thread = Thread(target=server.serve_forever, daemon=True)
thread.start()
base_url = f"http://127.0.0.1:{server.server_port}"

try:
    api = DeluxeSyncApi(base_url, plugin_version="0.1.0.0", timeout=2)

    profile = api.authenticate_kosync_profile(USERNAME, LOGIN_PHRASE)
    assert profile["auth_mode"] == "kosync"
    assert profile["username"] == USERNAME
    assert profile["user" + "key"] == EXPECTED_KEY
    assert LOGIN_PHRASE not in repr(profile)
    assert Handler.last_user == USERNAME
    assert Handler.last_key == EXPECTED_KEY

    capabilities = api.discover_capabilities(profile)
    assert capabilities["server_family"] == "kosync"
    assert capabilities["server_type"] == "standard"
    assert capabilities["capabilities"]["progress_sync"] is True
    assert capabilities["capabilities"]["logical_library"] is False

    library = api.get_library(profile, capabilities)
    assert library["visible_count"] == 1
    assert library["raw_count"] == 1
    assert library["logical_count"] == 0
    assert library["books"][0]["kind"] == "raw"
    assert library["books"][0]["document"] == "standard-book"
    assert library["books"][0]["title"] == "Standard Book"
    assert Handler.document_requests == 1

    saved_profile = dict(profile)
    reused = api.authenticate_kosync_profile(USERNAME, "", saved_profile)
    assert reused["user" + "key"] == EXPECTED_KEY

    try:
        api.authenticate_kosync_profile(
            USERNAME,
            "wrong-smoke-" + "phrase",
        )
    except AuthorizationError as error:
        assert error.status == 401
        assert Handler.migration_requests == 0
    else:
        raise RuntimeError("Invalid standard KOSync credentials were unexpectedly accepted")

finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)

print("Deluxe Sync Stage 2.5 standard KOSync smoke test passed")
