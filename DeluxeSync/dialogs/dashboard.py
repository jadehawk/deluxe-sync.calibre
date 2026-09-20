"""Stage 10 compact dashboard and safe batch-sync entry points."""

from __future__ import annotations

from dataclasses import dataclass
from os.path import basename
from threading import Thread
from typing import Any

from qt.core import (
    QComboBox,
    QDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QObject,
    QPushButton,
    QTimer,
    Qt,
    QVBoxLayout,
    pyqtSignal,
)

from calibre_plugins.deluxe_sync.api import (
    ApiError,
    AuthorizationError,
    CapabilityError,
    DeluxeSyncApi,
)
from calibre_plugins.deluxe_sync.binding_sync import recover_server_bindings
from calibre_plugins.deluxe_sync.bindings import (
    get_binding_tombstones,
    get_library_bindings,
)
from calibre_plugins.deluxe_sync.dialogs.match_books import MatchBooksDialog
from calibre_plugins.deluxe_sync.dialogs.sync_books import SyncSelectedBooksDialog
from calibre_plugins.deluxe_sync.dialogs.sync_preview import SyncPreviewDialog
from calibre_plugins.deluxe_sync.logger import get_logger
from calibre_plugins.deluxe_sync.models import (
    CalibreBook,
    library_book_count,
    linked_calibre_books,
    selected_calibre_books,
)
from calibre_plugins.deluxe_sync.settings import (
    get_active_server_profile,
    get_active_server_profile_id,
)


try:
    load_translations()
except NameError:
    def _(text):
        return text


LOGGER = get_logger("dashboard")


@dataclass(frozen=True)
class _ServerStatus:
    capabilities: dict[str, Any] | None = None
    server_profile: str = ""
    server_book_count: int | None = None
    browsing_supported: bool | None = None
    registration_supported: bool = False
    conflict_count: int | None = None
    remote_bindings_supported: bool = False
    error: Exception | None = None


class _AsyncBridge(QObject):
    completed = pyqtSignal(object)


class DashboardDialog(QDialog):
    """Compact Stage 10 status and safe sync dashboard."""

    SCOPE_SELECTED = "selected"
    SCOPE_LINKED = "linked"

    def __init__(self, plugin_action, *, auto_refresh: bool = True):
        super().__init__(plugin_action.gui)
        self.action = plugin_action
        self._generation = 0
        self._loading = False
        self._library_uuid = ""
        self._profile_id = ""
        self._bindings: dict[str, dict[str, Any]] = {}
        self._linked_books: list[CalibreBook] = []
        self._total_books = 0
        self._server_browsing_supported: bool | None = None
        self._server_registration_supported = False
        self._bridge = _AsyncBridge(self)
        self._bridge.completed.connect(self._server_status_loaded)

        self.setWindowTitle(_("Deluxe Sync — Dashboard"))
        layout = QVBoxLayout(self)

        server_group = QGroupBox(_("Server"))
        server_layout = QGridLayout(server_group)
        server_layout.setHorizontalSpacing(18)
        server_layout.setVerticalSpacing(8)
        server_layout.setColumnStretch(1, 1)
        server_layout.setColumnStretch(3, 1)

        server_labels = (
            (_("Status:"), 0, 0),
            (_("Server:"), 0, 2),
            (_("Server Profile:"), 1, 0),
            (_("Books on Server:"), 1, 2),
        )
        label_width = max(
            self.fontMetrics().horizontalAdvance(text)
            for text, _row, _column in server_labels
        ) + 8
        for text, row, column in server_labels:
            label = QLabel(text)
            label.setMinimumWidth(label_width)
            label.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            server_layout.addWidget(label, row, column)

        self.server_status_label = QLabel(_("Not checked"))
        self.server_status_label.setWordWrap(True)
        server_layout.addWidget(self.server_status_label, 0, 1)

        self.server_url_label = QLabel(_("Not configured"))
        self.server_url_label.setWordWrap(True)
        server_layout.addWidget(self.server_url_label, 0, 3)

        self.server_profile_label = QLabel(_("Not checked"))
        self.server_profile_label.setWordWrap(True)
        server_layout.addWidget(self.server_profile_label, 1, 1)

        self.server_book_count_label = QLabel(_("Not checked"))
        self.server_book_count_label.setWordWrap(True)
        server_layout.addWidget(self.server_book_count_label, 1, 3)

        layout.addWidget(server_group)

        library_group = QGroupBox(_("Calibre library"))
        library_layout = QGridLayout(library_group)
        library_layout.addWidget(QLabel(_("Library:")), 0, 0)
        self.library_label = QLabel(_("No library open"))
        self.library_label.setWordWrap(True)
        library_layout.addWidget(self.library_label, 0, 1, 1, 5)

        library_layout.addWidget(QLabel(_("Books:")), 1, 0)
        self.total_count_label = QLabel("0")
        library_layout.addWidget(self.total_count_label, 1, 1)
        library_layout.addWidget(QLabel(_("Linked:")), 1, 2)
        self.linked_count_label = QLabel("0")
        library_layout.addWidget(self.linked_count_label, 1, 3)
        library_layout.addWidget(QLabel(_("Not linked:")), 1, 4)
        self.unlinked_count_label = QLabel("0")
        library_layout.addWidget(self.unlinked_count_label, 1, 5)

        library_layout.addWidget(QLabel(_("Binding conflicts:")), 2, 0)
        self.conflict_count_label = QLabel(_("Not checked"))
        self.conflict_count_label.setWordWrap(True)
        library_layout.addWidget(self.conflict_count_label, 2, 1, 1, 5)
        layout.addWidget(library_group)

        sync_group = QGroupBox(_("Sync"))
        sync_layout = QGridLayout(sync_group)
        sync_layout.addWidget(QLabel(_("Scope:")), 0, 0)
        self.scope_combo = QComboBox()
        self.scope_combo.addItem(_("Selected books"), self.SCOPE_SELECTED)
        self.scope_combo.addItem(
            _("All linked books in this library"),
            self.SCOPE_LINKED,
        )
        self.scope_combo.currentIndexChanged.connect(self._scope_changed)
        sync_layout.addWidget(self.scope_combo, 0, 1, 1, 3)

        self.scope_summary_label = QLabel("")
        self.scope_summary_label.setWordWrap(True)
        sync_layout.addWidget(self.scope_summary_label, 1, 0, 1, 4)

        self.review_button = QPushButton(_("Review Changes"))
        self.review_button.clicked.connect(self.review_changes)
        sync_layout.addWidget(self.review_button, 2, 0)

        self.sync_button = QPushButton(_("Sync Now"))
        self.sync_button.setToolTip(
            _(
                "Sync reading data for the selected scope, including progress and mapped "
                "reading information plus annotations or vocabulary where supported. "
                "This does not write title, author, series, identifier, or cover metadata; "
                "use Review Changes for server-supported metadata writes."
            )
        )
        self.sync_button.clicked.connect(self.sync_now)
        sync_layout.addWidget(self.sync_button, 2, 1)

        self.match_button = QPushButton(_("Match Server Books"))
        self.match_button.setToolTip(
            _("Check server capabilities to determine whether books can be matched or safely created.")
        )
        self.match_button.clicked.connect(self.match_server_books)
        sync_layout.addWidget(self.match_button, 2, 2)

        self.configure_button = QPushButton(_("Configure"))
        self.configure_button.clicked.connect(self.configure)
        sync_layout.addWidget(self.configure_button, 2, 3)

        self.activity_label = QLabel(_("Ready."))
        self.activity_label.setWordWrap(True)
        sync_layout.addWidget(self.activity_label, 3, 0, 1, 4)
        layout.addWidget(sync_group)

        controls = QHBoxLayout()
        self.refresh_button = QPushButton(_("Refresh Status"))
        self.refresh_button.clicked.connect(self.refresh)
        controls.addWidget(self.refresh_button)
        controls.addStretch(1)
        close_button = QPushButton(_("Close"))
        close_button.clicked.connect(self.accept)
        controls.addWidget(close_button)
        layout.addLayout(controls)

        self.resize(690, 390)
        self._refresh_local_state()
        if auto_refresh:
            QTimer.singleShot(0, self.refresh_server_status)

    def _plugin_version(self) -> str:
        base_plugin = getattr(self.action, "interface_action_base_plugin", None)
        return str(getattr(base_plugin, "version_string", None) or "unknown")

    def _current_library(self) -> tuple[Any, str]:
        gui = getattr(self.action, "gui", None)
        db = getattr(gui, "current_db", None)
        library_uuid = str(getattr(db, "library_id", "") or "").strip()
        if db is None or not library_uuid:
            raise RuntimeError(_("Open a Calibre library to use Deluxe Sync."))
        return db, library_uuid

    @staticmethod
    def _library_name(db: Any) -> str:
        for attr in ("library_path", "library_path_for_db"):
            value = getattr(db, attr, None)
            if callable(value):
                try:
                    value = value()
                except Exception:
                    value = None
            text = str(value or "").rstrip("\\/")
            if text:
                return basename(text) or text
        return _("Active library")

    def _refresh_local_state(self) -> None:
        gui = getattr(self.action, "gui", None)
        previous_library_uuid = self._library_uuid
        previous_profile_id = self._profile_id
        previous_server_url = self.server_url_label.text()
        try:
            db, library_uuid = self._current_library()
            profile_id = get_active_server_profile_id()
            bindings = get_library_bindings(library_uuid, profile_id)
            linked_books = linked_calibre_books(gui, bindings)
            total_books = library_book_count(gui)
        except RuntimeError as error:
            self._library_uuid = ""
            self._bindings = {}
            self._linked_books = []
            self._total_books = 0
            self.library_label.setText(str(error))
            self.total_count_label.setText("0")
            self.linked_count_label.setText("0")
            self.unlinked_count_label.setText("0")
            self._scope_changed()
            return

        profile = get_active_server_profile()
        current_server_url = (
            str(profile.get("server_url") or "").strip()
            if isinstance(profile, dict)
            else ""
        )
        server_context_changed = (
            library_uuid != previous_library_uuid
            or profile_id != previous_profile_id
            or current_server_url != previous_server_url
        )

        self._library_uuid = library_uuid
        self._profile_id = profile_id
        self._bindings = bindings
        self._linked_books = linked_books
        self._total_books = total_books

        self.library_label.setText(
            _("{name} · UUID {uuid}").format(
                name=self._library_name(db),
                uuid=library_uuid,
            )
        )
        self.total_count_label.setText(str(total_books))
        self.linked_count_label.setText(str(len(linked_books)))
        self.unlinked_count_label.setText(str(max(total_books - len(linked_books), 0)))
        if server_context_changed:
            self._render_server_basics()
        self._scope_changed()

    def _render_server_basics(self) -> None:
        self._server_browsing_supported = None
        self._server_registration_supported = False
        profile = get_active_server_profile()
        if not profile:
            self.server_status_label.setText(_("Not connected"))
            self.server_url_label.setText(_("Not configured"))
            self.server_profile_label.setText(_("Not checked"))
            self.server_book_count_label.setText(_("Not available"))
            self.conflict_count_label.setText(_("Not available"))
            return

        server_url = str(profile.get("server_url") or "").strip()
        self.server_url_label.setText(server_url or _("Not configured"))
        self.server_status_label.setText(_("Not checked"))
        self.server_profile_label.setText(_("Not checked"))
        self.server_book_count_label.setText(_("Not checked"))

    def _selected_count(self) -> int:
        gui = getattr(self.action, "gui", None)
        library_view = getattr(gui, "library_view", None)
        if library_view is None:
            return 0
        try:
            return len(list(library_view.get_selected_ids(as_set=False) or ()))
        except Exception:
            return 0

    def _scope_changed(self) -> None:
        scope = self.scope_combo.currentData()
        if scope == self.SCOPE_LINKED:
            count = len(self._linked_books)
            self.scope_summary_label.setText(
                _(
                    "{count} linked books are in scope. Unlinked Calibre books are never "
                    "included in this batch."
                ).format(count=count)
            )
        else:
            count = self._selected_count()
            self.scope_summary_label.setText(
                _("{count} currently selected Calibre books are in scope.").format(
                    count=count
                )
            )

        enabled = bool(self._library_uuid) and count > 0 and not self._loading
        self.review_button.setEnabled(enabled)
        self.sync_button.setEnabled(enabled)
        self._update_match_action()

    def _update_match_action(self) -> None:
        selected_ready = (
            bool(self._library_uuid)
            and self._selected_count() > 0
            and not self._loading
        )
        if self._server_browsing_supported is False:
            self.match_button.setText(_("Server Matching Unavailable"))
            self.match_button.setToolTip(
                _(
                    "This server supports progress sync but does not expose a browseable "
                    "book list, so Calibre cannot match server books."
                )
            )
            self.match_button.setEnabled(False)
            return

        if self._server_browsing_supported is True:
            if self._server_registration_supported:
                self.match_button.setText(_("Match / Create on Server"))
                self.match_button.setToolTip(
                    _(
                        "Match selected Calibre books to existing server books, or safely "
                        "create missing books without inventing reading progress."
                    )
                )
            else:
                self.match_button.setText(_("Match to Server Book"))
                self.match_button.setToolTip(
                    _(
                        "Match selected Calibre books to existing server books. If a book "
                        "is missing, sync it from your e-reader first, then refresh matches."
                    )
                )
            self.match_button.setEnabled(selected_ready)
            return

        self.match_button.setText(_("Match Server Books"))
        self.match_button.setToolTip(
            _("Refresh server status to check whether server-book matching is available.")
        )
        self.match_button.setEnabled(selected_ready)

    def refresh(self) -> None:
        if self._loading:
            return
        self._refresh_local_state()
        self.refresh_server_status()

    def refresh_server_status(self) -> None:
        if self._loading:
            return

        profile = get_active_server_profile()
        if not profile:
            self._render_server_basics()
            self.activity_label.setText(
                _("Configure a server connection before syncing.")
            )
            self._scope_changed()
            return

        server_url = str(profile.get("server_url") or "").strip()
        if not server_url:
            self.server_status_label.setText(_("Saved server profile is incomplete."))
            self.activity_label.setText(_("Review Server Connection settings."))
            return

        if not self._library_uuid:
            self.activity_label.setText(_("Open a Calibre library before syncing."))
            return

        self._generation += 1
        generation = self._generation
        self._loading = True
        self.refresh_button.setEnabled(False)
        self.server_status_label.setText(_("Checking…"))
        self.server_profile_label.setText(_("Checking…"))
        self.server_book_count_label.setText(_("Checking…"))
        self.conflict_count_label.setText(_("Checking…"))
        self.activity_label.setText(_("Checking server and saved authentication…"))
        self._scope_changed()

        Thread(
            target=self._server_status_worker,
            args=(
                generation,
                dict(profile),
                self._profile_id,
                self._library_uuid,
                dict(self._bindings),
            ),
            daemon=True,
        ).start()

    def _server_status_worker(
        self,
        generation: int,
        profile: dict[str, Any],
        profile_id: str,
        library_uuid: str,
        bindings: dict[str, dict[str, Any]],
    ) -> None:
        status = _ServerStatus()
        try:
            api = DeluxeSyncApi(
                str(profile.get("server_url") or ""),
                plugin_version=self._plugin_version(),
            )
            api.validate_profile(profile)
            capabilities = api.discover_capabilities(profile)
            server_profile = api.detect_server_profile(profile, capabilities)

            server_book_count = None
            browsing_supported: bool | None = None
            try:
                library = api.get_library(profile, capabilities)
                browsing_supported = True
                books = library.get("books")
                try:
                    server_book_count = int(library.get("visible_count"))
                except (TypeError, ValueError):
                    if isinstance(books, list):
                        server_book_count = len(books)
            except CapabilityError:
                browsing_supported = False
                server_book_count = None
            except ApiError as error:
                LOGGER.warning(
                    "Dashboard server book count unavailable status=%s error_type=%s",
                    error.status,
                    type(error).__name__,
                )

            capability_map = capabilities.get("capabilities")
            registration_supported = (
                isinstance(capability_map, dict)
                and capability_map.get("document_registration") is True
            )
            recovery = recover_server_bindings(
                api,
                profile,
                profile_id,
                library_uuid,
                bindings,
                get_binding_tombstones(library_uuid, profile_id),
                capabilities,
            )
            status = _ServerStatus(
                capabilities=capabilities,
                server_profile=server_profile,
                server_book_count=server_book_count,
                browsing_supported=browsing_supported,
                registration_supported=registration_supported,
                conflict_count=recovery.conflict_count,
                remote_bindings_supported=recovery.remote_enabled,
            )
        except Exception as error:
            status = _ServerStatus(error=error)

        try:
            self._bridge.completed.emit((generation, status))
        except RuntimeError:
            return

    def _server_status_loaded(self, payload: object) -> None:
        generation, status = payload
        if generation != self._generation:
            return

        self._loading = False
        self.refresh_button.setEnabled(True)
        self._scope_changed()

        if status.error is not None:
            self._server_browsing_supported = None
            self._server_registration_supported = False
            self.match_button.setText(_("Match Server Books"))
            self.match_button.setToolTip(
                _("Server matching is unavailable until the server status check succeeds.")
            )
            self.match_button.setEnabled(False)
            if isinstance(status.error, AuthorizationError):
                self.server_status_label.setText(_("Authentication rejected"))
                self.activity_label.setText(
                    _("Reconnect Deluxe Sync, then refresh this dashboard.")
                )
            elif isinstance(status.error, ApiError):
                self.server_status_label.setText(_("Unavailable"))
                self.activity_label.setText(
                    _("Could not contact the server: {error}").format(
                        error=status.error
                    )
                )
            else:
                self.server_status_label.setText(_("Status check failed"))
                self.activity_label.setText(
                    _("Could not refresh server status: {error_type}").format(
                        error_type=type(status.error).__name__
                    )
                )
            self.server_profile_label.setText(_("Not available"))
            self.server_book_count_label.setText(_("Not available"))
            self.conflict_count_label.setText(_("Not available"))
            LOGGER.warning(
                "Dashboard status refresh failed error_type=%s",
                type(status.error).__name__,
            )
            return

        self._server_browsing_supported = status.browsing_supported
        self._server_registration_supported = status.registration_supported
        self._update_match_action()

        profile_labels = {
            "enhanced": _("Enhanced"),
            "bookorbit": _("BookOrbit"),
            "crosspoint": _("Crosspoint"),
            "standard": _("Standard KOSync"),
        }
        profile_name = profile_labels.get(
            status.server_profile,
            str(status.server_profile or _("Unknown")).replace("_", " ").title(),
        )
        self.server_status_label.setText(_("Connected"))
        self.server_profile_label.setText(profile_name)
        self.server_book_count_label.setText(
            str(status.server_book_count)
            if status.server_book_count is not None
            else _("Not available")
        )

        if status.remote_bindings_supported:
            conflict_count = int(status.conflict_count or 0)
            if conflict_count:
                self.conflict_count_label.setText(
                    _(
                        "{count} · local links remain authoritative; review before changing links."
                    ).format(count=conflict_count)
                )
                self.activity_label.setText(
                    _("Server connected. Binding conflicts need review.")
                )
            else:
                self.conflict_count_label.setText("0")
                self.activity_label.setText(_("Server connected. Ready to sync."))
        else:
            self.conflict_count_label.setText(
                _("Not available on this server")
            )
            self.activity_label.setText(_("Server connected. Ready to sync."))

        LOGGER.info(
            "Dashboard status refreshed server_profile=%s server_books=%s linked=%s total=%s conflicts=%s",
            status.server_profile or "unknown",
            status.server_book_count if status.server_book_count is not None else "unavailable",
            len(self._linked_books),
            self._total_books,
            status.conflict_count if status.remote_bindings_supported else "unsupported",
        )

    def _books_for_scope(self) -> list[CalibreBook]:
        if self.scope_combo.currentData() == self.SCOPE_LINKED:
            return list(self._linked_books)
        return selected_calibre_books(getattr(self.action, "gui", None))

    def review_changes(self) -> None:
        try:
            books = self._books_for_scope()
        except RuntimeError as error:
            self.activity_label.setText(str(error))
            return
        if not books:
            self.activity_label.setText(_("There are no books in the selected scope."))
            return

        dialog = SyncPreviewDialog(
            self.action,
            books=books,
            auto_refresh=True,
            parent_dialog=self,
        )
        self.action._center_dialog(dialog)
        dialog.exec()
        self.activity_label.setText(_("Review closed. No metadata changes are automatic."))
        self._refresh_local_state()

    def match_server_books(self) -> None:
        dialog = MatchBooksDialog(self.action, parent_dialog=self)
        self.action._center_dialog(dialog)
        dialog.exec()
        self.activity_label.setText(_("Matching window closed."))
        self._refresh_local_state()

    def configure(self) -> None:
        self.action.show_config()
        self._refresh_local_state()
        self._render_server_basics()
        self.refresh_server_status()

    def sync_now(self) -> None:
        try:
            books = self._books_for_scope()
        except RuntimeError as error:
            self.activity_label.setText(str(error))
            return
        if not books:
            self.activity_label.setText(_("There are no books in the selected scope."))
            return

        linked_scope = self.scope_combo.currentData() == self.SCOPE_LINKED
        if linked_scope and len(books) > 1:
            answer = QMessageBox.question(
                self,
                _("Sync Linked Books"),
                _(
                    "Sync {count} linked books? Deluxe Sync will read server state into "
                    "your mapped Calibre columns. Unlinked books are excluded, and metadata "
                    "writes still require Review Changes."
                ).format(count=len(books)),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.activity_label.setText(_("Batch sync cancelled."))
                return

        scope_label = (
            _("all linked books in this library")
            if linked_scope
            else _("selected books")
        )
        dialog = SyncSelectedBooksDialog(
            self.action,
            auto_sync=True,
            auto_show_completion=True,
            books=books,
            scope_label=scope_label,
            parent_dialog=self,
        )
        self.action._center_dialog(dialog)
        dialog.exec()
        self.activity_label.setText(_("Sync window closed."))
        self._refresh_local_state()
