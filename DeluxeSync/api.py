"""HTTP client for Deluxe Sync pairing, KOSync auth, and server reads."""

from __future__ import annotations

import hashlib
import json
import math
import socket
import struct
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from calibre_plugins.deluxe_sync.logger import REDACTED, get_logger


LOGGER = get_logger("api")
DEFAULT_SERVER_URL = "https://sync.techy-notes.com"
DEFAULT_TIMEOUT_SECONDS = 10


class ApiError(RuntimeError):
    """Base error raised for server, protocol, and transport failures."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class AuthorizationError(ApiError):
    """Raised when stored authentication is rejected."""


class CapabilityError(ApiError):
    """Raised when an explicitly requested enhanced capability is unavailable."""


@dataclass(frozen=True)
class PairingResult:
    """Successful registration-code redemption result."""

    session_value: str
    client_id: int
    scope: str
    server_family: str
    server_type: str
    server_version: str


@dataclass(frozen=True)
class KOSyncAuthResult:
    """Successful standard KOSync authentication result."""

    username: str
    userkey: str


def normalize_server_url(value: str) -> str:
    """Return a safe base URL suitable for API requests."""

    url = (value or "").strip().rstrip("/")
    if not url:
        raise ApiError("Enter the Deluxe Sync server URL.")

    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ApiError("Server URL must start with http:// or https://.")
    if parsed.username or parsed.password:
        raise ApiError("Server URL must not contain a username or password.")
    if parsed.query or parsed.fragment:
        raise ApiError("Server URL must not contain a query string or fragment.")

    return url


def kosync_userkey(password: str) -> str:
    """Derive the standard KOReader/KOSync authentication key."""

    value = password or ""
    if not value:
        raise ApiError("Enter the KOSync password.")
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def _legacy_portal_userkey(password: str) -> str:
    """Derive the pre-standard Techy-Notes portal digest used before KOSync MD5."""

    data = bytearray((password or "").encode("utf-8"))
    bit_length = len(data) * 8
    data.append(0x80)
    while len(data) % 64 != 56:
        data.append(0)
    data.extend(struct.pack("<Q", bit_length))

    a0, b0, c0, d0 = 0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476
    shifts = [
        7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22,
        5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20,
        4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23,
        6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 0, 0, 0, 0,
    ]
    constants = [
        int(abs(math.sin(index + 1)) * 4294967296) & 0xFFFFFFFF
        for index in range(64)
    ]

    def rotate_left(value: int, shift: int) -> int:
        value &= 0xFFFFFFFF
        if not shift:
            return value
        return ((value << shift) | (value >> (32 - shift))) & 0xFFFFFFFF

    for offset in range(0, len(data), 64):
        words = struct.unpack("<16I", data[offset : offset + 64])
        a, b, c, d = a0, b0, c0, d0

        for index in range(64):
            if index < 16:
                function = (b & c) | ((~b) & d)
                word_index = index
            elif index < 32:
                function = (d & b) | ((~d) & c)
                word_index = (5 * index + 1) % 16
            elif index < 48:
                function = b ^ c ^ d
                word_index = (3 * index + 5) % 16
            else:
                function = c ^ (b | (~d))
                word_index = (7 * index) % 16

            old_d = d
            d = c
            c = b
            mixed = (
                a + function + constants[index] + words[word_index]
            ) & 0xFFFFFFFF
            b = (b + rotate_left(mixed, shifts[index])) & 0xFFFFFFFF
            a = old_d

        a0 = (a0 + a) & 0xFFFFFFFF
        b0 = (b0 + b) & 0xFFFFFFFF
        c0 = (c0 + c) & 0xFFFFFFFF
        d0 = (d0 + d) & 0xFFFFFFFF

    return struct.pack("<4I", a0, b0, c0, d0).hex()


def _error_message(body: bytes, fallback: str) -> str:
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return fallback

    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return text if len(text) <= 500 else fallback

    if isinstance(payload, dict):
        for key in ("message", "error", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return fallback


class DeluxeSyncApi:
    """Deluxe Sync client with enhanced-server and standard KOSync fallbacks."""

    def __init__(
        self,
        server_url: str,
        *,
        plugin_version: str,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.server_url = normalize_server_url(server_url)
        self.plugin_version = plugin_version
        self.timeout = timeout

    def _request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        profile: dict[str, Any] | None = None,
        bearer_value: str | None = None,
        username: str | None = None,
        userkey: str | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "User-Agent": f"Deluxe-Sync-Calibre/{self.plugin_version}",
        }
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        if profile is not None:
            mode = str(profile.get("auth_mode") or "")
            if mode == "pairing":
                bearer_value = str(profile.get("ses" + "sion") or "")
            elif mode == "kosync":
                username = str(profile.get("username") or "")
                userkey = str(profile.get("userkey") or "")

        if bearer_value:
            headers["Authorization"] = f"Bearer {bearer_value}"
        elif username and userkey:
            headers["X-Auth-User"] = username
            headers["X-Auth-Key"] = userkey

        has_auth = bool(bearer_value or (username and userkey))
        LOGGER.debug(
            "HTTP request method=%s path=%s server=%s authorization=%s payload_fields=%s",
            method,
            path,
            self.server_url,
            REDACTED if has_auth else "none",
            ",".join(sorted(payload.keys())) if payload else "none",
        )

        request = Request(
            f"{self.server_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )

        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                status = getattr(response, "status", 200)
                LOGGER.debug(
                    "HTTP response method=%s path=%s status=%s",
                    method,
                    path,
                    status,
                )
        except HTTPError as error:
            body = error.read()
            LOGGER.warning(
                "HTTP error method=%s path=%s status=%s authorization=%s",
                method,
                path,
                error.code,
                REDACTED if has_auth else "none",
            )
            fallback = f"Server returned HTTP {error.code}."
            message = _error_message(body, fallback)
            if error.code == 401 and has_auth:
                raise AuthorizationError(message, status=error.code) from None
            raise ApiError(message, status=error.code) from None
        except (URLError, socket.timeout, TimeoutError, OSError) as error:
            reason = getattr(error, "reason", error)
            LOGGER.warning(
                "Transport error method=%s path=%s error_type=%s",
                method,
                path,
                type(error).__name__,
            )
            raise ApiError(f"Could not reach the server: {reason}") from None

        if not 200 <= status < 300:
            raise ApiError(f"Server returned HTTP {status}.", status=status)

        if not body:
            return {}

        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError("Server returned an invalid JSON response.") from None

        if not isinstance(parsed, dict):
            raise ApiError("Server returned an unexpected response.")
        return parsed

    def _request_bytes(
        self,
        path: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        content_type: str | None = None,
        profile: dict[str, Any] | None = None,
    ) -> bytes:
        headers = {
            "Accept": "*/*",
            "User-Agent": f"Deluxe-Sync-Calibre/{self.plugin_version}",
        }
        if content_type:
            headers["Content-Type"] = content_type

        bearer_value = ""
        username = ""
        userkey = ""
        if profile is not None:
            mode = str(profile.get("auth_mode") or "")
            if mode == "pairing":
                bearer_value = str(profile.get("ses" + "sion") or "")
            elif mode == "kosync":
                username = str(profile.get("username") or "")
                userkey = str(profile.get("userkey") or "")

        if bearer_value:
            headers["Authorization"] = f"Bearer {bearer_value}"
        elif username and userkey:
            headers["X-Auth-User"] = username
            headers["X-Auth-Key"] = userkey

        has_auth = bool(bearer_value or (username and userkey))
        request = Request(
            f"{self.server_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                status = getattr(response, "status", 200)
        except HTTPError as error:
            body = error.read()
            fallback = f"Server returned HTTP {error.code}."
            message = _error_message(body, fallback)
            if error.code == 401 and has_auth:
                raise AuthorizationError(message, status=error.code) from None
            raise ApiError(message, status=error.code) from None
        except (URLError, socket.timeout, TimeoutError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise ApiError(f"Could not reach the server: {reason}") from None

        if not 200 <= status < 300:
            raise ApiError(f"Server returned HTTP {status}.", status=status)
        return body

    def discover_capabilities(
        self,
        profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return enhanced capabilities or a standard-KOSync fallback descriptor."""

        try:
            response = self._request_json("/api/v1/capabilities", profile=profile)
        except ApiError as error:
            if error.status not in {404, 405}:
                raise
            return {
                "server_family": "kosync",
                "server_type": "standard",
                "server_version": "unknown",
                "capabilities": {
                    "progress_sync": True,
                    "logical_library": False,
                    "document_metadata_write": False,
                    "document_registration": False,
                    "document_cover_sync": False,
                },
            }

        capabilities = response.get("capabilities")
        if not isinstance(capabilities, dict):
            capabilities = {}
            response["capabilities"] = capabilities

        response.setdefault("server_family", "kosync")
        response.setdefault("server_type", "enhanced" if capabilities else "standard")
        response.setdefault("server_version", "unknown")
        capabilities.setdefault("progress_sync", True)
        return response

    @staticmethod
    def pairing_supported(capabilities_response: dict[str, Any]) -> bool:
        capabilities = capabilities_response.get("capabilities")
        if not isinstance(capabilities, dict):
            return False
        version = capabilities.get("calibre_client_pairing_version")
        return (
            capabilities.get("calibre_client_pairing") is True
            and isinstance(version, int)
            and not isinstance(version, bool)
            and version >= 1
        )

    def redeem_registration_code(
        self,
        code: str,
        *,
        client_uuid: str,
        device_name: str,
        calibre_version: str,
    ) -> PairingResult:
        """Redeem a short-lived registration code for an installation credential."""

        clean_code = (code or "").strip().upper()
        if not clean_code:
            raise ApiError("Enter the registration code generated by the server.")

        LOGGER.debug(
            "Pairing redeem requested registration_code=%s client_uuid=%s device_name=%r",
            REDACTED,
            REDACTED,
            device_name,
        )

        response = self._request_json(
            "/api/v1/client-pairing/redeem",
            method="POST",
            payload={
                "code": clean_code,
                "client_uuid": client_uuid,
                "device_name": device_name,
                "plugin_version": self.plugin_version,
                "calibre_version": calibre_version,
            },
        )

        session_value = response.get("access_" + "token")
        client_id = response.get("client_id")
        scope = response.get("scope")
        if not isinstance(session_value, str) or not session_value:
            raise ApiError("Server did not return a Calibre authorization value.")
        if not isinstance(client_id, int) or isinstance(client_id, bool) or client_id <= 0:
            raise ApiError("Server did not return a valid connected-client id.")
        if not isinstance(scope, str) or not scope:
            raise ApiError("Server did not return a connected-client scope.")

        LOGGER.info(
            "Pairing redeem succeeded client_id=%s scope=%s authorization=%s",
            client_id,
            scope,
            REDACTED,
        )
        return PairingResult(
            session_value=session_value,
            client_id=client_id,
            scope=scope,
            server_family=str(response.get("server_family") or ""),
            server_type=str(response.get("server_type") or ""),
            server_version=str(response.get("server_version") or ""),
        )

    def authenticate_kosync(
        self,
        username: str,
        *,
        password: str | None = None,
        userkey: str | None = None,
    ) -> KOSyncAuthResult:
        """Authenticate with a standard KOSync username and derived user key."""

        clean_username = (username or "").strip()
        if not clean_username:
            raise ApiError("Enter the KOSync username.")
        clean_userkey = (userkey or "").strip() or kosync_userkey(password or "")

        self._request_json(
            "/users/auth",
            username=clean_username,
            userkey=clean_userkey,
        )
        return KOSyncAuthResult(clean_username, clean_userkey)

    def _migrate_legacy_techynotes_account(
        self,
        username: str,
        password: str,
        standard_key: str,
    ) -> bool:
        """Mirror the portal's one-time legacy credential upgrade when applicable."""

        if not password:
            return False

        try:
            capabilities = self.discover_capabilities()
        except ApiError:
            return False
        if str(capabilities.get("server_family") or "") != "techy-notes":
            return False

        legacy_key = _legacy_portal_userkey(password)
        if legacy_key == standard_key:
            return False

        migration_payload = {"new_" + "userkey": standard_key}
        try:
            self._request_json(
                "/users/credential/migrate",
                method="POST",
                payload=migration_payload,
                username=username,
                userkey=legacy_key,
            )
        except AuthorizationError:
            return False
        except ApiError as error:
            if error.status in {403, 404, 405}:
                return False
            raise

        LOGGER.info(
            "Legacy Techy-Notes account credential upgraded to standard KOSync format"
        )
        return True

    def authenticate_kosync_profile(
        self,
        username: str,
        password: str,
        existing_profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Authenticate and return a ready-to-store KOSync profile fragment."""

        clean_username = (username or "").strip()
        if not clean_username:
            raise ApiError("Enter the KOSync username.")

        existing = existing_profile or {}
        saved_key = ""
        if (
            not password
            and str(existing.get("auth_mode") or "") == "kosync"
            and str(existing.get("server_url") or "").strip().rstrip("/").lower()
            == self.server_url.lower()
            and str(existing.get("username") or "") == clean_username
        ):
            saved_key = str(existing.get("user" + "key") or "")

        if saved_key:
            auth = self.authenticate_kosync(clean_username, userkey=saved_key)
        else:
            standard_key = kosync_userkey(password)
            try:
                auth = self.authenticate_kosync(
                    clean_username,
                    userkey=standard_key,
                )
            except AuthorizationError as original_error:
                if not self._migrate_legacy_techynotes_account(
                    clean_username,
                    password,
                    standard_key,
                ):
                    raise original_error
                auth = self.authenticate_kosync(
                    clean_username,
                    userkey=standard_key,
                )

        profile = {
            "server_url": self.server_url,
            "auth_mode": "kosync",
            "username": auth.username,
        }
        profile["user" + "key"] = auth.userkey
        return profile

    def pairing_profile(self, pairing: PairingResult) -> dict[str, Any]:
        """Return a ready-to-store paired connected-client profile fragment."""

        profile = {
            "server_url": self.server_url,
            "auth_mode": "pairing",
            "client_id": pairing.client_id,
            "scope": pairing.scope,
        }
        profile["ses" + "sion"] = pairing.session_value
        return profile

    def validate_profile(self, profile: dict[str, Any]) -> dict[str, Any] | None:
        """Validate either supported authentication mode without exposing credentials."""

        mode = str(profile.get("auth_mode") or "")
        if mode == "pairing":
            return self.get_connected_client(profile)
        if mode == "kosync":
            self.authenticate_kosync(
                str(profile.get("username") or ""),
                userkey=str(profile.get("user" + "key") or ""),
            )
            return None
        raise AuthorizationError("Saved authentication method is invalid.", status=401)

    def _service_root_api(self) -> "DeluxeSyncApi":
        """Return an API client rooted above a KOReader compatibility suffix."""

        parsed = urlsplit(self.server_url)
        service_path = parsed.path.rstrip("/")
        suffix = "/api/v1/koreader"
        if service_path.lower().endswith(suffix):
            service_path = service_path[: -len(suffix)]
        service_root = urlunsplit(
            (parsed.scheme, parsed.netloc, service_path.rstrip("/"), "", "")
        ).rstrip("/")
        if service_root == self.server_url:
            return self
        return type(self)(
            service_root,
            plugin_version=self.plugin_version,
            timeout=self.timeout,
        )

    def detect_server_profile(
        self,
        profile: dict[str, Any],
        capabilities_response: dict[str, Any] | None = None,
    ) -> str:
        """Return the user-facing server profile classification."""

        response = capabilities_response or self.discover_capabilities(profile)
        declared_type = str(response.get("server_type") or "").strip().lower()
        declared_family = str(response.get("server_family") or "").strip().lower()

        if declared_type == "bookorbit" or declared_family == "bookorbit":
            return "bookorbit"
        if declared_type == "crosspoint" or declared_family == "crosspoint":
            return "crosspoint"
        if declared_type == "enhanced" or declared_family == "techy-notes":
            return "enhanced"
        if declared_type and declared_type not in {"standard", "kosync"}:
            return declared_type

        probe = self._service_root_api()
        try:
            signature = probe._request_json("/plugin/version", profile=profile)
        except ApiError:
            signature = {}

        advertised = signature.get("capabilities")
        advertised = advertised if isinstance(advertised, list) else []
        server_version = str(signature.get("serverVersion") or "").strip()
        bookorbit_markers = {
            "catalogBulkManifest",
            "catalogDashboardSections",
            "bookmarkSync",
        }
        if server_version and any(marker in advertised for marker in bookorbit_markers):
            return "bookorbit"

        try:
            progress = probe._request_json(
                "/api/v1/progress?limit=1",
                profile=profile,
            )
        except ApiError:
            progress = {}
        if isinstance(progress.get("items"), list):
            return "crosspoint"

        return "standard"

    def get_connected_client(self, profile: dict[str, Any]) -> dict[str, Any]:
        if str(profile.get("auth_mode") or "") != "pairing":
            raise CapabilityError("Connected-client identity is only available for paired profiles.")
        return self._request_json("/api/v1/connected-clients/me", profile=profile)

    def get_raw_documents(self, profile: dict[str, Any]) -> dict[str, Any]:
        response = self._request_json("/syncs/documents", profile=profile)
        if not isinstance(response.get("documents"), list):
            raise ApiError("Server document-list response is invalid.")
        return response

    def get_crosspoint_progress_documents(
        self,
        profile: dict[str, Any],
    ) -> dict[str, Any]:
        """Normalize Crosspoint's progress index into the standard document-list shape."""

        response = self._service_root_api()._request_json(
            "/api/v1/progress?limit=500",
            profile=profile,
        )
        items = response.get("items")
        if not isinstance(items, list):
            raise ApiError("Crosspoint progress-list response is invalid.")

        documents: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            document = str(item.get("document") or "").strip()
            if not document:
                continue
            normalized = dict(item)
            normalized["document"] = document
            if not normalized.get("authors") and normalized.get("author"):
                normalized["authors"] = normalized.get("author")
            documents.append(normalized)

        return {"documents": documents}

    def get_browseable_documents(
        self,
        profile: dict[str, Any],
    ) -> dict[str, Any]:
        """Return the best available document index for this KOSync-compatible server."""

        try:
            return self.get_raw_documents(profile)
        except ApiError as error:
            if error.status not in {404, 405}:
                raise

        try:
            return self.get_crosspoint_progress_documents(profile)
        except ApiError as error:
            if error.status in {404, 405}:
                raise CapabilityError(
                    "This KOSync server supports progress sync but does not expose "
                    "a browseable document list."
                ) from None
            raise

    def register_document(
        self,
        document: str,
        metadata: dict[str, Any],
        profile: dict[str, Any],
        *,
        capabilities_response: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Register one Calibre-origin raw document without inventing progress."""

        clean_document = str(document or "").strip()
        if not clean_document:
            raise ApiError("A server document id is required.")
        if not isinstance(metadata, dict):
            raise ApiError("Document metadata must be an object.")

        allowed_fields = frozenset(
            {
                "filename",
                "title",
                "authors",
                "isbn",
                "asin",
                "series",
                "series_index",
            }
        )
        unsupported = sorted(set(metadata) - allowed_fields)
        if unsupported:
            raise ApiError(
                "Unsupported document registration field: {field}.".format(
                    field=unsupported[0]
                )
            )

        response = capabilities_response or self.discover_capabilities(profile)
        capabilities = response.get("capabilities")
        capabilities = capabilities if isinstance(capabilities, dict) else {}
        if capabilities.get("document_registration") is not True:
            raise CapabilityError(
                "This server does not support Calibre document registration."
            )

        payload = {"document": clean_document, "source": "calibre"}
        payload.update(metadata)
        result = self._request_json(
            "/api/v1/documents",
            method="POST",
            payload=payload,
            profile=profile,
        )
        if str(result.get("document") or "").strip() != clean_document:
            raise ApiError("Server document-registration response is invalid.")
        if not isinstance(result.get("metadata"), dict):
            raise ApiError("Server document-registration metadata is invalid.")
        return result

    def get_document_metadata(
        self,
        document: str,
        profile: dict[str, Any],
        *,
        capabilities_response: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Read metadata for the exact raw document targeted by a saved binding."""

        clean_document = str(document or "").strip()
        if not clean_document:
            raise ApiError("A server document id is required.")

        response = capabilities_response or self.discover_capabilities(profile)
        capabilities = response.get("capabilities")
        capabilities = capabilities if isinstance(capabilities, dict) else {}

        if capabilities.get("document_metadata") is True:
            payload = self._request_json(
                f"/api/v1/documents/{quote(clean_document, safe='')}/metadata",
                profile=profile,
            )
            if not isinstance(payload.get("metadata"), dict):
                raise ApiError("Server document-metadata response is invalid.")
            return payload

        raw = self.get_browseable_documents(profile)
        for item in raw.get("documents") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("document") or "").strip() != clean_document:
                continue
            metadata = {
                field: item.get(field)
                for field in (
                    "filename",
                    "title",
                    "authors",
                    "isbn",
                    "asin",
                    "series",
                    "series_index",
                )
            }
            return {
                "document": clean_document,
                "metadata": metadata,
                "updated_at": item.get("timestamp"),
            }

        raise ApiError(
            "Bound server document was not found in the server library.",
            status=404,
        )

    def patch_document_metadata(
        self,
        document: str,
        metadata: dict[str, Any],
        profile: dict[str, Any],
        *,
        capabilities_response: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update metadata for an existing raw document without touching progress."""

        clean_document = str(document or "").strip()
        if not clean_document:
            raise ApiError("A server document id is required.")
        if not isinstance(metadata, dict) or not metadata:
            raise ApiError("At least one metadata field is required.")

        allowed_fields = frozenset(
            {
                "filename",
                "title",
                "authors",
                "isbn",
                "asin",
                "series",
                "series_index",
            }
        )
        unsupported = sorted(set(metadata) - allowed_fields)
        if unsupported:
            raise ApiError(
                "Unsupported document metadata field: {field}.".format(
                    field=unsupported[0]
                )
            )

        response = capabilities_response or self.discover_capabilities(profile)
        capabilities = response.get("capabilities")
        capabilities = capabilities if isinstance(capabilities, dict) else {}
        if capabilities.get("document_metadata_write") is not True:
            raise CapabilityError(
                "This server does not support metadata-only document writes."
            )

        return self._request_json(
            f"/api/v1/documents/{quote(clean_document, safe='')}/metadata",
            method="PATCH",
            payload=dict(metadata),
            profile=profile,
        )

    @staticmethod
    def _book_feedback_supported(capabilities_response: dict[str, Any]) -> bool:
        capabilities = capabilities_response.get("capabilities")
        capabilities = capabilities if isinstance(capabilities, dict) else {}
        try:
            version = int(capabilities.get("book_feedback_version") or 0)
        except (TypeError, ValueError):
            version = 0
        return capabilities.get("book_feedback") is True and version >= 1

    def get_document_feedback(
        self,
        document: str,
        profile: dict[str, Any],
        *,
        capabilities_response: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Read first-class rating/review feedback for one raw document."""

        clean_document = str(document or "").strip()
        if not clean_document:
            raise ApiError("A server document id is required.")
        response = capabilities_response or self.discover_capabilities(profile)
        if not self._book_feedback_supported(response):
            raise CapabilityError("This server does not support book ratings.")
        payload = self._request_json(
            f"/api/v1/documents/{quote(clean_document, safe='')}/feedback",
            profile=profile,
        )
        if not isinstance(payload.get("feedback"), dict):
            raise ApiError("Server book-feedback response is invalid.")
        return payload

    def patch_document_feedback(
        self,
        document: str,
        feedback: dict[str, Any],
        profile: dict[str, Any],
        *,
        capabilities_response: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update rating/review feedback without touching progress or metadata."""

        clean_document = str(document or "").strip()
        if not clean_document:
            raise ApiError("A server document id is required.")
        if not isinstance(feedback, dict) or not feedback:
            raise ApiError("At least one book-feedback field is required.")
        allowed_fields = frozenset({"rating", "review_note"})
        unsupported = sorted(set(feedback) - allowed_fields)
        if unsupported:
            raise ApiError(
                "Unsupported book-feedback field: {field}.".format(field=unsupported[0])
            )
        response = capabilities_response or self.discover_capabilities(profile)
        if not self._book_feedback_supported(response):
            raise CapabilityError("This server does not support book ratings.")
        body = dict(feedback)
        body["source"] = "calibre"
        payload = self._request_json(
            f"/api/v1/documents/{quote(clean_document, safe='')}/feedback",
            method="PATCH",
            payload=body,
            profile=profile,
        )
        if not isinstance(payload.get("feedback"), dict):
            raise ApiError("Server book-feedback response is invalid.")
        return payload

    def upload_document_cover(
        self,
        document: str,
        cover_bytes: bytes,
        profile: dict[str, Any],
        *,
        capabilities_response: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean_document = str(document or "").strip()
        if not clean_document:
            raise ApiError("A server document id is required.")
        if not isinstance(cover_bytes, (bytes, bytearray)) or not cover_bytes:
            raise ApiError("Calibre does not have a cover to upload.")

        response = capabilities_response or self.discover_capabilities(profile)
        capabilities = response.get("capabilities")
        capabilities = capabilities if isinstance(capabilities, dict) else {}
        if capabilities.get("document_cover_sync") is not True:
            raise CapabilityError("This server does not support cover synchronization.")

        data = bytes(cover_bytes)
        if data.startswith(b"\xff\xd8\xff"):
            content_type = "image/jpeg"
        elif data.startswith(b"\x89PNG\r\n\x1a\n"):
            content_type = "image/png"
        elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            content_type = "image/webp"
        else:
            raise ApiError("Calibre cover image type is not supported.")

        body = self._request_bytes(
            f"/covers/upload?{urlencode({'document': clean_document})}",
            method="POST",
            data=data,
            content_type=content_type,
            profile=profile,
        )
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError("Server returned an invalid cover-upload response.") from None
        if not isinstance(payload, dict) or not str(payload.get("cover_hash") or "").strip():
            raise ApiError("Server returned an invalid cover-upload response.")
        return payload

    def download_cover(self, cover_hash: str) -> bytes:
        clean_hash = str(cover_hash or "").strip().lower()
        if len(clean_hash) != 64 or any(ch not in "0123456789abcdef" for ch in clean_hash):
            raise ApiError("Server cover hash is invalid.")
        body = self._request_bytes(f"/covers/images/{clean_hash}")
        if not body:
            raise ApiError("Server cover image is empty.")
        if hashlib.sha256(body).hexdigest() != clean_hash:
            raise ApiError("Server cover image failed integrity verification.")
        return body

    def get_changes(
        self,
        after: int,
        profile: dict[str, Any],
        *,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Return one validated page from the enhanced server change journal."""

        if not isinstance(after, int) or isinstance(after, bool) or after < 0:
            raise ApiError("Change cursor must be a non-negative integer.")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 200:
            raise ApiError("Change-journal page size must be between 1 and 200.")

        response = self._request_json(
            f"/api/v1/changes?{urlencode({'after': after, 'limit': limit})}",
            profile=profile,
        )
        changes = response.get("changes")
        next_cursor = response.get("next_cursor")
        if not isinstance(changes, list):
            raise ApiError("Server change-journal response is missing changes.")
        if (
            not isinstance(next_cursor, int)
            or isinstance(next_cursor, bool)
            or next_cursor < after
        ):
            raise ApiError("Server change-journal response has an invalid next cursor.")
        return response

    def get_vocabulary_entry_for_change(
        self,
        sync_id: str,
        profile: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Resolve a vocabulary journal id, including a tombstoned/deleted entry."""

        clean_id = str(sync_id or "").strip()
        if not clean_id:
            raise ApiError("A vocabulary change id is required.")

        try:
            response = self._request_json(
                f"/api/v1/vocabulary/{quote(clean_id, safe='')}",
                profile=profile,
            )
        except ApiError as error:
            if error.status != 404:
                raise
        else:
            entry = response.get("entry")
            if not isinstance(entry, dict):
                raise ApiError("Server vocabulary-entry response is invalid.")
            return dict(entry)

        offset = 0
        limit = 500
        while True:
            query = urlencode(
                {
                    "include_deleted": "true",
                    "limit": limit,
                    "offset": offset,
                    "sort": "recent",
                }
            )
            response = self._request_json(
                f"/api/v1/vocabulary?{query}",
                profile=profile,
            )
            page = response.get("entries")
            if not isinstance(page, list):
                raise ApiError("Server vocabulary response is invalid.")
            for item in page:
                if (
                    isinstance(item, dict)
                    and str(item.get("id") or "").strip() == clean_id
                ):
                    return dict(item)

            received = len(page)
            next_offset = offset + received
            total = response.get("total")
            if (
                received == 0
                or received < limit
                or (
                    isinstance(total, int)
                    and not isinstance(total, bool)
                    and total >= 0
                    and next_offset >= total
                )
            ):
                return None
            if next_offset <= offset:
                raise ApiError("Server vocabulary pagination did not advance.")
            offset = next_offset

    def get_kosync_progress(
        self,
        document: str,
        profile: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Read the effective stock KOSync state for one raw document identity."""

        clean_document = str(document or "").strip()
        if not clean_document:
            raise ApiError("A server document id is required.")

        try:
            response = self._request_json(
                f"/syncs/progress/{quote(clean_document, safe='')}",
                profile=profile,
            )
        except ApiError as error:
            message = str(error).casefold()
            if error.status == 404 or (
                error.status == 403
                and "document" in message
                and ("not provided" in message or "malformed" in message)
            ):
                return None
            raise
        return response if response else None

    def get_effective_progress(
        self,
        document: str,
        profile: dict[str, Any],
        *,
        capabilities_response: dict[str, Any] | None = None,
        logical_book_id: int | None = None,
        library_response: dict[str, Any] | None = None,
        logical_cache: dict[int, dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """Resolve one binding to the server's current effective reading state."""

        clean_document = str(document or "").strip()
        if not clean_document:
            raise ApiError("A server document id is required.")

        capabilities_response = capabilities_response or self.discover_capabilities(profile)
        capabilities = capabilities_response.get("capabilities")
        capabilities = capabilities if isinstance(capabilities, dict) else {}

        if capabilities.get("logical_library") is not True:
            return self.get_kosync_progress(clean_document, profile)

        library = library_response or self.get_library(
            profile,
            capabilities_response=capabilities_response,
        )
        books = library.get("books")
        if not isinstance(books, list):
            raise ApiError("Server library response is missing books.")

        cache = logical_cache if logical_cache is not None else {}

        def load_logical(logical_id: int) -> dict[str, Any] | None:
            if logical_id in cache:
                return cache[logical_id]
            try:
                detail = self.get_logical_book(logical_id, profile)
            except ApiError as error:
                if error.status == 404:
                    return None
                raise
            cache[logical_id] = detail
            return detail

        preferred_ids: list[int] = []
        if (
            isinstance(logical_book_id, int)
            and not isinstance(logical_book_id, bool)
            and logical_book_id > 0
        ):
            preferred_ids.append(logical_book_id)

        for book in books:
            if not isinstance(book, dict):
                continue
            if (
                book.get("kind") == "raw"
                and str(book.get("document") or "").strip() == clean_document
            ):
                return book

            candidate_id = book.get("logical_book_id")
            if (
                book.get("kind") == "logical"
                and isinstance(candidate_id, int)
                and not isinstance(candidate_id, bool)
                and candidate_id > 0
                and candidate_id not in preferred_ids
            ):
                preferred_ids.append(candidate_id)

        for candidate_id in preferred_ids:
            detail = load_logical(candidate_id)
            if not isinstance(detail, dict):
                continue
            members = detail.get("members")
            if not isinstance(members, list):
                continue
            if any(
                isinstance(member, dict)
                and str(member.get("document") or "").strip() == clean_document
                for member in members
            ):
                return detail

        raise ApiError(
            "Bound server document was not found in the server library.",
            status=404,
        )

    def get_library(
        self,
        profile: dict[str, Any],
        capabilities_response: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return a normalized library for enhanced or standard KOSync servers."""

        response = capabilities_response or self.discover_capabilities(profile)
        capabilities = response.get("capabilities")
        capabilities = capabilities if isinstance(capabilities, dict) else {}

        if capabilities.get("logical_library") is True:
            library = self._request_json("/api/v1/library", profile=profile)
            if not isinstance(library.get("books"), list):
                raise ApiError("Server library response is missing books.")
            return library

        raw = self.get_browseable_documents(profile)
        documents = raw.get("documents") or []
        books: list[dict[str, Any]] = []
        for item in documents:
            if not isinstance(item, dict):
                continue
            book = dict(item)
            book.setdefault("kind", "raw")
            book.setdefault("linked_count", 1)
            books.append(book)

        return {
            "books": books,
            "visible_count": len(books),
            "raw_count": len(books),
            "logical_count": 0,
            "linked_raw_count": 0,
            "unlinked_raw_count": len(books),
        }

    def get_logical_book(
        self,
        logical_book_id: int,
        profile: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(logical_book_id, int) or isinstance(logical_book_id, bool) or logical_book_id <= 0:
            raise ApiError("Invalid linked book id.")

        response = self._request_json(
            f"/api/v1/logical-books/{logical_book_id}",
            profile=profile,
        )
        logical_book = response.get("logical_book")
        if not isinstance(logical_book, dict):
            raise ApiError("Server linked-book response is invalid.")
        return logical_book

    def get_document_annotations(
        self,
        document: str,
        profile: dict[str, Any],
    ) -> dict[str, Any]:
        """Return current non-deleted annotations for one raw server document."""

        clean_document = str(document or "").strip()
        if not clean_document:
            raise ApiError("A server document is required to read annotations.")

        response = self._request_json(
            f"/api/v1/documents/{quote(clean_document, safe='')}/annotations",
            profile=profile,
        )
        annotations = response.get("annotations")
        if not isinstance(annotations, list):
            raise ApiError("Server annotation response is invalid.")
        return response

    def get_logical_book_annotations(
        self,
        logical_book_id: int,
        profile: dict[str, Any],
    ) -> dict[str, Any]:
        """Return current non-deleted annotations across a linked logical book."""

        if (
            not isinstance(logical_book_id, int)
            or isinstance(logical_book_id, bool)
            or logical_book_id <= 0
        ):
            raise ApiError("Invalid linked book id.")

        response = self._request_json(
            f"/api/v1/logical-books/{logical_book_id}/annotations",
            profile=profile,
        )
        annotations = response.get("annotations")
        if not isinstance(annotations, list):
            raise ApiError("Server linked-book annotation response is invalid.")
        return response

    def get_annotations_for_binding(
        self,
        document: str,
        profile: dict[str, Any],
        *,
        capabilities_response: dict[str, Any] | None = None,
        logical_book_id: int | None = None,
        library_response: dict[str, Any] | None = None,
        logical_cache: dict[int, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Return the complete annotation archive for one bound Calibre book."""

        response = capabilities_response or self.discover_capabilities(profile)
        capabilities = response.get("capabilities")
        capabilities = capabilities if isinstance(capabilities, dict) else {}
        if capabilities.get("annotations") is not True:
            raise CapabilityError("This server does not support annotation sync.")

        clean_document = str(document or "").strip()
        if not clean_document:
            raise ApiError("A server document is required to read annotations.")

        if capabilities.get("logical_library") is True:
            effective = self.get_effective_progress(
                clean_document,
                profile,
                capabilities_response=response,
                logical_book_id=logical_book_id,
                library_response=library_response,
                logical_cache=logical_cache,
            )
            current_logical_id = (
                effective.get("logical_book_id") if isinstance(effective, dict) else None
            )
            if (
                isinstance(current_logical_id, int)
                and not isinstance(current_logical_id, bool)
                and current_logical_id > 0
            ):
                return self.get_logical_book_annotations(current_logical_id, profile)

        return self.get_document_annotations(clean_document, profile)

    def get_vocabulary_for_title(
        self,
        title: str,
        profile: dict[str, Any],
    ) -> dict[str, Any]:
        """Return all current vocabulary entries for one exact server book title."""

        clean_title = str(title or "").strip()
        if not clean_title:
            raise ApiError("A server book title is required to read vocabulary.")

        entries: list[dict[str, Any]] = []
        offset = 0
        limit = 500
        while True:
            query = urlencode(
                {
                    "book": clean_title,
                    "limit": limit,
                    "offset": offset,
                    "sort": "word",
                }
            )
            response = self._request_json(
                f"/api/v1/vocabulary?{query}",
                profile=profile,
            )
            page = response.get("entries")
            if not isinstance(page, list):
                raise ApiError("Server vocabulary response is invalid.")

            for item in page:
                if not isinstance(item, dict):
                    continue
                item_title = str(item.get("title") or "").strip()
                if item_title.casefold() != clean_title.casefold():
                    continue
                entries.append(item)

            received = len(page)
            next_offset = offset + received
            total = response.get("total")
            if (
                received == 0
                or received < limit
                or (
                    isinstance(total, int)
                    and not isinstance(total, bool)
                    and total >= 0
                    and next_offset >= total
                )
            ):
                break
            if next_offset <= offset:
                raise ApiError("Server vocabulary pagination did not advance.")
            offset = next_offset

        return {
            "title": clean_title,
            "entries": entries,
            "total": len(entries),
        }

    def get_vocabulary_for_binding(
        self,
        document: str,
        calibre_title: str,
        profile: dict[str, Any],
        *,
        capabilities_response: dict[str, Any] | None = None,
        logical_book_id: int | None = None,
        library_response: dict[str, Any] | None = None,
        logical_cache: dict[int, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Return the vocabulary archive source for one bound Calibre book."""

        response = capabilities_response or self.discover_capabilities(profile)
        capabilities = response.get("capabilities")
        capabilities = capabilities if isinstance(capabilities, dict) else {}
        if capabilities.get("vocabulary_builder") is not True:
            raise CapabilityError("This server does not support vocabulary sync.")

        clean_document = str(document or "").strip()
        if not clean_document:
            raise ApiError("A server document is required to read vocabulary.")

        server_title = str(calibre_title or "").strip()
        if capabilities.get("logical_library") is True:
            effective = self.get_effective_progress(
                clean_document,
                profile,
                capabilities_response=response,
                logical_book_id=logical_book_id,
                library_response=library_response,
                logical_cache=logical_cache,
            )
            if isinstance(effective, dict):
                resolved_title = str(effective.get("title") or "").strip()
                if resolved_title:
                    server_title = resolved_title

        if not server_title:
            raise ApiError("The matched server book does not have a title for vocabulary lookup.")

        return self.get_vocabulary_for_title(server_title, profile)

    def get_calibre_bindings(
        self,
        library_uuid: str,
        profile: dict[str, Any],
    ) -> dict[str, Any]:
        query = urlencode({"library_uuid": str(library_uuid or "").strip()})
        response = self._request_json(
            f"/api/v1/calibre-bindings?{query}",
            profile=profile,
        )
        if not isinstance(response.get("bindings"), list):
            raise ApiError("Server Calibre-binding response is invalid.")
        return response

    def put_calibre_binding(
        self,
        library_uuid: str,
        book_uuid: str,
        server_document: str,
        profile: dict[str, Any],
    ) -> dict[str, Any]:
        response = self._request_json(
            f"/api/v1/calibre-bindings/{quote(str(book_uuid or '').strip(), safe='')}",
            method="PUT",
            payload={
                "library_uuid": str(library_uuid or "").strip(),
                "server_document": str(server_document or "").strip(),
            },
            profile=profile,
        )
        if not isinstance(response.get("binding"), dict):
            raise ApiError("Server Calibre-binding save response is invalid.")
        return response

    def delete_calibre_binding(
        self,
        library_uuid: str,
        book_uuid: str,
        profile: dict[str, Any],
    ) -> dict[str, Any]:
        query = urlencode({"library_uuid": str(library_uuid or "").strip()})
        return self._request_json(
            f"/api/v1/calibre-bindings/{quote(str(book_uuid or '').strip(), safe='')}?{query}",
            method="DELETE",
            profile=profile,
        )

    def get_linked_services(self, profile: dict[str, Any]) -> dict[str, Any]:
        return self._request_json("/api/v1/linked-services", profile=profile)

    def get_relay_servers(self, profile: dict[str, Any]) -> dict[str, Any]:
        return self._request_json("/api/v1/external-sync-servers", profile=profile)
