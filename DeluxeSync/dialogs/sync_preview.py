"""Metadata comparison and metadata-only server sync dialog."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from threading import Thread
from typing import Any

from qt.core import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QObject,
    QPushButton,
    QStandardItem,
    QStandardItemModel,
    QTableView,
    QTimer,
    QPalette,
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
from calibre_plugins.deluxe_sync.bindings import get_library_bindings
from calibre_plugins.deluxe_sync.metadata_preview import (
    ACTION_CALIBRE_TO_SERVER,
    ACTION_DO_NOT_SYNC,
    ACTION_KEEP_CALIBRE,
    ACTION_KEEP_SERVER,
    ACTION_SAME,
    ACTION_SERVER_TO_CALIBRE,
    POLICY_CALIBRE_WINS,
    POLICY_DO_NOT_SYNC,
    POLICY_SERVER_WINS,
    MetadataFieldPreview,
    build_metadata_preview,
    clean_metadata_policies,
)
from calibre_plugins.deluxe_sync.metadata_sync import (
    build_metadata_patch,
    metadata_patch_mismatches,
)
from calibre_plugins.deluxe_sync.models import CalibreBook, selected_calibre_books
from calibre_plugins.deluxe_sync.settings import (
    get_active_server_profile,
    get_active_server_profile_id,
    get_column_mappings,
    get_metadata_policies,
    set_metadata_policies,
)


try:
    load_translations()
except NameError:
    def _(text):
        return text

    def ngettext(singular, plural, count):
        return singular if count == 1 else plural


_HEADERS = (
    _("Field"),
    _("Calibre"),
    _("Server"),
    _("Proposed action"),
)

_FIELD_LABELS = {
    "cover": _("Cover"),
    "rating": _("Rating"),
    "review_note": _("Summary / Review"),
    "title": _("Title"),
    "authors": _("Authors"),
    "isbn": _("ISBN"),
    "asin": _("ASIN"),
    "series": _("Series"),
    "series_index": _("Series #"),
}

_POLICY_CHOICES = (
    (_("Calibre wins"), POLICY_CALIBRE_WINS),
    (_("Server wins"), POLICY_SERVER_WINS),
    (_("Do not sync"), POLICY_DO_NOT_SYNC),
)


def _supports_book_feedback(capabilities_response: dict[str, Any] | None) -> bool:
    if not isinstance(capabilities_response, dict):
        return False
    capabilities = capabilities_response.get("capabilities")
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    try:
        version = int(capabilities.get("book_feedback_version") or 0)
    except (TypeError, ValueError):
        version = 0
    return capabilities.get("book_feedback") is True and version >= 1


@dataclass(frozen=True)
class _FetchResult:
    book: CalibreBook
    metadata: dict[str, Any] | None
    document: str = ""
    error: str = ""


@dataclass(frozen=True)
class _SyncResult:
    book: CalibreBook
    metadata: dict[str, Any] | None
    document: str
    changed_fields: tuple[str, ...] = ()
    calibre_cover_bytes: bytes | None = None
    cover_hash: str = ""
    rating_action: str = ""
    calibre_rating: float | None = None
    review_action: str = ""
    calibre_review_note: str | None = None
    error: str = ""


@dataclass(frozen=True)
class _SyncPlan:
    preview: _FetchResult
    metadata_patch: dict[str, Any]
    rating_action: str = ""
    review_action: str = ""
    cover_action: str = ""
    cover_bytes: bytes | None = None
    cover_hash: str = ""
    error: str = ""


class _AsyncBridge(QObject):
    completed = pyqtSignal(object)


class SyncPreviewDialog(QDialog):
    """Compare selected linked books and apply approved metadata-only server writes."""

    def __init__(
        self,
        plugin_action,
        *,
        auto_refresh: bool = True,
        books: list[CalibreBook] | None = None,
        parent_dialog: QDialog | None = None,
    ):
        super().__init__(parent_dialog or plugin_action.gui)
        self.action = plugin_action
        self._explicit_books = list(books) if books is not None else None
        self._generation = 0
        self._loading = False
        self._library_uuid = ""
        self._profile_id = ""
        self._capabilities_response: dict[str, Any] | None = None
        self._write_supported = False
        self._cover_sync_supported = False
        self._feedback_supported = False
        self._results: list[_FetchResult] = []
        self._skipped_books: list[CalibreBook] = []
        self._bridge = _AsyncBridge(self)
        self._bridge.completed.connect(self._preview_loaded)
        self._sync_bridge = _AsyncBridge(self)
        self._sync_bridge.completed.connect(self._metadata_synced)
        self._policy_boxes: dict[str, QComboBox] = {}

        self.setWindowTitle(_("Deluxe Sync — Sync Preview"))
        layout = QVBoxLayout(self)

        intro = QLabel(
            _(
                "Compare Calibre metadata, covers, ratings, and mapped reviews with the server for the currently selected linked books. "
                "Text metadata shown as Calibre → Server can be written to the server; Server → Calibre "
                "text metadata remains preview-only. Covers, ratings, and the mapped Summary / Review column can sync in either direction when supported. "
                "If the preferred metadata side is empty while the other side has a value, Deluxe Sync keeps the "
                "existing value instead of erasing it. Review notes use the dedicated mapped column, never Calibre's built-in Comments field, and sync does not change reading progress."
            )
        )
        intro.setWordWrap(True)
        if self._explicit_books is not None:
            intro.setText(
                _(
                    "Compare Calibre metadata, covers, ratings, and mapped reviews with the server for this book batch. "
                    "Only linked books participate. Text metadata shown as Calibre → Server can "
                    "be written to the server; Server → Calibre text metadata remains preview-only. "
                    "Covers, ratings, and the mapped Summary / Review column can sync in either direction when supported. Empty preferred metadata "
                    "values never erase good data. Review notes use the dedicated mapped column, never Calibre's built-in Comments field, and sync does not change reading progress."
                )
            )
        layout.addWidget(intro)

        policy_label = QLabel(
            _(
                "Choose which side should win for each field. These choices are saved for future "
                "previews and metadata sync."
            )
        )
        policy_label.setWordWrap(True)
        layout.addWidget(policy_label)

        self.server_write_support_label = QLabel(_("Server writes: Checking…"))
        self.server_write_support_label.setWordWrap(True)
        layout.addWidget(self.server_write_support_label)

        policy_grid = QGridLayout()
        self.policy_grid = policy_grid
        policy_grid.setHorizontalSpacing(18)
        policy_grid.setVerticalSpacing(8)
        policies = clean_metadata_policies(get_metadata_policies())
        policy_positions = (
            ("cover", 0, 0),
            ("title", 1, 0),
            ("authors", 2, 0),
            ("isbn", 0, 1),
            ("asin", 1, 1),
            ("series", 2, 1),
            ("series_index", 0, 2),
            ("rating", 1, 2),
            ("review_note", 2, 2),
        )
        label_width = max(
            self.fontMetrics().horizontalAdvance(f"{_FIELD_LABELS[field]}:")
            for field, _row, _column in policy_positions
        ) + 8
        for field, row, visual_column in policy_positions:
            label_column = visual_column * 2
            combo_column = label_column + 1

            label = QLabel(f"{_FIELD_LABELS[field]}:")
            label.setMinimumWidth(label_width)
            label.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )

            combo = QComboBox()
            combo.setMinimumWidth(150)
            combo.setMinimumContentsLength(14)
            for text, policy in _POLICY_CHOICES:
                combo.addItem(text, policy)
            index = combo.findData(policies[field])
            if index >= 0:
                combo.setCurrentIndex(index)
            combo.currentIndexChanged.connect(self._policy_changed)
            self._policy_boxes[field] = combo

            policy_grid.addWidget(label, row, label_column)
            policy_grid.addWidget(combo, row, combo_column)
            policy_grid.setColumnStretch(combo_column, 1)
        layout.addLayout(policy_grid)

        self.status_label = QLabel(_("Ready."))
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.results_model = QStandardItemModel(0, len(_HEADERS), self)
        self.results_model.setHorizontalHeaderLabels(_HEADERS)
        self.results_table = QTableView()
        self.results_table.setModel(self.results_model)
        self.results_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.results_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.results_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.results_table.setAlternatingRowColors(False)
        self.results_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.results_table, 1)

        controls = QHBoxLayout()
        self.refresh_button = QPushButton(_("Refresh Preview"))
        self.refresh_button.clicked.connect(self.refresh_preview)
        controls.addWidget(self.refresh_button)

        self.sync_metadata_button = QPushButton(_("Sync Approved Changes"))
        self.sync_metadata_button.clicked.connect(
            lambda: self.sync_metadata(show_completion=True)
        )
        self.sync_metadata_button.setEnabled(False)
        controls.addWidget(self.sync_metadata_button)

        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(False)
        controls.addWidget(self.summary_label, 1)

        close_button = QPushButton(
            _("Back to Dashboard") if parent_dialog is not None else _("Close")
        )
        close_button.clicked.connect(self.accept)
        controls.addWidget(close_button)
        layout.addLayout(controls)

        self.resize(1120, 620)
        if auto_refresh:
            QTimer.singleShot(0, self.refresh_preview)

    def _plugin_version(self) -> str:
        base_plugin = getattr(self.action, "interface_action_base_plugin", None)
        return str(getattr(base_plugin, "version_string", None) or "unknown")

    def _current_policies(self) -> dict[str, str]:
        return clean_metadata_policies(
            {
                field: combo.currentData()
                for field, combo in self._policy_boxes.items()
            }
        )

    def _server_writable_fields(self) -> tuple[str, ...]:
        fields: list[str] = []
        if self._write_supported:
            fields.extend(
                ("title", "authors", "isbn", "asin", "series", "series_index")
            )
        if self._cover_sync_supported:
            fields.insert(0, "cover")
        if self._feedback_supported:
            insert_at = 1 if fields and fields[0] == "cover" else 0
            fields[insert_at:insert_at] = ["rating", "review_note"]
        return tuple(fields)

    def _update_server_write_support(self) -> None:
        writable = self._server_writable_fields()
        if writable:
            names = ", ".join(_FIELD_LABELS[field] for field in writable)
            self.server_write_support_label.setText(
                _("Server writes supported: {fields}.").format(fields=names)
            )
        else:
            self.server_write_support_label.setText(
                _(
                    "Server writes supported: None. Metadata and covers are read-only "
                    "on this server."
                )
            )

    def _approved_change_count(self) -> int:
        policies = self._current_policies()
        count = 0
        for result in self._results:
            if result.error or not isinstance(result.metadata, dict):
                continue
            if self._write_supported:
                count += len(build_metadata_patch(result.book, result.metadata, policies))
            if self._feedback_supported:
                for preview in build_metadata_preview(result.book, result.metadata, policies):
                    if preview.field not in {"rating", "review_note"}:
                        continue
                    if preview.action in {
                        ACTION_CALIBRE_TO_SERVER,
                        ACTION_SERVER_TO_CALIBRE,
                    }:
                        count += 1
            if self._cover_sync_supported:
                cover_preview = next(
                    (
                        preview
                        for preview in build_metadata_preview(
                            result.book, result.metadata, policies
                        )
                        if preview.field == "cover"
                    ),
                    None,
                )
                if cover_preview and cover_preview.action in {
                    ACTION_CALIBRE_TO_SERVER,
                    ACTION_SERVER_TO_CALIBRE,
                }:
                    count += 1
        return count

    def _refresh_sync_availability(self) -> bool:
        approved_count = self._approved_change_count()
        supports_writes = self._write_supported or self._cover_sync_supported or self._feedback_supported
        ready = not self._loading and supports_writes and approved_count > 0
        self.sync_metadata_button.setEnabled(ready)

        if self._loading:
            tooltip = _("Wait for the preview to finish loading.")
        elif not supports_writes:
            tooltip = _(
                "Disabled because this server does not support metadata, cover, rating, or review writes."
            )
        elif approved_count <= 0:
            tooltip = _("There are no approved Calibre → Server changes to write.")
        else:
            tooltip = _("Write the approved metadata and cover changes to the server.")
        self.sync_metadata_button.setToolTip(tooltip)
        return ready

    def _policy_changed(self, _index: int) -> None:
        policies = self._current_policies()
        set_metadata_policies(policies)
        if self._results or self._skipped_books:
            self._render_results()
        else:
            self._refresh_sync_availability()

    def _replace_rows(
        self,
        rows: list[tuple[str, str, str, str]],
        *,
        group_header_rows: set[int] | None = None,
        group_ranges: list[tuple[int, int]] | None = None,
    ) -> None:
        self.results_table.clearSpans()
        self.results_model.setRowCount(0)
        for row in rows:
            self.results_model.appendRow([QStandardItem(value) for value in row])

        palette = self.results_table.palette()
        group_brushes = (
            palette.brush(QPalette.ColorRole.Base),
            palette.brush(QPalette.ColorRole.AlternateBase),
        )
        for group_index, (start_row, end_row) in enumerate(group_ranges or []):
            brush = group_brushes[group_index % len(group_brushes)]
            for row_index in range(start_row, end_row + 1):
                for column_index in range(len(_HEADERS)):
                    item = self.results_model.item(row_index, column_index)
                    if item is not None:
                        item.setBackground(brush)

        header_height = max(
            self.results_table.verticalHeader().defaultSectionSize(),
            self.fontMetrics().height() + 12,
        )
        for row_index in sorted(group_header_rows or set()):
            if row_index < 0 or row_index >= self.results_model.rowCount():
                continue
            header_item = self.results_model.item(row_index, 0)
            if header_item is None:
                continue
            font = header_item.font()
            font.setBold(True)
            header_item.setFont(font)
            header_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            header_item.setSelectable(False)
            self.results_table.setSpan(row_index, 0, 1, len(_HEADERS))
            self.results_table.setRowHeight(row_index, header_height)

        self.results_table.resizeColumnsToContents()
        self.results_table.horizontalHeader().setStretchLastSection(True)

    @staticmethod
    def _display_value(value: Any) -> str:
        if value is None or value == "":
            return _("(empty)")
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)

    @staticmethod
    def _action_text(action: str) -> str:
        if action == ACTION_SAME:
            return _("Already matches")
        if action == ACTION_CALIBRE_TO_SERVER:
            return _("Calibre → Server")
        if action == ACTION_SERVER_TO_CALIBRE:
            return _("Server → Calibre")
        if action == ACTION_DO_NOT_SYNC:
            return _("No change (Do not sync)")
        if action == ACTION_KEEP_SERVER:
            return _("Keep server value (Calibre is empty)")
        if action == ACTION_KEEP_CALIBRE:
            return _("Keep Calibre value (Server is empty)")
        return _("No change")

    def _field_row(
        self,
        preview: MetadataFieldPreview,
    ) -> tuple[str, str, str, str]:
        if preview.field == "cover":
            calibre_display = _("Cover available") if preview.calibre_value else _("No cover")
            server_display = _("Cover available") if preview.server_value else _("No cover")
        elif preview.field == "rating":
            calibre_display = _("Unrated") if preview.calibre_value is None else _("{rating} / 5").format(rating=preview.calibre_value)
            server_display = _("Unrated") if preview.server_value is None else _("{rating} / 5").format(rating=preview.server_value)
        elif preview.field == "review_note":
            calibre_display = _("Not mapped") if preview.calibre_value is None else self._display_value(preview.calibre_value)
            server_display = self._display_value(preview.server_value)
        else:
            calibre_display = self._display_value(preview.calibre_value)
            server_display = self._display_value(preview.server_value)
        action_text = self._action_text(preview.action)
        if preview.field in {"rating", "review_note"}:
            if preview.field == "review_note" and preview.calibre_value is None:
                action_text = _("Map Summary / Review in Column Mappings")
            elif (
                preview.action in {ACTION_CALIBRE_TO_SERVER, ACTION_SERVER_TO_CALIBRE}
                and not self._feedback_supported
            ):
                action_text = _("Not supported by this server")
        elif preview.field == "cover":
            if (
                preview.action in {ACTION_CALIBRE_TO_SERVER, ACTION_SERVER_TO_CALIBRE}
                and not self._cover_sync_supported
            ):
                action_text = _("Not supported by this server")
        elif (
            preview.action == ACTION_CALIBRE_TO_SERVER
            and not self._write_supported
        ):
            action_text = _("Not supported by this server")
        elif preview.action == ACTION_SERVER_TO_CALIBRE:
            action_text = _("Preview only: Server → Calibre")

        return (
            _FIELD_LABELS.get(preview.field, preview.field),
            calibre_display,
            server_display,
            action_text,
        )

    def _render_results(self) -> None:
        rows: list[tuple[str, str, str, str]] = []
        group_header_rows: set[int] = set()
        group_ranges: list[tuple[int, int]] = []
        policies = self._current_policies()

        for book in self._skipped_books:
            group_start = len(rows)
            group_header_rows.add(group_start)
            rows.append((book.title, "", "", ""))
            rows.append((_("Status"), "", "", _("Not linked")))
            group_ranges.append((group_start, len(rows) - 1))

        failed = 0
        linked = 0
        for result in self._results:
            linked += 1
            group_start = len(rows)
            group_header_rows.add(group_start)
            rows.append((result.book.title, "", "", ""))

            if result.error:
                failed += 1
                rows.append(
                    (
                        _("Status"),
                        "",
                        "",
                        _("Error: {error}").format(error=result.error),
                    )
                )
                group_ranges.append((group_start, len(rows) - 1))
                continue

            for preview in build_metadata_preview(
                result.book,
                result.metadata,
                policies,
            ):
                rows.append(self._field_row(preview))
            group_ranges.append((group_start, len(rows) - 1))

        self._replace_rows(
            rows,
            group_header_rows=group_header_rows,
            group_ranges=group_ranges,
        )
        self.summary_label.setText(
            _(
                "Preview: {linked} linked, {unlinked} not linked, {failed} failed."
            ).format(
                linked=linked,
                unlinked=len(self._skipped_books),
                failed=failed,
            )
        )
        self._refresh_sync_availability()

    def refresh_preview(self) -> None:
        if self._loading:
            return

        self.summary_label.setText("")
        self._capabilities_response = None
        self._write_supported = False
        self._cover_sync_supported = False
        self._feedback_supported = False
        self._results = []
        self._skipped_books = []
        self.sync_metadata_button.setEnabled(False)
        self.server_write_support_label.setText(_("Server writes: Checking…"))

        try:
            books = (
                list(self._explicit_books)
                if self._explicit_books is not None
                else selected_calibre_books(getattr(self.action, "gui", None))
            )
        except RuntimeError as error:
            self.status_label.setText(str(error))
            self._replace_rows([])
            return

        if not books:
            self.status_label.setText(
                _("Select one or more Calibre books, then choose Sync Preview.")
            )
            self._replace_rows([])
            return

        profile = get_active_server_profile()
        if not profile:
            self.status_label.setText(
                _(
                    "Server authentication required. Open Configure Deluxe Sync → "
                    "Server Connection."
                )
            )
            self._replace_rows([])
            return

        server_url = str(profile.get("server_url") or "").strip()
        if not server_url:
            self.status_label.setText(_("Saved server profile is incomplete."))
            self._replace_rows([])
            return

        self._library_uuid = books[0].library_uuid
        self._profile_id = get_active_server_profile_id()
        bindings = get_library_bindings(self._library_uuid, self._profile_id)

        bound: list[tuple[CalibreBook, dict[str, Any]]] = []
        skipped: list[CalibreBook] = []
        for book in books:
            binding = bindings.get(book.book_uuid)
            document = (
                str(binding.get("server_document") or "").strip()
                if isinstance(binding, dict)
                else ""
            )
            if (
                not isinstance(binding, dict)
                or binding.get("enrolled") is not True
                or not document
            ):
                skipped.append(book)
                continue
            bound.append((book, binding))

        self._skipped_books = skipped
        if not bound:
            self.status_label.setText(
                _("None of the selected books are linked in Deluxe Sync.")
            )
            self._render_results()
            return

        self._generation += 1
        generation = self._generation
        self._loading = True
        self.refresh_button.setEnabled(False)
        self.sync_metadata_button.setEnabled(False)
        self.status_label.setText(
            ngettext(
                "Reading server metadata for {count} linked book…",
                "Reading server metadata for {count} linked books…",
                len(bound),
            ).format(count=len(bound))
        )
        self._render_results()

        Thread(
            target=self._load_worker,
            args=(generation, bound, server_url, dict(profile)),
            name="DeluxeSyncStage6MetadataPreview",
            daemon=True,
        ).start()

    def _load_worker(
        self,
        generation: int,
        bound: list[tuple[CalibreBook, dict[str, Any]]],
        server_url: str,
        profile: dict[str, Any],
    ) -> None:
        results: list[_FetchResult] = []
        capabilities: dict[str, Any] | None = None
        fatal_error: Exception | None = None

        try:
            api = DeluxeSyncApi(server_url, plugin_version=self._plugin_version())
            capabilities = api.discover_capabilities(profile)
            for book, binding in bound:
                document = str(binding.get("server_document") or "").strip()
                try:
                    payload = api.get_document_metadata(
                        document,
                        profile,
                        capabilities_response=capabilities,
                    )
                    metadata = payload.get("metadata")
                    if not isinstance(metadata, dict):
                        raise ApiError("Server document-metadata response is invalid.")
                    metadata = dict(metadata)
                    cover = payload.get("cover")
                    metadata["cover"] = (
                        str(cover.get("hash") or "").strip().lower()
                        if isinstance(cover, dict)
                        else ""
                    )
                    metadata["rating_known"] = False
                    metadata["review_known"] = False
                    if _supports_book_feedback(capabilities):
                        feedback_payload = api.get_document_feedback(
                            document,
                            profile,
                            capabilities_response=capabilities,
                        )
                        feedback = feedback_payload.get("feedback")
                        if not isinstance(feedback, dict):
                            raise ApiError("Server book-feedback response is invalid.")
                        metadata["rating_known"] = feedback.get("rating_known") is True
                        if metadata["rating_known"]:
                            metadata["rating"] = feedback.get("rating")
                        metadata["review_known"] = feedback.get("review_known") is True
                        if metadata["review_known"]:
                            metadata["review_note"] = feedback.get("review_note")
                    results.append(
                        _FetchResult(
                            book=book,
                            metadata=metadata,
                            document=document,
                        )
                    )
                except ApiError as error:
                    results.append(
                        _FetchResult(
                            book=book,
                            metadata=None,
                            document=document,
                            error=str(error),
                        )
                    )
        except Exception as error:
            fatal_error = error

        try:
            self._bridge.completed.emit(
                (generation, results, capabilities, fatal_error)
            )
        except RuntimeError:
            return

    def _preview_loaded(self, payload: object) -> None:
        generation, results, capabilities, fatal_error = payload
        if generation != self._generation:
            return

        self._loading = False
        self.refresh_button.setEnabled(True)
        self._capabilities_response = (
            dict(capabilities) if isinstance(capabilities, dict) else None
        )
        capability_values = (
            self._capabilities_response.get("capabilities")
            if isinstance(self._capabilities_response, dict)
            else {}
        )
        capability_values = (
            capability_values if isinstance(capability_values, dict) else {}
        )
        self._write_supported = (
            capability_values.get("document_metadata_write") is True
        )
        self._cover_sync_supported = (
            capability_values.get("document_cover_sync") is True
        )
        self._feedback_supported = bool(
            self._capabilities_response
            and _supports_book_feedback(self._capabilities_response)
        )

        if fatal_error is not None:
            self._write_supported = False
            self._cover_sync_supported = False
            self._feedback_supported = False
            self.server_write_support_label.setText(
                _("Server write support unavailable.")
            )
            if isinstance(fatal_error, AuthorizationError):
                message = _(
                    "Server authentication was rejected. Reconnect Deluxe Sync and try again."
                )
            else:
                message = _("Could not read server metadata: {error}").format(
                    error=fatal_error
                )
            self.status_label.setText(message)
            self._results = []
            self._render_results()
            return

        gui = getattr(self.action, "gui", None)
        db = getattr(gui, "current_db", None)
        current_library_uuid = str(getattr(db, "library_id", "") or "").strip()
        if db is None or current_library_uuid != self._library_uuid:
            self._write_supported = False
            self._cover_sync_supported = False
            self._feedback_supported = False
            self.status_label.setText(
                _(
                    "The active Calibre library changed while metadata was being read. "
                    "The preview was discarded."
                )
            )
            self._results = []
            self._replace_rows([])
            self._refresh_sync_availability()
            return

        self._results = list(results)
        self._update_server_write_support()
        if self._write_supported or self._cover_sync_supported or self._feedback_supported:
            self.status_label.setText(_("Ready."))
        else:
            self.status_label.setText(
                _(
                    "Preview ready. This server does not support metadata, cover, rating, or review writes."
                )
            )
        self._render_results()

    def sync_metadata(self, *, show_completion: bool = False) -> None:
        if self._loading:
            return

        if not isinstance(self._capabilities_response, dict) or not (
            self._write_supported or self._cover_sync_supported or self._feedback_supported
        ):
            self.status_label.setText(
                _("This server does not support the approved metadata, cover, rating, or review changes.")
            )
            self._refresh_sync_availability()
            return

        if get_active_server_profile_id() != self._profile_id:
            self.status_label.setText(
                _("The active server profile changed. Refresh Preview before syncing.")
            )
            self.sync_metadata_button.setEnabled(False)
            return

        profile = get_active_server_profile()
        if not profile:
            self.status_label.setText(
                _(
                    "Server authentication required. Open Configure Deluxe Sync → "
                    "Server Connection."
                )
            )
            self.sync_metadata_button.setEnabled(False)
            return

        server_url = str(profile.get("server_url") or "").strip()
        if not server_url:
            self.status_label.setText(_("Saved server profile is incomplete."))
            self.sync_metadata_button.setEnabled(False)
            return

        gui = getattr(self.action, "gui", None)
        db = getattr(gui, "current_db", None)
        current_library_uuid = str(getattr(db, "library_id", "") or "").strip()
        if db is None or current_library_uuid != self._library_uuid:
            self.status_label.setText(
                _("Return to the Calibre library used for this Preview before syncing.")
            )
            self.sync_metadata_button.setEnabled(False)
            return

        calibre_api = getattr(db, "new_api", None)
        policies = self._current_policies()
        planned: list[_SyncPlan] = []
        for result in self._results:
            if result.error or not isinstance(result.metadata, dict):
                continue

            previews = build_metadata_preview(result.book, result.metadata, policies)
            metadata_patch = (
                build_metadata_patch(result.book, result.metadata, policies)
                if self._write_supported
                else {}
            )
            rating_preview = next(
                (preview for preview in previews if preview.field == "rating"),
                None,
            )
            review_preview = next(
                (preview for preview in previews if preview.field == "review_note"),
                None,
            )
            cover_preview = next(
                (preview for preview in previews if preview.field == "cover"),
                None,
            )
            rating_action = ""
            review_action = ""
            cover_action = ""
            cover_bytes = None
            cover_hash = ""
            plan_error = ""
            if (
                self._feedback_supported
                and rating_preview is not None
                and rating_preview.action
                in {ACTION_CALIBRE_TO_SERVER, ACTION_SERVER_TO_CALIBRE}
            ):
                rating_action = rating_preview.action
                if rating_action == ACTION_SERVER_TO_CALIBRE and calibre_api is None:
                    plan_error = _("Calibre rating API is unavailable.")
            if (
                self._feedback_supported
                and review_preview is not None
                and review_preview.action
                in {ACTION_CALIBRE_TO_SERVER, ACTION_SERVER_TO_CALIBRE}
            ):
                review_action = review_preview.action
                if review_action == ACTION_SERVER_TO_CALIBRE and calibre_api is None:
                    plan_error = _("Calibre custom-column API is unavailable.")
            if (
                self._cover_sync_supported
                and cover_preview is not None
                and cover_preview.action
                in {ACTION_CALIBRE_TO_SERVER, ACTION_SERVER_TO_CALIBRE}
            ):
                cover_action = cover_preview.action
                if cover_action == ACTION_CALIBRE_TO_SERVER:
                    cover_hash = str(result.book.cover_hash or "").strip().lower()
                    if calibre_api is None:
                        plan_error = _("Calibre cover API is unavailable.")
                    else:
                        try:
                            raw_cover = calibre_api.cover(result.book.book_id)
                            cover_bytes = bytes(raw_cover) if raw_cover else None
                        except Exception:
                            cover_bytes = None
                        if not cover_bytes:
                            plan_error = _("Calibre cover could not be read.")
                else:
                    cover_hash = str(result.metadata.get("cover") or "").strip().lower()
                    if not cover_hash:
                        plan_error = _("Server cover is unavailable.")

            if metadata_patch or rating_action or review_action or cover_action or plan_error:
                planned.append(
                    _SyncPlan(
                        preview=result,
                        metadata_patch=metadata_patch,
                        rating_action=rating_action,
                        review_action=review_action,
                        cover_action=cover_action,
                        cover_bytes=cover_bytes,
                        cover_hash=cover_hash,
                        error=plan_error,
                    )
                )

        if not planned:
            self.status_label.setText(_("No approved changes are ready to sync."))
            self._refresh_sync_availability()
            return

        self._generation += 1
        generation = self._generation
        self._loading = True
        self.refresh_button.setEnabled(False)
        self.sync_metadata_button.setEnabled(False)
        self.summary_label.setText("")
        self.status_label.setText(
            ngettext(
                "Syncing approved changes for {count} linked book…",
                "Syncing approved changes for {count} linked books…",
                len(planned),
            ).format(count=len(planned))
        )

        Thread(
            target=self._sync_worker,
            args=(
                generation,
                planned,
                server_url,
                dict(profile),
                show_completion,
            ),
            name="DeluxeSyncStage6ApprovedSync",
            daemon=True,
        ).start()

    def _sync_worker(
        self,
        generation: int,
        planned: list[_SyncPlan],
        server_url: str,
        profile: dict[str, Any],
        show_completion: bool,
    ) -> None:
        results: list[_SyncResult] = []
        capabilities: dict[str, Any] | None = None
        fatal_error: Exception | None = None

        try:
            api = DeluxeSyncApi(server_url, plugin_version=self._plugin_version())
            capabilities = api.discover_capabilities(profile)
            capability_values = capabilities.get("capabilities")
            capability_values = (
                capability_values if isinstance(capability_values, dict) else {}
            )
            if any(plan.metadata_patch for plan in planned) and (
                capability_values.get("document_metadata_write") is not True
            ):
                raise CapabilityError(
                    "This server no longer supports metadata-only document writes."
                )
            if any(plan.cover_action for plan in planned) and (
                capability_values.get("document_cover_sync") is not True
            ):
                raise CapabilityError(
                    "This server no longer supports cover synchronization."
                )
            if any(plan.rating_action or plan.review_action for plan in planned) and not _supports_book_feedback(
                capabilities
            ):
                raise CapabilityError(
                    "This server no longer supports book ratings and reviews."
                )

            for plan in planned:
                preview_result = plan.preview
                if plan.error:
                    results.append(
                        _SyncResult(
                            book=preview_result.book,
                            metadata=preview_result.metadata,
                            document=preview_result.document,
                            error=plan.error,
                        )
                    )
                    continue

                try:
                    changed_fields: list[str] = []
                    downloaded_cover = None
                    calibre_rating = None
                    calibre_review_note = None
                    if plan.metadata_patch:
                        api.patch_document_metadata(
                            preview_result.document,
                            plan.metadata_patch,
                            profile,
                            capabilities_response=capabilities,
                        )
                        changed_fields.extend(sorted(plan.metadata_patch))

                    feedback_patch: dict[str, Any] = {}
                    if plan.rating_action == ACTION_CALIBRE_TO_SERVER:
                        feedback_patch["rating"] = preview_result.book.rating
                    if plan.review_action == ACTION_CALIBRE_TO_SERVER:
                        feedback_patch["review_note"] = (
                            preview_result.book.review_note
                            if preview_result.book.review_note
                            else None
                        )

                    feedback = None
                    if feedback_patch:
                        feedback_payload = api.patch_document_feedback(
                            preview_result.document,
                            feedback_patch,
                            profile,
                            capabilities_response=capabilities,
                        )
                        feedback = feedback_payload.get("feedback")
                        if not isinstance(feedback, dict):
                            raise ApiError("Server book-feedback response is invalid.")
                        if "rating" in feedback_patch:
                            if feedback.get("rating_known") is not True or feedback.get("rating") != feedback_patch["rating"]:
                                raise ApiError("Server rating verification failed after update.")
                            changed_fields.append("rating")
                        if "review_note" in feedback_patch:
                            if feedback.get("review_known") is not True or feedback.get("review_note") != feedback_patch["review_note"]:
                                raise ApiError("Server review verification failed after update.")
                            changed_fields.append("review_note")

                    if (
                        plan.rating_action == ACTION_SERVER_TO_CALIBRE
                        or plan.review_action == ACTION_SERVER_TO_CALIBRE
                    ):
                        if feedback is None:
                            feedback_payload = api.get_document_feedback(
                                preview_result.document,
                                profile,
                                capabilities_response=capabilities,
                            )
                            feedback = feedback_payload.get("feedback")
                        if not isinstance(feedback, dict):
                            raise ApiError("Server book-feedback response is invalid.")
                        if plan.rating_action == ACTION_SERVER_TO_CALIBRE:
                            if feedback.get("rating_known") is not True:
                                raise ApiError("Server rating is no longer available.")
                            calibre_rating = feedback.get("rating")
                            changed_fields.append("rating")
                        if plan.review_action == ACTION_SERVER_TO_CALIBRE:
                            if feedback.get("review_known") is not True:
                                raise ApiError("Server review is no longer available.")
                            calibre_review_note = feedback.get("review_note")
                            changed_fields.append("review_note")

                    if plan.cover_action == ACTION_CALIBRE_TO_SERVER:
                        uploaded = api.upload_document_cover(
                            preview_result.document,
                            plan.cover_bytes or b"",
                            profile,
                            capabilities_response=capabilities,
                        )
                        uploaded_hash = str(uploaded.get("cover_hash") or "").strip().lower()
                        if uploaded_hash != plan.cover_hash:
                            raise ApiError("Server cover verification failed after upload.")
                        changed_fields.append("cover")
                    elif plan.cover_action == ACTION_SERVER_TO_CALIBRE:
                        downloaded_cover = api.download_cover(plan.cover_hash)
                        changed_fields.append("cover")

                    payload = api.get_document_metadata(
                        preview_result.document,
                        profile,
                        capabilities_response=capabilities,
                    )
                    metadata = payload.get("metadata")
                    if not isinstance(metadata, dict):
                        raise ApiError("Server document-metadata response is invalid.")
                    metadata = dict(metadata)
                    cover = payload.get("cover")
                    server_cover_hash = (
                        str(cover.get("hash") or "").strip().lower()
                        if isinstance(cover, dict)
                        else ""
                    )
                    metadata["cover"] = server_cover_hash
                    metadata["rating_known"] = False
                    metadata["review_known"] = False
                    if _supports_book_feedback(capabilities):
                        feedback_payload = api.get_document_feedback(
                            preview_result.document,
                            profile,
                            capabilities_response=capabilities,
                        )
                        feedback = feedback_payload.get("feedback")
                        if not isinstance(feedback, dict):
                            raise ApiError("Server book-feedback response is invalid.")
                        metadata["rating_known"] = feedback.get("rating_known") is True
                        if metadata["rating_known"]:
                            metadata["rating"] = feedback.get("rating")
                        else:
                            metadata.pop("rating", None)
                        metadata["review_known"] = feedback.get("review_known") is True
                        if metadata["review_known"]:
                            metadata["review_note"] = feedback.get("review_note")
                        else:
                            metadata.pop("review_note", None)

                    mismatches = metadata_patch_mismatches(
                        plan.metadata_patch, metadata
                    )
                    if mismatches:
                        raise ApiError(
                            "Server metadata verification failed for: {fields}.".format(
                                fields=", ".join(mismatches)
                            )
                        )
                    if plan.cover_action and server_cover_hash != plan.cover_hash:
                        raise ApiError("Server cover changed during synchronization.")

                    results.append(
                        _SyncResult(
                            book=preview_result.book,
                            metadata=metadata,
                            document=preview_result.document,
                            changed_fields=tuple(changed_fields),
                            calibre_cover_bytes=downloaded_cover,
                            cover_hash=plan.cover_hash if plan.cover_action else "",
                            rating_action=plan.rating_action,
                            calibre_rating=calibre_rating,
                            review_action=plan.review_action,
                            calibre_review_note=calibre_review_note,
                        )
                    )
                except ApiError as error:
                    results.append(
                        _SyncResult(
                            book=preview_result.book,
                            metadata=preview_result.metadata,
                            document=preview_result.document,
                            error=str(error),
                        )
                    )
        except Exception as error:
            fatal_error = error

        try:
            self._sync_bridge.completed.emit(
                (
                    generation,
                    results,
                    capabilities,
                    fatal_error,
                    show_completion,
                )
            )
        except RuntimeError:
            return

    def _metadata_synced(self, payload: object) -> None:
        (
            generation,
            sync_results,
            capabilities,
            fatal_error,
            show_completion,
        ) = payload
        if generation != self._generation:
            return

        self._loading = False
        self.refresh_button.setEnabled(True)
        self._capabilities_response = (
            dict(capabilities) if isinstance(capabilities, dict) else None
        )
        capability_values = (
            self._capabilities_response.get("capabilities")
            if isinstance(self._capabilities_response, dict)
            else {}
        )
        capability_values = (
            capability_values if isinstance(capability_values, dict) else {}
        )
        self._write_supported = (
            capability_values.get("document_metadata_write") is True
        )
        self._cover_sync_supported = (
            capability_values.get("document_cover_sync") is True
        )
        self._feedback_supported = bool(
            self._capabilities_response
            and _supports_book_feedback(self._capabilities_response)
        )

        if fatal_error is not None:
            if isinstance(fatal_error, AuthorizationError):
                message = _(
                    "Server authentication was rejected. Reconnect Deluxe Sync and try again."
                )
            elif isinstance(fatal_error, CapabilityError):
                message = str(fatal_error)
            else:
                message = _("Could not sync approved changes: {error}").format(
                    error=fatal_error
                )
            self.status_label.setText(message)
            self._refresh_sync_availability()
            return

        gui = getattr(self.action, "gui", None)
        db = getattr(gui, "current_db", None)
        current_library_uuid = str(getattr(db, "library_id", "") or "").strip()
        if db is None or current_library_uuid != self._library_uuid:
            self._write_supported = False
            self._cover_sync_supported = False
            self._feedback_supported = False
            self.status_label.setText(
                _(
                    "The active Calibre library changed while changes were being synced. "
                    "Server writes may have completed; return to the original library and "
                    "refresh Preview."
                )
            )
            self._results = []
            self._replace_rows([])
            self._refresh_sync_availability()
            return

        calibre_api = getattr(db, "new_api", None)
        adjusted_results: list[_SyncResult] = []
        for synced in sync_results:
            if not isinstance(synced, _SyncResult):
                continue
            if synced.rating_action == ACTION_SERVER_TO_CALIBRE and not synced.error:
                try:
                    if calibre_api is None:
                        raise RuntimeError("Calibre rating API is unavailable.")
                    raw_rating = (
                        0
                        if synced.calibre_rating is None
                        else int(round(float(synced.calibre_rating) * 2))
                    )
                    calibre_api.set_field(
                        "rating",
                        {synced.book.book_id: raw_rating},
                    )
                    stored_rating = calibre_api.field_for(
                        "rating",
                        synced.book.book_id,
                        default_value=0,
                    )
                    stored_raw = float(stored_rating or 0)
                    stored_stars = None if stored_raw == 0 else stored_raw / 2
                    if stored_stars != synced.calibre_rating:
                        raise RuntimeError(
                            "Calibre rating verification failed after update."
                        )
                    synced = replace(
                        synced,
                        book=replace(synced.book, rating=synced.calibre_rating),
                    )
                except Exception as error:
                    synced = replace(
                        synced,
                        changed_fields=(),
                        error=_("Could not update the Calibre rating: {error}").format(
                            error=error
                        ),
                    )
            if synced.review_action == ACTION_SERVER_TO_CALIBRE and not synced.error:
                try:
                    if calibre_api is None:
                        raise RuntimeError("Calibre custom-column API is unavailable.")
                    review_lookup = str(
                        get_column_mappings().get("review_note") or ""
                    ).strip()
                    if not review_lookup:
                        raise RuntimeError(
                            "Summary / Review custom column is not mapped."
                        )
                    review_value = str(synced.calibre_review_note or "")
                    calibre_api.set_field(
                        review_lookup,
                        {synced.book.book_id: review_value},
                    )
                    stored_review = str(
                        calibre_api.field_for(
                            review_lookup,
                            synced.book.book_id,
                            default_value="",
                        )
                        or ""
                    )
                    if stored_review != review_value:
                        raise RuntimeError(
                            "Calibre Summary / Review verification failed after update."
                        )
                    synced = replace(
                        synced,
                        book=replace(synced.book, review_note=review_value),
                    )
                except Exception as error:
                    synced = replace(
                        synced,
                        changed_fields=(),
                        error=_(
                            "Could not update the Calibre Summary / Review column: {error}"
                        ).format(error=error),
                    )
            if synced.calibre_cover_bytes and not synced.error:
                try:
                    if calibre_api is None:
                        raise RuntimeError("Calibre cover API is unavailable.")
                    calibre_api.set_cover(
                        {synced.book.book_id: bytes(synced.calibre_cover_bytes)}
                    )
                    stored_cover = calibre_api.cover(synced.book.book_id)
                    actual_hash = (
                        hashlib.sha256(bytes(stored_cover)).hexdigest()
                        if stored_cover
                        else ""
                    )
                    if actual_hash != synced.cover_hash:
                        raise RuntimeError(
                            "Calibre cover verification failed after update."
                        )
                    synced = replace(
                        synced,
                        book=replace(synced.book, cover_hash=synced.cover_hash),
                    )
                except Exception as error:
                    synced = replace(
                        synced,
                        changed_fields=(),
                        error=_("Could not update the Calibre cover: {error}").format(
                            error=error
                        ),
                    )
            adjusted_results.append(synced)

        sync_by_book = {
            result.book.book_uuid: result
            for result in adjusted_results
        }
        previous_results = list(self._results)
        merged: list[_FetchResult] = []
        for result in previous_results:
            synced = sync_by_book.get(result.book.book_uuid)
            if synced is None:
                merged.append(result)
                continue
            merged.append(
                _FetchResult(
                    book=synced.book,
                    metadata=synced.metadata,
                    document=synced.document,
                    error=synced.error,
                )
            )

        self._results = merged
        updated_books = sum(
            1
            for result in adjusted_results
            if result.changed_fields and not result.error
        )
        failed_writes = sum(1 for result in adjusted_results if result.error)
        preexisting_failures = sum(
            1
            for result in previous_results
            if result.error and result.book.book_uuid not in sync_by_book
        )
        failed_books = failed_writes + preexisting_failures
        no_change_books = sum(
            1
            for result in previous_results
            if not result.error and result.book.book_uuid not in sync_by_book
        )

        self.status_label.setText(_("Ready."))
        self._render_results()
        summary = _(
            "Sync complete: {updated} updated, {unchanged} no approved changes, "
            "{skipped} not linked, {failed} failed."
        ).format(
            updated=updated_books,
            unchanged=no_change_books,
            skipped=len(self._skipped_books),
            failed=failed_books,
        )
        self.summary_label.setText(summary)
        if show_completion:
            self._show_metadata_sync_summary(summary)

    def _show_metadata_sync_summary(self, summary: str) -> None:
        QMessageBox.information(self, _("Sync Complete"), summary)
