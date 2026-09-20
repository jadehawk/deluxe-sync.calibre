"""Server connection configuration page for Deluxe Sync."""

from __future__ import annotations

import platform
from html import escape
from pathlib import Path
from typing import Any, Callable

from calibre.constants import __version__ as CALIBRE_VERSION
from qt.core import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTimer,
    QVBoxLayout,
    QWidget,
)

from calibre_plugins.deluxe_sync.api import (
    DEFAULT_SERVER_URL,
    ApiError,
    AuthorizationError,
    DeluxeSyncApi,
)
from calibre_plugins.deluxe_sync.binding_sync import recover_server_bindings, server_binding_supported
from calibre_plugins.deluxe_sync.bindings import (
    get_binding_tombstones,
    get_library_bindings,
    save_binding_record,
)
from calibre_plugins.deluxe_sync.logger import REDACTED, get_logger
from calibre_plugins.deluxe_sync.settings import (
    forget_server_profile,
    get_active_server_profile,
    get_active_server_profile_id,
    get_or_create_client_uuid,
    get_server_url,
    save_server_profile,
    set_server_url,
)


try:
    load_translations()
except NameError:
    def _(text):
        return text


LOGGER = get_logger("connection")


def _detail_table(rows: list[tuple[str, Any]]) -> str:
    """Render compact two-column details with every value aligned after the colon."""

    rendered = []
    for label, value in rows:
        rendered.append(
            "<tr>"
            f"<td nowrap style='padding-right:12px;'>{escape(str(label))}:</td>"
            f"<td>{escape(str(value))}</td>"
            "</tr>"
        )
    return "<table cellspacing='0' cellpadding='0'>" + "".join(rendered) + "</table>"


def _detail_lines(lines: list[str]) -> str:
    rows: list[tuple[str, str]] = []
    for line in lines:
        label, separator, value = str(line).partition(":")
        rows.append((label.strip(), value.strip() if separator else ""))
    return _detail_table(rows)


def _status_text(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    labels = {
        "connected": _("Connected"),
        "disconnected": _("Disconnected"),
        "disabled": _("Disabled"),
        "error": _("Error"),
        "needs_attention": _("Needs attention"),
        "needs-attention": _("Needs attention"),
        "unknown": _("Unknown"),
    }
    return labels.get(normalized, str(value or _("Unknown")).replace("_", " ").strip().title())


class ConnectionPage(QWidget):
    """Registration, validation, and server capability details."""

    def __init__(
        self,
        plugin_action,
        *,
        on_back: Callable[[], None],
        on_status_changed: Callable[[], None] | None = None,
        on_layout_changed: Callable[[], None] | None = None,
    ):
        super().__init__()
        self.action = plugin_action
        self.on_back = on_back
        self.on_status_changed = on_status_changed
        self.on_layout_changed = on_layout_changed
        self._auto_test_token = 0
        self.library_uuid, self.library_name = self._current_library()
        self.plugin_version = self._plugin_version()
        self.device_name = platform.node().strip() or "Calibre"
        self._busy = False
        self._summary_text = _("Not connected")

        layout = QVBoxLayout(self)

        header_row = QHBoxLayout()
        back_button = QPushButton(_("← Back"))
        back_button.clicked.connect(self.on_back)
        header_row.addWidget(back_button)

        heading = QLabel(_("<b>Server Connection</b>"))
        header_row.addWidget(heading)
        header_row.addStretch(1)
        layout.addLayout(header_row)

        intro = QLabel(
            _("Connect this Calibre installation to Techy-Notes or a standard KOSync "
              "server. Authentication is shared by every Calibre library; book mappings "
              "and sync state remain library-specific.")
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        connection_group = QGroupBox(_("Connection"))
        connection_layout = QFormLayout(connection_group)
        connection_layout.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )

        self.server_url = QLineEdit()
        self.server_url.setText(get_server_url())
        self.server_url.setPlaceholderText(DEFAULT_SERVER_URL)
        self.server_url.setClearButtonEnabled(True)
        self.server_url.setMinimumWidth(480)
        connection_layout.addRow(_("Server URL:"), self.server_url)

        self.auth_mode = QComboBox()
        self.auth_mode.addItem(_("Pair with Techy-Notes (Recommended)"), "pairing")
        self.auth_mode.addItem(_("KOSync username / password"), "kosync")
        self.auth_mode.currentIndexChanged.connect(self._auth_mode_changed)
        connection_layout.addRow(_("Authentication:"), self.auth_mode)

        self.registration_code = QLineEdit()
        self.registration_code.setPlaceholderText(_("ABCD-EFGH"))
        self.registration_code.setClearButtonEnabled(True)
        connection_layout.addRow(_("Registration code:"), self.registration_code)

        self.username = QLineEdit()
        self.username.setPlaceholderText(_("KOSync username"))
        self.username.setClearButtonEnabled(True)
        connection_layout.addRow(_("Username:"), self.username)

        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText(_("KOSync password"))
        self.password.setClearButtonEnabled(True)
        connection_layout.addRow(_("Password:"), self.password)

        self.library_label = QLabel(self.library_name or _("No active Calibre library"))
        self.library_label.setWordWrap(True)
        connection_layout.addRow(_("Current library:"), self.library_label)

        auth_help = QLabel(
            _("Pairing keeps a restricted Techy-Notes credential. KOSync sign-in stores "
              "the derived KOSync key, not the plaintext password.")
        )
        auth_help.setWordWrap(True)
        connection_layout.addRow(auth_help)

        button_row = QHBoxLayout()
        self.connect_button = QPushButton(_("Connect"))
        self.connect_button.clicked.connect(self.connect_registration)
        button_row.addWidget(self.connect_button)

        self.test_button = QPushButton(_("Test Connection"))
        self.test_button.clicked.connect(self.test_connection)
        button_row.addWidget(self.test_button)

        self.recover_button = QPushButton(_("Recover Server Bindings…"))
        self.recover_button.setToolTip(
            _(
                "Restore previously backed-up Deluxe Sync bindings for the current "
                "Calibre library without changing metadata or reading progress."
            )
        )
        self.recover_button.clicked.connect(self.recover_bindings)
        self.recover_button.setEnabled(False)
        button_row.addWidget(self.recover_button)

        self.disconnect_button = QPushButton(_("Disconnect / Forget"))
        self.disconnect_button.clicked.connect(self.forget_current_registration)
        button_row.addWidget(self.disconnect_button)
        connection_layout.addRow(button_row)

        layout.addWidget(connection_group)

        first_details_row = QHBoxLayout()

        status_group = QGroupBox(_("Server connection details"))
        status_layout = QVBoxLayout(status_group)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        status_layout.addWidget(self.status_label)
        status_layout.addStretch(1)
        first_details_row.addWidget(status_group, 1)

        capabilities_group = QGroupBox(_("Enhanced capabilities"))
        capabilities_layout = QVBoxLayout(capabilities_group)
        self.capabilities_label = QLabel(
            _("Capabilities will be shown after the server is contacted.")
        )
        self.capabilities_label.setWordWrap(True)
        capabilities_layout.addWidget(self.capabilities_label)
        capabilities_layout.addStretch(1)
        first_details_row.addWidget(capabilities_group, 1)

        layout.addLayout(first_details_row)

        second_details_row = QHBoxLayout()

        linked_group = QGroupBox(_("Connected linked services"))
        linked_layout = QVBoxLayout(linked_group)
        self.linked_services_label = QLabel(
            _("Connect to a server to load linked services.")
        )
        self.linked_services_label.setWordWrap(True)
        linked_layout.addWidget(self.linked_services_label)
        linked_layout.addStretch(1)
        second_details_row.addWidget(linked_group, 1)

        relay_group = QGroupBox(_("Connected KOSync relay servers"))
        relay_layout = QVBoxLayout(relay_group)
        self.relay_servers_label = QLabel(
            _("Connect to a server to load relay servers.")
        )
        self.relay_servers_label.setWordWrap(True)
        relay_layout.addWidget(self.relay_servers_label)
        relay_layout.addStretch(1)
        second_details_row.addWidget(relay_group, 1)

        layout.addLayout(second_details_row)
        layout.addStretch(1)
        self._load_saved_state()

    def summary_text(self) -> str:
        return self._summary_text

    def _notify_status_changed(self) -> None:
        if self.on_status_changed is not None:
            self.on_status_changed()

    def _notify_layout_changed(self) -> None:
        if self.on_layout_changed is not None:
            self.on_layout_changed()

    def schedule_auto_test(self, delay_ms: int = 1000) -> None:
        """Auto-test the saved connection shortly after this page is opened."""

        self._auto_test_token += 1
        generation = self._auto_test_token
        QTimer.singleShot(
            max(0, int(delay_ms)),
            lambda: self._run_scheduled_test(generation),
        )

    def _run_scheduled_test(self, generation: int) -> None:
        if generation != self._auto_test_token or self._busy:
            return
        if get_active_server_profile():
            self.test_connection()

    def _current_library(self) -> tuple[str | None, str | None]:
        gui = getattr(self.action, "gui", None)
        db = getattr(gui, "current_db", None)
        if db is None:
            return None, None

        library_uuid = str(getattr(db, "library_id", "") or "").strip()
        if not library_uuid:
            return None, None

        raw_path = str(getattr(db, "library_path", "") or "").strip()
        library_name = Path(raw_path).name if raw_path else None
        return library_uuid, library_name or None

    def _plugin_version(self) -> str:
        base_plugin = getattr(self.action, "interface_action_base_plugin", None)
        value = getattr(base_plugin, "version_string", None)
        return str(value or "unknown")

    def _selected_auth_mode(self) -> str:
        return str(self.auth_mode.currentData() or "pairing")

    def _select_auth_mode(self, mode: str) -> None:
        index = self.auth_mode.findData(mode)
        self.auth_mode.setCurrentIndex(index if index >= 0 else 0)

    def _auth_mode_changed(self) -> None:
        pairing = self._selected_auth_mode() == "pairing"
        self.registration_code.setEnabled(pairing and not self._busy)
        self.username.setEnabled(not pairing and not self._busy)
        self.password.setEnabled(not pairing and not self._busy)

    def _update_recovery_button(self) -> None:
        profile = get_active_server_profile()
        pairing = isinstance(profile, dict) and str(profile.get("auth_mode") or "") == "pairing"
        self.recover_button.setEnabled(
            not self._busy and pairing and bool(self.library_uuid)
        )

    def _load_saved_state(self) -> None:
        profile = get_active_server_profile()
        if not profile:
            self._render_not_connected(_("Choose an authentication method and connect."))
            self._update_recovery_button()
            self._auth_mode_changed()
            return

        saved_url = profile.get("server_url")
        if isinstance(saved_url, str) and saved_url.strip():
            self.server_url.setText(saved_url.strip())

        mode = str(profile.get("auth_mode") or "pairing")
        self._select_auth_mode(mode)
        if mode == "kosync":
            self.username.setText(str(profile.get("username") or ""))

        self._render_saved_registration(
            profile,
            _("Saved server profile loaded. Use Test Connection to refresh details."),
            summary=_("Configured · connection not tested this session"),
        )
        self._render_cached_capabilities(profile)
        self._update_recovery_button()
        self._auth_mode_changed()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.server_url.setEnabled(not busy)
        self.auth_mode.setEnabled(not busy)
        self.test_button.setEnabled(not busy)
        self.connect_button.setEnabled(not busy)
        self._update_recovery_button()
        self.disconnect_button.setEnabled(
            not busy and bool(get_active_server_profile())
        )
        self._auth_mode_changed()

    def _render_not_connected(self, detail: str | None = None) -> None:
        detail = detail or _("Authentication required.")
        self.status_label.setText(
            _detail_table(
                [
                    (_("Server Status"), _("Not Connected")),
                    (_("Details"), detail),
                ]
            )
        )
        self.capabilities_label.setText(
            _("Capabilities will be shown after the server is contacted.")
        )
        self.linked_services_label.setText(_("Not available while disconnected."))
        self.relay_servers_label.setText(_("Not available while disconnected."))
        self.disconnect_button.setEnabled(False)
        self.connect_button.setEnabled(not self._busy)
        self._summary_text = _("Not connected")
        self._notify_status_changed()

    def _server_descriptor(self, response: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(response.get("server_family") or "kosync"),
            str(response.get("server_type") or "standard"),
            str(response.get("server_version") or "unknown"),
        )

    def _render_reachable_unregistered(
        self,
        response: dict[str, Any],
        detail: str,
    ) -> None:
        server_family, server_type, server_version = self._server_descriptor(response)
        self.status_label.setText(
            "\n".join(
                [
                    _("Server reachable"),
                    _("Server: {family} / {type} · {version}").format(family=server_family, type=server_type, version=server_version),
                    detail,
                ]
            )
        )
        self._render_capabilities(response)
        self.linked_services_label.setText(
            _("Authenticate to view account linked services.")
        )
        self.relay_servers_label.setText(
            _("Authenticate to view account relay servers.")
        )
        self._summary_text = (
            _("Server reachable · {family} / {type} · {version}").format(family=server_family, type=server_type, version=server_version)
        )
        self._notify_status_changed()

    def _render_saved_registration(
        self,
        registration: dict,
        detail: str | None = None,
        *,
        summary: str | None = None,
    ) -> None:
        mode = str(registration.get("auth_mode") or "pairing")
        auth_text = (
            _("Techy-Notes pairing")
            if mode == "pairing"
            else _("KOSync username ({username})").format(username=registration.get("username") or _("unknown"))
        )
        lines = [
            _("Saved connection"),
            detail or _("Checking the saved server profile…"),
            _("Server: {server}").format(server=registration.get("server_url") or _("unknown")),
            _("Authentication: {auth}").format(auth=auth_text),
        ]
        if mode == "pairing":
            lines.append(_("Client ID: {client_id}").format(client_id=registration.get("client_id") or _("unknown")))
            lines.append(_("Scope: {scope}").format(scope=registration.get("scope") or _("unknown")))
        self.status_label.setText(_detail_lines(lines))
        self.disconnect_button.setEnabled(not self._busy)
        self._summary_text = summary or _("Saved connection · validation pending")
        self._notify_status_changed()

    def _render_connected(
        self,
        registration: dict,
        identity: dict[str, Any] | None,
        capabilities_response: dict[str, Any],
    ) -> None:
        server_family, server_type, server_version = self._server_descriptor(
            capabilities_response
        )
        mode = str(registration.get("auth_mode") or "pairing")
        lines = [
            _("Server Status: Connected"),
            f"Server: {server_family} / {server_type} · {server_version}",
            _("URL: {url}").format(url=registration.get("server_url") or _("unknown")),
            (
                _("Authentication: Techy-Notes pairing")
                if mode == "pairing"
                else _("Authentication: KOSync username ({username})").format(username=registration.get("username") or _("unknown"))
            ),
            _("Calibre client: {client}").format(client=self.device_name),
        ]
        if identity and isinstance(identity.get("client"), dict):
            client = identity["client"]
            lines.append(
                f"Client ID: {client.get('id') or registration.get('client_id') or 'unknown'}"
            )
            lines.append(
                f"Scope: {client.get('scope') or registration.get('scope') or 'unknown'}"
            )

        self.status_label.setText(_detail_lines(lines))
        self._render_capabilities(capabilities_response, registration)
        self.disconnect_button.setEnabled(not self._busy)
        self._summary_text = (
            _("Connected · {family} / {type} · {version}").format(family=server_family, type=server_type, version=server_version)
        )
        self._notify_status_changed()

    def _render_cached_capabilities(self, profile: dict[str, Any]) -> None:
        capabilities = profile.get("capabilities")
        if not isinstance(capabilities, dict):
            return
        self._render_capabilities(
            {
                "server_family": profile.get("server_family"),
                "server_type": profile.get("server_type"),
                "server_version": profile.get("server_version"),
                "capabilities": capabilities,
            },
            profile,
        )

    def _render_capabilities(
        self,
        response: dict[str, Any],
        profile: dict[str, Any] | None = None,
    ) -> None:
        capabilities = response.get("capabilities")
        capabilities = capabilities if isinstance(capabilities, dict) else {}

        def available(value: Any, unknown: str | None = None) -> str:
            if value is True:
                return _("Available")
            if value is False:
                return _("Unavailable")
            return unknown or _("Unavailable")

        metadata_version = capabilities.get("document_metadata_write_version")
        if capabilities.get("document_metadata_write") is True:
            metadata_text = _("Available")
            if isinstance(metadata_version, int) and not isinstance(metadata_version, bool):
                metadata_text += f" (v{metadata_version})"
        elif profile and profile.get("metadata_compatible") is True:
            metadata_text = _("Compatible")
        elif profile and profile.get("metadata_compatible") is False:
            metadata_text = _("Unsupported")
        else:
            metadata_text = _("Not tested")

        server_family, server_type, _server_version = self._server_descriptor(response)
        enhanced = server_type == "enhanced" or server_family == "techy-notes"
        lines = [
            _("Standard KOSync progress: {state}").format(state=_("Available")),
            _(
                "Server document listing: {state}"
            ).format(
                state=available(
                    capabilities.get("document_listing"),
                    _("Not tested"),
                )
            ),
            _("Book metadata: {state}").format(state=metadata_text),
        ]
        if enhanced:
            lines.extend(
                [
                    _("Logical library: {state}").format(state=available(capabilities.get("logical_library"))),
                    _("Change journal: {state}").format(state=available(capabilities.get("change_journal"))),
                    _("Annotations: {state}").format(state=available(capabilities.get("annotations"))),
                    _("Vocabulary Builder: {state}").format(state=available(capabilities.get("vocabulary_builder"))),
                    _("Calibre pairing: {state}").format(state=available(capabilities.get("calibre_client_pairing"))),
                    _("Calibre binding backup: {state}").format(state=available(capabilities.get("calibre_bindings"))),
                ]
            )
        else:
            lines.append(_("Enhanced APIs: Not advertised by this server"))

        self.capabilities_label.setText(_detail_lines(lines))

    def _render_linked_services(self, state: dict[str, Any]) -> None:
        accounts = state.get("accounts")
        if not isinstance(accounts, list) or not accounts:
            self.linked_services_label.setText(_("None connected."))
            return

        lines = []
        for account in accounts:
            if not isinstance(account, dict):
                continue
            name = account.get("display_name") or account.get("service_id") or _("Service")
            status = _status_text(account.get("status") or "unknown")



            lines.append(f"{name}: {status}")
        self.linked_services_label.setText(
            _detail_lines(lines) if lines else _("None connected.")
        )

    def _render_relay_servers(self, state: dict[str, Any]) -> None:
        servers = state.get("servers")
        if not isinstance(servers, list) or not servers:
            self.relay_servers_label.setText(_("None connected."))
            return

        lines = []
        for server in servers:
            if not isinstance(server, dict):
                continue
            name = server.get("name") or server.get("base_url") or _("KOSync server")
            status = _status_text(server.get("status") or "unknown")
            server_type = server.get("server_type") or "standard"
            mode = server.get("effective_mode") or server.get("detected_mode") or "standard"
            lines.append(f"{name}: {status} · {server_type} / {mode}")
        self.relay_servers_label.setText(
            _detail_lines(lines) if lines else _("None connected.")
        )

    def _refresh_related_connections(
        self,
        api: DeluxeSyncApi,
        profile: dict[str, Any],
    ) -> None:
        try:
            linked = api.get_linked_services(profile)
        except ApiError as error:
            self.linked_services_label.setText(
                _("Not available on this server.")
                if error.status in {403, 404, 405}
                else _("Could not load: {error}").format(error=error)
            )
        else:
            self._render_linked_services(linked)

        try:
            relays = api.get_relay_servers(profile)
        except ApiError as error:
            self.relay_servers_label.setText(
                _("Not available on this server.")
                if error.status in {403, 404, 405}
                else _("Could not load: {error}").format(error=error)
            )
        else:
            self._render_relay_servers(relays)

    def _api(self, server_url: str | None = None) -> DeluxeSyncApi:
        return DeluxeSyncApi(
            server_url or self.server_url.text(),
            plugin_version=self.plugin_version,
        )

    def _same_server(self, first: str, second: str) -> bool:
        return first.strip().rstrip("/").lower() == second.strip().rstrip("/").lower()

    def _profile_with_server_details(
        self,
        profile: dict[str, Any],
        response: dict[str, Any],
    ) -> dict[str, Any]:
        server_family, server_type, server_version = self._server_descriptor(response)
        result = dict(profile)
        result["server_family"] = server_family
        result["server_type"] = server_type
        result["server_version"] = server_version
        capabilities = response.get("capabilities")
        result["capabilities"] = (
            dict(capabilities) if isinstance(capabilities, dict) else {}
        )
        return result

    def _probe_document_listing(
        self,
        api: DeluxeSyncApi,
        profile: dict[str, Any],
    ) -> None:
        try:
            api.get_raw_documents(profile)
        except ApiError as error:
            profile["capabilities"]["document_listing"] = (
                False if error.status in {404, 405} else None
            )
        else:
            profile["capabilities"]["document_listing"] = True

    def connect_registration(self) -> None:
        """Connect using Techy-Notes pairing or standard KOSync authentication."""

        if self._busy:
            return

        requested_url = self.server_url.text().strip()
        auth_mode = self._selected_auth_mode()
        LOGGER.info(
            "Connect requested server=%s auth_mode=%s client_uuid=%s",
            requested_url,
            auth_mode,
            REDACTED,
        )
        self._set_busy(True)
        self.status_label.setText(_("Connecting…"))
        self.capabilities_label.setText(_("Checking server capabilities…"))
        self.linked_services_label.setText(_("Loading after authentication…"))
        self.relay_servers_label.setText(_("Loading after authentication…"))
        self._summary_text = "Connecting…"
        self._notify_status_changed()

        try:
            api = self._api()

            if auth_mode == "pairing":
                capabilities = api.discover_capabilities()
                if not api.pairing_supported(capabilities):
                    raise ApiError(
                        "This server does not advertise Calibre pairing. "
                        "Choose KOSync username / password instead."
                    )
                pairing = api.redeem_registration_code(
                    self.registration_code.text(),
                    client_uuid=get_or_create_client_uuid(),
                    device_name=self.device_name,
                    calibre_version=CALIBRE_VERSION,
                )
                profile = api.pairing_profile(pairing)
                self.registration_code.clear()
            else:
                profile = api.authenticate_kosync_profile(
                    self.username.text(),
                    self.password.text(),
                    get_active_server_profile(),
                )
                capabilities = api.discover_capabilities(profile)
                self.password.clear()

            set_server_url(api.server_url)
            profile["name"] = "Primary Server"
            profile = self._profile_with_server_details(profile, capabilities)
            save_server_profile(profile)
            identity = api.validate_profile(profile)
            self._probe_document_listing(api, profile)
            capabilities["capabilities"] = profile["capabilities"]
            save_server_profile(profile)

        except ApiError as error:
            LOGGER.warning(
                "Connect failed server=%s auth_mode=%s status=%s error_type=%s",
                requested_url,
                auth_mode,
                error.status,
                type(error).__name__,
            )
            self._render_not_connected(str(error))
        else:
            self._render_connected(profile, identity, capabilities)
            self._refresh_related_connections(api, profile)
        finally:
            self._set_busy(False)
            self._notify_layout_changed()

    def test_connection(self) -> None:
        """Refresh server, authentication, capabilities, and connection summaries."""

        self._auto_test_token += 1

        if self._busy:
            return

        requested_url = self.server_url.text().strip()
        profile = get_active_server_profile()
        LOGGER.info("Test Connection requested server=%s", requested_url)
        self._set_busy(True)
        self.status_label.setText(_("Testing server connection…"))
        self.capabilities_label.setText(_("Refreshing server capabilities…"))
        self.linked_services_label.setText(_("Refreshing…"))
        self.relay_servers_label.setText(_("Refreshing…"))
        self._summary_text = "Testing connection…"
        self._notify_status_changed()

        try:
            api = self._api()
            if not profile:
                capabilities = api.discover_capabilities()
                self._render_reachable_unregistered(
                    capabilities,
                    "Authentication required.",
                )
                return

            saved_url = str(profile.get("server_url") or "")
            if not self._same_server(saved_url, api.server_url):
                capabilities = api.discover_capabilities()
                self._render_reachable_unregistered(
                    capabilities,
                    "This URL differs from the saved server profile. "
                    "Use Connect to authenticate this server.",
                )
                return

            identity = api.validate_profile(profile)
            capabilities = api.discover_capabilities(profile)
            profile = self._profile_with_server_details(profile, capabilities)
            self._probe_document_listing(api, profile)
            capabilities["capabilities"] = profile["capabilities"]
            save_server_profile(profile)
            set_server_url(api.server_url)

        except AuthorizationError as error:
            LOGGER.warning(
                "Test Connection rejected stored authentication status=%s authorization=%s",
                error.status,
                REDACTED,
            )
            self._render_saved_registration(
                profile or {},
                f"Authentication was rejected: {error}",
                summary="Authentication rejected",
            )
            self.linked_services_label.setText(_("Authentication required."))
            self.relay_servers_label.setText(_("Authentication required."))
        except ApiError as error:
            LOGGER.warning(
                "Test Connection failed server=%s status=%s error_type=%s",
                requested_url,
                error.status,
                type(error).__name__,
            )
            if profile:
                self._render_saved_registration(
                    profile,
                    f"Connection test failed: {error}",
                    summary="Configured · connection test failed",
                )
                self.capabilities_label.setText(
                    _("Capabilities could not be refreshed.")
                )
                self.linked_services_label.setText(_("Could not refresh."))
                self.relay_servers_label.setText(_("Could not refresh."))
            else:
                self._render_not_connected(str(error))
        else:
            self._render_connected(profile, identity, capabilities)
            self._refresh_related_connections(api, profile)
        finally:
            self._set_busy(False)
            self._notify_layout_changed()

    def recover_bindings(self) -> None:
        """Restore this Calibre library's durable binding map from Techy-Notes."""

        if self._busy:
            return

        self._auto_test_token += 1
        self.library_uuid, self.library_name = self._current_library()
        self.library_label.setText(
            self.library_name or _("No active Calibre library")
        )
        profile = get_active_server_profile()

        if not self.library_uuid:
            QMessageBox.information(
                self,
                _("Recover Server Bindings"),
                _("Open a Calibre library before recovering bindings."),
            )
            self._update_recovery_button()
            return

        if not profile:
            QMessageBox.information(
                self,
                _("Recover Server Bindings"),
                _("Connect this Calibre installation to Techy-Notes first."),
            )
            self._update_recovery_button()
            return

        self._set_busy(True)
        try:
            api = self._api(str(profile.get("server_url") or self.server_url.text()))
            capabilities = api.discover_capabilities(profile)
            if not server_binding_supported(profile, capabilities):
                QMessageBox.information(
                    self,
                    _("Recover Server Bindings"),
                    _(
                        "This server does not advertise Calibre binding backup for "
                        "the current connection."
                    ),
                )
                return

            profile_id = get_active_server_profile_id()
            recovery = recover_server_bindings(
                api,
                profile,
                profile_id,
                self.library_uuid,
                get_library_bindings(self.library_uuid, profile_id),
                get_binding_tombstones(self.library_uuid, profile_id),
                capabilities,
            )
            if recovery.warnings:
                raise ApiError("; ".join(recovery.warnings))

            for binding in recovery.bindings.values():
                save_binding_record(binding, profile_id)

            summary_lines = [
                _("Binding recovery complete."),
                "",
                _("Server bindings found: {count}").format(
                    count=recovery.found_count
                ),
                _("Restored locally: {count}").format(
                    count=recovery.restored_count
                ),
                _("Already present: {count}").format(
                    count=recovery.already_present_count
                ),
                _("Conflicts kept local: {count}").format(
                    count=recovery.conflict_count
                ),
            ]
            if recovery.suppressed_count:
                summary_lines.append(
                    _("Pending removals skipped: {count}").format(
                        count=recovery.suppressed_count
                    )
                )
            QMessageBox.information(
                self,
                _("Recover Server Bindings"),
                "\n".join(summary_lines),
            )
            LOGGER.info(
                "Calibre binding recovery library=%s found=%s restored=%s existing=%s conflicts=%s suppressed=%s",
                self.library_uuid,
                recovery.found_count,
                recovery.restored_count,
                recovery.already_present_count,
                recovery.conflict_count,
                recovery.suppressed_count,
            )
        except AuthorizationError as error:
            QMessageBox.warning(
                self,
                _("Recover Server Bindings"),
                _("Server authentication failed: {error}").format(error=error),
            )
        except ApiError as error:
            QMessageBox.warning(
                self,
                _("Recover Server Bindings"),
                _("Could not recover server bindings: {error}").format(error=error),
            )
        finally:
            self._set_busy(False)
            self._notify_layout_changed()

    def validate_saved_registration(self) -> None:
        """Validate the installation-level saved server profile."""

        if get_active_server_profile() and not self._busy:
            self.test_connection()

    def forget_current_registration(self) -> None:
        """Forget this Calibre installation's active server profile."""

        if self._busy:
            return
        LOGGER.info("Local server profile forgotten authorization=%s", REDACTED)
        forget_server_profile()
        self.registration_code.clear()
        self.password.clear()
        self._render_not_connected(
            "Local authentication was forgotten. A paired Techy-Notes client can "
            "also be revoked from the server portal."
        )

    def save_settings(self) -> None:
        """Persist the server URL; registration codes are never saved."""

        value = self.server_url.text().strip()
        server_url = value or DEFAULT_SERVER_URL
        LOGGER.debug("Server URL preference saved server=%s", server_url)
        set_server_url(server_url)
