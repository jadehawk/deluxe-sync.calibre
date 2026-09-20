"""Read-only selected-book matching review dialog for Deluxe Sync Stage 3."""

from __future__ import annotations

import hashlib

from threading import Thread
from typing import Any

from qt.core import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QObject,
    QPushButton,
    QStandardItem,
    QStandardItemModel,
    QTableView,
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
from calibre_plugins.deluxe_sync.binding_sync import reconcile_server_bindings
from calibre_plugins.deluxe_sync.bindings import (
    bind_candidate,
    bind_server_document,
    clear_binding_tombstone,
    forget_binding,
    get_binding_tombstones,
    get_library_bindings,
    save_binding_record,
)
from calibre_plugins.deluxe_sync.document_registration import (
    calibre_document_id,
    registration_metadata,
)
from calibre_plugins.deluxe_sync.logger import get_logger
from calibre_plugins.deluxe_sync.matching import (
    build_remote_records,
    match_book,
    match_books,
)
from calibre_plugins.deluxe_sync.models import MatchCandidate, MatchResult, selected_calibre_books
from calibre_plugins.deluxe_sync.settings import (
    get_active_server_profile,
    get_active_server_profile_id,
)


try:
    load_translations()
except NameError:
    def _(text):
        return text

    def ngettext(singular, plural, count):
        return singular if count == 1 else plural


LOGGER = get_logger("matching-dialog")

_RESULT_HEADERS = (
    _("Calibre title"),
    _("Author(s)"),
    _("Status"),
    _("Best candidate"),
    _("Reason"),
    _("Raw document"),
    _("Logical book"),
)
_CANDIDATE_HEADERS = (
    _("Reason"),
    _("Server title"),
    _("Author(s)"),
    _("ISBN"),
    _("ASIN"),
    _("Filename"),
    _("Raw document"),
    _("Logical book"),
)

_STATE_LABELS = {
    "already_enrolled": _("Linked"),
    "matched": _("Automatic candidate"),
    "needs_review": _("Needs review"),
    "no_match": _("No server match"),
    "binding_missing": _("Bound record unavailable"),
}
_REASON_LABELS = {
    "Existing binding": _("Existing binding"),
    "Exact ASIN": _("Exact ASIN"),
    "Exact ASIN (ambiguous)": _("Exact ASIN (ambiguous)"),
    "Exact ISBN": _("Exact ISBN"),
    "Exact ISBN (ambiguous)": _("Exact ISBN (ambiguous)"),
    "Title + author + series": _("Title + author + series"),
    "Title + author": _("Title + author"),
    "Title + author (ambiguous)": _("Title + author (ambiguous)"),
    "Filename hint — review required": _("Filename hint — review required"),
}


def _reason_text(reason: str) -> str:
    return _REASON_LABELS.get(reason, reason)


class _AsyncBridge(QObject):
    completed = pyqtSignal(object)


class MatchBooksDialog(QDialog):
    """Match only the books currently selected in Calibre."""

    def __init__(
        self,
        plugin_action,
        *,
        parent_dialog: QDialog | None = None,
        auto_refresh: bool = True,
    ):
        super().__init__(parent_dialog or plugin_action.gui)
        self.action = plugin_action
        self._generation = 0
        self._loading = False
        self._creating = False
        self._results: list[MatchResult] = []
        self._remote_records = []
        self._profile_id = get_active_server_profile_id()
        self._library_uuid = ""
        self._capabilities_response: dict[str, Any] = {}
        self._registration_supported = False
        self._browsing_supported = False
        self._bridge = _AsyncBridge(self)
        self._bridge.completed.connect(self._matches_loaded)
        self._create_bridge = _AsyncBridge(self)
        self._create_bridge.completed.connect(self._creation_completed)

        self.setWindowTitle(_("Deluxe Sync — Match Selected Books"))
        layout = QVBoxLayout(self)

        intro = QLabel(
            _("Only the books currently selected in Calibre are inspected. Matching does not "
              "change Calibre book metadata or reading progress. Safe book creation is offered "
              "only when the connected server explicitly supports document registration without "
              "inventing reading progress. Approved bindings may be backed up to compatible "
              "Techy-Notes servers for recovery.")
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.workflow_label = QLabel(_("Server matching capability: Checking…"))
        self.workflow_label.setWordWrap(True)
        layout.addWidget(self.workflow_label)

        controls = QHBoxLayout()
        self.refresh_button = QPushButton(_("Refresh Matches"))
        self.refresh_button.clicked.connect(self.refresh_matches)
        controls.addWidget(self.refresh_button)

        self.server_library_button = QPushButton(_("Open Server Library"))
        self.server_library_button.setEnabled(False)
        self.server_library_button.clicked.connect(self.action.show_library_dialog)
        controls.addWidget(self.server_library_button)

        controls.addStretch(1)
        layout.addLayout(controls)

        self.status_label = QLabel(_("Ready."))
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.results_model = QStandardItemModel(0, len(_RESULT_HEADERS), self)
        self.results_model.setHorizontalHeaderLabels(_RESULT_HEADERS)
        self.results_table = QTableView()
        self.results_table.setModel(self.results_model)
        self.results_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.results_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.results_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.results_table.setAlternatingRowColors(True)
        self.results_table.setSortingEnabled(True)
        self.results_table.horizontalHeader().setStretchLastSection(True)
        self.results_table.selectionModel().selectionChanged.connect(
            self._result_selection_changed
        )
        layout.addWidget(self.results_table, 2)

        candidate_label = QLabel(_("<b>Candidate details</b>"))
        layout.addWidget(candidate_label)

        self.candidates_model = QStandardItemModel(0, len(_CANDIDATE_HEADERS), self)
        self.candidates_model.setHorizontalHeaderLabels(_CANDIDATE_HEADERS)
        self.candidates_table = QTableView()
        self.candidates_table.setModel(self.candidates_model)
        self.candidates_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.candidates_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.candidates_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.candidates_table.setAlternatingRowColors(True)
        self.candidates_table.horizontalHeader().setStretchLastSection(True)
        self.candidates_table.selectionModel().selectionChanged.connect(
            self._candidate_selection_changed
        )
        layout.addWidget(self.candidates_table, 1)

        action_row = QHBoxLayout()
        self.bind_button = QPushButton(_("Bind Selected Candidate"))
        self.bind_button.setEnabled(False)
        self.bind_button.clicked.connect(self.bind_selected_candidate)
        action_row.addWidget(self.bind_button)

        self.create_button = QPushButton(_("Create on Server"))
        self.create_button.setEnabled(False)
        self.create_button.setVisible(False)
        self.create_button.clicked.connect(self.create_selected_on_server)
        action_row.addWidget(self.create_button)

        self.clear_button = QPushButton(_("Clear Binding"))
        self.clear_button.setEnabled(False)
        self.clear_button.clicked.connect(self.clear_selected_binding)
        action_row.addWidget(self.clear_button)

        action_row.addStretch(1)
        close_button = QPushButton(
            _("Back to Dashboard") if parent_dialog is not None else _("Close")
        )
        close_button.clicked.connect(self.accept)
        action_row.addWidget(close_button)
        layout.addLayout(action_row)

        self.resize(1380, 780)
        if auto_refresh:
            QTimer.singleShot(0, self.refresh_matches)

    def _plugin_version(self) -> str:
        base_plugin = getattr(self.action, "interface_action_base_plugin", None)
        return str(getattr(base_plugin, "version_string", None) or "unknown")

    def _selected_books(self):
        return selected_calibre_books(getattr(self.action, "gui", None))

    def refresh_matches(self) -> None:
        if self._loading:
            return

        try:
            books = self._selected_books()
        except RuntimeError as error:
            self.status_label.setText(_("Could not bind candidate: {error}").format(error=error))
            self._replace_results([])
            return

        if not books:
            self.status_label.setText(
                _("Select one or more Calibre books, then choose Refresh Matches.")
            )
            self._replace_results([])
            return

        profile = get_active_server_profile()
        if not profile:
            self.status_label.setText(
                _("Server authentication required. Open Configure Deluxe Sync → Server Connection.")
            )
            self._replace_results([])
            return

        server_url = str(profile.get("server_url") or "").strip()
        if not server_url:
            self.status_label.setText(_("Saved server profile is incomplete."))
            self._replace_results([])
            return

        self._library_uuid = books[0].library_uuid
        self._profile_id = get_active_server_profile_id()
        bindings = get_library_bindings(self._library_uuid, self._profile_id)
        tombstones = get_binding_tombstones(self._library_uuid, self._profile_id)

        self._generation += 1
        generation = self._generation
        self._loading = True
        self._registration_supported = False
        self._browsing_supported = False
        self._capabilities_response = {}
        self.refresh_button.setEnabled(False)
        self.server_library_button.setEnabled(False)
        self.bind_button.setEnabled(False)
        self.create_button.setEnabled(False)
        self.create_button.setVisible(False)
        self.clear_button.setEnabled(False)
        self.workflow_label.setText(_("Server matching capability: Checking…"))
        self.status_label.setText(
            ngettext(
                "Matching {count} selected Calibre book…",
                "Matching {count} selected Calibre books…",
                len(books),
            ).format(count=len(books))
        )

        Thread(
            target=self._load_worker,
            args=(generation, books, server_url, profile, bindings, tombstones),
            name="DeluxeSyncStage3Matching",
            daemon=True,
        ).start()

    def _load_worker(
        self,
        generation: int,
        books,
        server_url: str,
        profile: dict[str, Any],
        bindings: dict[str, dict[str, Any]],
        tombstones: set[str],
    ) -> None:
        payload = None
        error = None
        warnings: list[str] = []
        try:
            api = DeluxeSyncApi(server_url, plugin_version=self._plugin_version())
            capabilities = api.discover_capabilities(profile)
            reconciliation = reconcile_server_bindings(
                api,
                profile,
                self._profile_id,
                books[0].library_uuid,
                bindings,
                tombstones,
                capabilities,
            )
            warnings.extend(reconciliation.warnings)
            library = api.get_library(profile, capabilities_response=capabilities)
            logical_details: dict[int, dict[str, Any]] = {}

            for remote_book in library.get("books") or ():
                if not isinstance(remote_book, dict) or remote_book.get("kind") != "logical":
                    continue
                try:
                    logical_id = int(remote_book.get("logical_book_id"))
                except (TypeError, ValueError):
                    continue
                try:
                    logical_details[logical_id] = api.get_logical_book(
                        logical_id, profile
                    )
                except ApiError as detail_error:
                    warnings.append(
                        _("Linked book {logical_id} details were unavailable ({error}).").format(logical_id=logical_id, error=detail_error)
                    )

            remote_records = build_remote_records(library, logical_details)
            results = match_books(books, remote_records, reconciliation.bindings)
            payload = (results, remote_records, warnings, reconciliation, capabilities)
        except Exception as caught:
            error = caught

        try:
            self._bridge.completed.emit((generation, payload, error))
        except RuntimeError:
            return

    def _matches_loaded(self, result: object) -> None:
        generation, payload, error = result
        if generation != self._generation:
            return

        self._loading = False
        self.refresh_button.setEnabled(True)

        if error is not None:
            if isinstance(error, AuthorizationError):
                self.status_label.setText(_("Server authentication failed: {error}").format(error=error))
                self.workflow_label.setText(_("Server matching capability unavailable."))
            elif isinstance(error, CapabilityError):
                self.status_label.setText(
                    _("This server supports progress sync but does not expose a browseable book list.")
                )
                self.workflow_label.setText(
                    _("Server book matching is unavailable. Continue using reading-progress sync; "
                      "Calibre cannot browse or link server books on this server.")
                )
            elif isinstance(error, ApiError):
                self.status_label.setText(_("Could not load server library: {error}").format(error=error))
                self.workflow_label.setText(_("Server matching capability unavailable."))
            else:
                self.status_label.setText(_("Could not match selected books."))
                LOGGER.exception(
                    "Stage 3 matching failed error_type=%s",
                    type(error).__name__,
                    exc_info=error,
                )
            self._replace_results([])
            return

        results, remote_records, warnings, reconciliation, capabilities = payload
        self._capabilities_response = capabilities if isinstance(capabilities, dict) else {}
        capability_map = self._capabilities_response.get("capabilities")
        self._registration_supported = (
            isinstance(capability_map, dict)
            and capability_map.get("document_registration") is True
        )
        self._browsing_supported = True
        self.server_library_button.setEnabled(True)
        self.create_button.setVisible(self._registration_supported)
        if self._registration_supported:
            self.workflow_label.setText(
                _("This server supports matching existing books and safe Calibre book creation.")
            )
        else:
            self.workflow_label.setText(
                _("This server can match existing server books but cannot safely create them from "
                  "Calibre. If a book is missing, sync it from your e-reader first, then Refresh Matches.")
            )
        for binding in reconciliation.bindings.values():
            try:
                save_binding_record(binding, self._profile_id)
            except ValueError:
                LOGGER.warning("Could not persist reconciled server-backed binding")
        for book_uuid in reconciliation.cleared_tombstones:
            clear_binding_tombstone(
                self._library_uuid,
                book_uuid,
                self._profile_id,
            )
        self._remote_records = remote_records

        for match in results:
            if match.binding_changed and isinstance(match.binding, dict):
                try:
                    match.binding = save_binding_record(
                        match.binding, self._profile_id
                    )
                    match.binding_changed = False
                except ValueError:
                    LOGGER.warning(
                        "Could not reconcile binding book_uuid=%s",
                        match.local.book_uuid,
                    )

        self._replace_results(results)

        counts: dict[str, int] = {}
        for match in results:
            counts[match.state] = counts.get(match.state, 0) + 1

        selected_count = len(results)
        enrolled_count = counts.get("already_enrolled", 0)
        matched_count = counts.get("matched", 0)
        review_count = counts.get("needs_review", 0)
        no_match_count = counts.get("no_match", 0)
        parts = [
            ngettext("{count} selected book", "{count} selected books", selected_count).format(count=selected_count),
            ngettext("{count} linked", "{count} linked", enrolled_count).format(count=enrolled_count),
            ngettext(
                "{count} automatic candidate",
                "{count} automatic candidates",
                matched_count,
            ).format(count=matched_count),
            ngettext("{count} needs review", "{count} need review", review_count).format(count=review_count),
            ngettext("{count} no match", "{count} no matches", no_match_count).format(count=no_match_count),
        ]
        if counts.get("binding_missing"):
            missing_count = counts["binding_missing"]
            parts.append(
                ngettext(
                    "{count} bound record unavailable",
                    "{count} bound records unavailable",
                    missing_count,
                ).format(count=missing_count)
            )
        summary = " · ".join(parts)
        if warnings:
            summary += " · " + ngettext(
                "{count} warning",
                "{count} warnings",
                len(warnings),
            ).format(count=len(warnings))
        self.status_label.setText(summary)

        LOGGER.info(
            "Stage 3 match review loaded selected=%s enrolled=%s matched=%s review=%s no_match=%s",
            len(results),
            counts.get("already_enrolled", 0),
            counts.get("matched", 0),
            counts.get("needs_review", 0),
            counts.get("no_match", 0),
        )

    def _replace_results(self, results: list[MatchResult]) -> None:
        self._results = list(results)
        self.results_table.setSortingEnabled(False)
        self.results_model.removeRows(0, self.results_model.rowCount())

        for match in self._results:
            candidate = match.primary_candidate
            remote = candidate.remote if candidate is not None else None
            values = [
                match.local.title or _("Unknown book"),
                match.local.authors_text,
                _STATE_LABELS.get(match.state, match.state),
                remote.display_title if remote is not None else "",
                _reason_text(candidate.reason) if candidate is not None else "",
                remote.document if remote is not None else str(
                    (match.binding or {}).get("server_document") or ""
                ),
                str(remote.logical_book_id or "") if remote is not None else str(
                    (match.binding or {}).get("logical_book_id") or ""
                ),
            ]
            row = [QStandardItem(str(value or "")) for value in values]
            row[0].setData(match, Qt.ItemDataRole.UserRole)
            self.results_model.appendRow(row)

        self.results_table.setSortingEnabled(True)
        self.results_table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.candidates_model.removeRows(0, self.candidates_model.rowCount())
        self.bind_button.setEnabled(False)
        self.create_button.setEnabled(False)
        self.clear_button.setEnabled(False)

        if self.results_model.rowCount():
            self.results_table.setCurrentIndex(self.results_model.index(0, 0))

    def _selected_result(self) -> MatchResult | None:
        index = self.results_table.currentIndex()
        if not index.isValid():
            return None
        item = self.results_model.item(index.row(), 0)
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return value if isinstance(value, MatchResult) else None

    def _selected_candidate(self) -> MatchCandidate | None:
        index = self.candidates_table.currentIndex()
        if not index.isValid():
            return None
        item = self.candidates_model.item(index.row(), 0)
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return value if isinstance(value, MatchCandidate) else None

    def _result_selection_changed(self, *_args) -> None:
        match = self._selected_result()
        self.candidates_model.removeRows(0, self.candidates_model.rowCount())

        if match is None:
            self.bind_button.setEnabled(False)
            self.create_button.setEnabled(False)
            self.clear_button.setEnabled(False)
            return

        for candidate in match.candidates:
            remote = candidate.remote
            values = [
                _reason_text(candidate.reason),
                remote.display_title,
                remote.display_authors,
                remote.isbn,
                remote.asin,
                remote.filename,
                remote.document,
                str(remote.logical_book_id or ""),
            ]
            row = [QStandardItem(str(value or "")) for value in values]
            row[0].setData(candidate, Qt.ItemDataRole.UserRole)
            self.candidates_model.appendRow(row)

        self.create_button.setEnabled(
            match.state == "no_match"
            and self._registration_supported
            and not self._loading
            and not self._creating
        )
        self.clear_button.setEnabled(isinstance(match.binding, dict))
        if self.candidates_model.rowCount():
            self.candidates_table.setCurrentIndex(
                self.candidates_model.index(0, 0)
            )
        else:
            self.bind_button.setEnabled(False)

    def _candidate_selection_changed(self, *_args) -> None:
        match = self._selected_result()
        candidate = self._selected_candidate()
        if match is None or candidate is None or not candidate.remote.document:
            self.bind_button.setEnabled(False)
            return

        bound_document = str((match.binding or {}).get("server_document") or "")
        self.bind_button.setEnabled(candidate.remote.document != bound_document)

    def create_selected_on_server(self) -> None:
        if self._loading or self._creating:
            return

        match = self._selected_result()
        if match is None or match.state != "no_match":
            self.status_label.setText(
                _("Create on Server is only available for a book with no server match.")
            )
            return
        if not self._registration_supported:
            self.status_label.setText(
                _("This server does not support Calibre document registration.")
            )
            return

        if get_active_server_profile_id() != self._profile_id:
            self.status_label.setText(
                _("The active server profile changed. Refresh matches before creating the book.")
            )
            return
        profile = get_active_server_profile()
        if not profile:
            self.status_label.setText(
                _("Server authentication required. Open Configure Deluxe Sync → Server Connection.")
            )
            return
        server_url = str(profile.get("server_url") or "").strip()
        if not server_url:
            self.status_label.setText(_("Saved server profile is incomplete."))
            return

        try:
            document = calibre_document_id(match.local)
            metadata = registration_metadata(match.local)
        except (TypeError, ValueError) as error:
            self.status_label.setText(str(error))
            return

        gui = getattr(self.action, "gui", None)
        db = getattr(gui, "current_db", None)
        current_library_uuid = str(getattr(db, "library_id", "") or "").strip()
        if db is None or current_library_uuid != match.local.library_uuid:
            self.status_label.setText(
                _("Return to the Calibre library used for matching before creating the book.")
            )
            return

        cover_bytes = None
        calibre_api = getattr(db, "new_api", None)
        if calibre_api is not None:
            try:
                raw_cover = calibre_api.cover(match.local.book_id)
                cover_bytes = bytes(raw_cover) if raw_cover else None
            except Exception:
                cover_bytes = None
        if match.local.cover_hash and not cover_bytes:
            self.status_label.setText(
                _("The Calibre cover could not be read, so the server book was not created.")
            )
            return

        self._creating = True
        self.refresh_button.setEnabled(False)
        self.bind_button.setEnabled(False)
        self.create_button.setEnabled(False)
        self.clear_button.setEnabled(False)
        if cover_bytes:
            create_status = _(
                "Creating “{title}” on the server and copying its Calibre cover without reading progress…"
            )
        else:
            create_status = _(
                "Creating “{title}” on the server without reading progress…"
            )
        self.status_label.setText(
            create_status.format(title=match.local.title or _("Unknown book"))
        )

        generation = self._generation
        Thread(
            target=self._create_worker,
            args=(
                generation,
                match.local,
                document,
                metadata,
                cover_bytes,
                server_url,
                profile,
            ),
            name="DeluxeSyncStage6BRegistration",
            daemon=True,
        ).start()

    def _create_worker(
        self,
        generation: int,
        local,
        document: str,
        metadata: dict[str, object],
        cover_bytes: bytes | None,
        server_url: str,
        profile: dict[str, Any],
    ) -> None:
        payload = None
        error = None
        try:
            api = DeluxeSyncApi(server_url, plugin_version=self._plugin_version())
            capabilities = api.discover_capabilities(profile)
            capability_map = capabilities.get("capabilities")
            if (
                not isinstance(capability_map, dict)
                or capability_map.get("document_registration") is not True
            ):
                raise ApiError(
                    "This server does not support Calibre document registration."
                )
            registration = api.register_document(
                document,
                metadata,
                profile,
                capabilities_response=capabilities,
            )
            verified = api.get_document_metadata(
                document,
                profile,
                capabilities_response=capabilities,
            )
            if str(verified.get("document") or "").strip() != document:
                raise ApiError("Server document verification returned the wrong document.")
            if not isinstance(verified.get("metadata"), dict):
                raise ApiError("Server document verification returned invalid metadata.")

            cover_uploaded = False
            if cover_bytes:
                if capability_map.get("document_cover_sync") is True:
                    uploaded = api.upload_document_cover(
                        document,
                        cover_bytes,
                        profile,
                        capabilities_response=capabilities,
                    )
                    expected_cover_hash = hashlib.sha256(cover_bytes).hexdigest()
                    returned_cover_hash = str(uploaded.get("cover_hash") or "").strip().lower()
                    if returned_cover_hash != expected_cover_hash:
                        raise ApiError("Server cover verification returned the wrong cover.")
                    cover_uploaded = True
                else:
                    LOGGER.warning(
                        "Stage 6B server does not advertise document cover sync document=%s",
                        document,
                    )

            payload = (local, document, registration, verified, cover_uploaded)
        except Exception as caught:
            error = caught

        try:
            self._create_bridge.completed.emit((generation, payload, error))
        except RuntimeError:
            return

    def _creation_completed(self, result: object) -> None:
        generation, payload, error = result
        if generation != self._generation:
            return

        self._creating = False
        self.refresh_button.setEnabled(True)

        if error is not None:
            if isinstance(error, AuthorizationError):
                self.status_label.setText(
                    _("Server authentication failed: {error}").format(error=error)
                )
            elif isinstance(error, ApiError):
                self.status_label.setText(
                    _("Could not create the server book: {error}").format(error=error)
                )
            else:
                self.status_label.setText(_("Could not create the server book."))
                LOGGER.exception(
                    "Stage 6B registration failed error_type=%s",
                    type(error).__name__,
                    exc_info=error,
                )
            self._result_selection_changed()
            return

        local, document, registration, _verified, _cover_uploaded = payload
        try:
            binding = bind_server_document(local, document, self._profile_id)
        except ValueError as error:
            self.status_label.setText(
                _("The server book was created, but its local binding could not be saved: {error}").format(
                    error=error
                )
            )
            self._result_selection_changed()
            return

        for match in self._results:
            if match.local.book_uuid == local.book_uuid:
                match.binding = binding
                match.state = "already_enrolled"
                break

        if bool(registration.get("created")):
            self.status_label.setText(
                _("Created “{title}” on the server and linked it to this Calibre book. No reading progress was created.").format(
                    title=local.title or _("Unknown book")
                )
            )
        else:
            self.status_label.setText(
                _("The server record already existed. “{title}” is now linked to it; no reading progress was created.").format(
                    title=local.title or _("Unknown book")
                )
            )

        self._replace_results(self._results)
        QTimer.singleShot(0, self.refresh_matches)

    def bind_selected_candidate(self) -> None:
        match = self._selected_result()
        candidate = self._selected_candidate()
        if match is None or candidate is None:
            return

        try:
            binding = bind_candidate(match.local, candidate, self._profile_id)
        except ValueError as error:
            self.status_label.setText(str(error))
            return

        match.binding = binding
        match.state = "already_enrolled"
        match.candidates = [
            MatchCandidate(
                remote=candidate.remote,
                reason="Existing binding",
                tier=0,
                automatic=True,
            )
        ]
        self.status_label.setText(
            _("Bound “{title}”. The binding is saved locally and will be backed up to "
              "compatible Techy-Notes servers; no book metadata or reading progress was changed.").format(
                title=match.local.title
            )
        )
        self._replace_results(self._results)
        QTimer.singleShot(0, self.refresh_matches)

    def clear_selected_binding(self) -> None:
        match = self._selected_result()
        if match is None or not isinstance(match.binding, dict):
            return

        forget_binding(
            match.local.library_uuid,
            match.local.book_uuid,
            self._profile_id,
            tombstone=True,
        )
        replacement = match_book(match.local, self._remote_records, None)
        index = self._results.index(match)
        self._results[index] = replacement
        self.status_label.setText(
            _("Cleared the binding for “{title}”. Compatible server backup deletion will "
              "be retried until it is confirmed.").format(title=match.local.title)
        )
        self._replace_results(self._results)
        QTimer.singleShot(0, self.refresh_matches)
