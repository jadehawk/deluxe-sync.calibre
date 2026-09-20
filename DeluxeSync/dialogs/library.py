"""Read-only server library browser for Deluxe Sync Stage 2."""

from __future__ import annotations

from threading import Thread
from typing import Any

from qt.core import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QObject,
    QPushButton,
    QSortFilterProxyModel,
    QStandardItem,
    QStandardItemModel,
    QTableView,
    QTimer,
    Qt,
    QVBoxLayout,
    pyqtSignal,
)

from calibre_plugins.deluxe_sync.api import ApiError, AuthorizationError, DeluxeSyncApi
from calibre_plugins.deluxe_sync.logger import get_logger
from calibre_plugins.deluxe_sync.settings import get_active_server_profile


try:
    load_translations()
except NameError:
    def _(text):
        return text

    def ngettext(singular, plural, count):
        return singular if count == 1 else plural


LOGGER = get_logger("library")

_LIBRARY_HEADERS = (
    _("Title"),
    _("Author(s)"),
    _("Filename / Versions"),
    _("ISBN"),
    _("ASIN"),
    _("Series"),
    _("Progress"),
    _("State"),
    _("Raw document"),
    _("Logical book"),
)


def _clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(part).strip() for part in value if str(part).strip())
    return str(value).strip()


def _series_text(book: dict[str, Any]) -> str:
    series = _clean(book.get("series"))
    index = book.get("series_index")
    if index is None or index == "":
        return series
    try:
        numeric = float(index)
    except (TypeError, ValueError):
        suffix = _clean(index)
    else:
        suffix = str(int(numeric)) if numeric.is_integer() else str(numeric)
    if series:
        return f"{series} · #{suffix}"
    return f"#{suffix}"


def _percentage_value(book: dict[str, Any]) -> float:
    try:
        value = float(book.get("percentage") or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(100.0, value * 100.0))


def _percentage_text(book: dict[str, Any]) -> str:
    value = _percentage_value(book)
    if abs(value - round(value)) < 0.01:
        return f"{round(value):.0f}%"
    return f"{value:.1f}%"


def _reading_state_text(book: dict[str, Any]) -> str:
    value = _percentage_value(book)
    reading_state = book.get("reading_state")
    manual = isinstance(reading_state, dict) and reading_state.get("manual_completion") is True
    if value >= 99.5:
        return _("Finished (manual)") if manual else _("Finished")
    if value <= 0:
        return _("Not started")
    return _("Reading")


def _book_cells(book: dict[str, Any]) -> list[str]:
    is_logical = book.get("kind") == "logical"
    linked_count = int(book.get("linked_count") or 0) if is_logical else 0
    filename = (
        ngettext("{count} linked version", "{count} linked versions", linked_count).format(count=linked_count)
        if is_logical
        else _clean(book.get("filename"))
    )
    document = "" if is_logical else _clean(book.get("document"))
    logical_id = _clean(book.get("logical_book_id")) if is_logical else ""

    return [
        _clean(book.get("title")) or _("Unknown book"),
        _clean(book.get("authors")),
        filename,
        "" if is_logical else _clean(book.get("isbn")),
        "" if is_logical else _clean(book.get("asin")),
        _series_text(book),
        _percentage_text(book),
        _reading_state_text(book),
        document,
        logical_id,
    ]


class LinkedVersionsDialog(QDialog):
    """Display the raw documents that belong to one logical book."""

    def __init__(self, logical_book: dict[str, Any], parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("Deluxe Sync — Linked Versions"))

        layout = QVBoxLayout(self)
        title = _clean(logical_book.get("title")) or _("Linked book")
        count = len(logical_book.get("members") or [])
        heading = QLabel(
            _("<b>{title}</b><br>{versions}").format(
                title=title,
                versions=ngettext(
                    "{count} linked version",
                    "{count} linked versions",
                    count,
                ).format(count=count),
            )
        )
        heading.setWordWrap(True)
        layout.addWidget(heading)

        headers = (
            _("Title"),
            _("Author(s)"),
            _("Filename"),
            _("ISBN"),
            _("ASIN"),
            _("Series"),
            _("Progress"),
            _("State"),
            _("Raw document"),
        )
        model = QStandardItemModel(0, len(headers), self)
        model.setHorizontalHeaderLabels(headers)

        for member in logical_book.get("members") or []:
            if not isinstance(member, dict):
                continue
            values = [
                _clean(member.get("title")) or _("Unknown book"),
                _clean(member.get("authors")),
                _clean(member.get("filename")),
                _clean(member.get("isbn")),
                _clean(member.get("asin")),
                _series_text(member),
                _percentage_text(member),
                _reading_state_text(member),
                _clean(member.get("document")),
            ]
            model.appendRow([QStandardItem(value) for value in values])

        table = QTableView()
        table.setModel(model)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        table.setSortingEnabled(True)
        table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(table, 1)

        close_button = QPushButton(_("Close"))
        close_button.clicked.connect(self.accept)
        layout.addWidget(close_button)

        self.resize(1120, 520)


class _AsyncBridge(QObject):
    """Deliver worker-thread results back to the Calibre GUI thread."""

    completed = pyqtSignal(object)


class LibraryDialog(QDialog):
    """Browse the authenticated user's server library without changing data."""

    def __init__(self, plugin_action):
        super().__init__(plugin_action.gui)
        self.action = plugin_action
        self._api: DeluxeSyncApi | None = None
        self._profile: dict[str, Any] | None = None
        self._logical_cache: dict[int, dict[str, Any]] = {}
        self._load_generation = 0
        self._loading = False
        self._details_loading_id: int | None = None
        self._library_summary = "Ready to load the server library."

        self._library_bridge = _AsyncBridge(self)
        self._library_bridge.completed.connect(self._library_loaded)
        self._details_bridge = _AsyncBridge(self)
        self._details_bridge.completed.connect(self._linked_details_loaded)

        self.setWindowTitle(_("Deluxe Sync — Server Library"))
        layout = QVBoxLayout(self)

        heading = QLabel(_("<b>Server Library</b>"))
        layout.addWidget(heading)

        intro = QLabel(
            _("Read-only view of books stored on the Deluxe Sync server. "
              "Linked book versions are shown as one logical book so they are not duplicated.")
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        controls = QHBoxLayout()
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText(
            _("Search title, author, identifier, series, or document id…")
        )
        self.search_box.setClearButtonEnabled(True)
        controls.addWidget(self.search_box, 1)

        self.refresh_button = QPushButton(_("Refresh"))
        self.refresh_button.clicked.connect(self.refresh_library)
        controls.addWidget(self.refresh_button)

        self.versions_button = QPushButton(_("Linked Versions…"))
        self.versions_button.setEnabled(False)
        self.versions_button.clicked.connect(self.show_linked_versions)
        controls.addWidget(self.versions_button)

        self.configure_button = QPushButton(_("Configure Deluxe Sync"))
        self.configure_button.clicked.connect(self.action.show_config)
        controls.addWidget(self.configure_button)
        layout.addLayout(controls)

        self.status_label = QLabel(self._library_summary)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.model = QStandardItemModel(0, len(_LIBRARY_HEADERS), self)
        self.model.setHorizontalHeaderLabels(_LIBRARY_HEADERS)

        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.proxy.setFilterKeyColumn(-1)
        self.proxy.setDynamicSortFilter(True)
        self.search_box.textChanged.connect(self.proxy.setFilterFixedString)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.selectionModel().selectionChanged.connect(self._selection_changed)
        layout.addWidget(self.table, 1)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_button = QPushButton(_("Close"))
        close_button.clicked.connect(self.accept)
        close_row.addWidget(close_button)
        layout.addLayout(close_row)

        self.resize(1320, 700)
        QTimer.singleShot(0, self.refresh_library)

    def _plugin_version(self) -> str:
        base_plugin = getattr(self.action, "interface_action_base_plugin", None)
        value = getattr(base_plugin, "version_string", None)
        return str(value or "unknown")

    def _current_library_uuid(self) -> str:
        gui = getattr(self.action, "gui", None)
        db = getattr(gui, "current_db", None)
        return str(getattr(db, "library_id", "") or "").strip()

    def _connection(self) -> tuple[DeluxeSyncApi, dict[str, Any]]:
        if not self._current_library_uuid():
            raise AuthorizationError(
                _("Open a Calibre library before browsing Deluxe Sync."),
                status=401,
            )

        profile = get_active_server_profile()
        if not profile:
            raise AuthorizationError(
                _("Server authentication required. Open Configure Deluxe Sync → Server Connection."),
                status=401,
            )

        server_url = _clean(profile.get("server_url"))
        if not server_url:
            raise AuthorizationError(
                _("Saved server profile is incomplete. Connect to the server again."),
                status=401,
            )

        return (
            DeluxeSyncApi(server_url, plugin_version=self._plugin_version()),
            profile,
        )

    def refresh_library(self) -> None:
        if self._loading:
            return

        self._load_generation += 1
        generation = self._load_generation
        self._details_loading_id = None
        self.refresh_button.setEnabled(False)
        self.versions_button.setEnabled(False)
        self.status_label.setText(_("Loading server library…"))

        try:
            api, profile = self._connection()
        except AuthorizationError as error:
            self._api = None
            self._profile = None
            self.model.removeRows(0, self.model.rowCount())
            self._library_summary = str(error)
            self.status_label.setText(self._library_summary)
            self.refresh_button.setEnabled(True)
            LOGGER.warning(
                "Server library authorization failed status=%s error_type=%s",
                error.status,
                type(error).__name__,
            )
            return

        self._loading = True
        Thread(
            target=self._load_library_worker,
            args=(generation, api, profile),
            name="DeluxeSyncLibraryLoad",
            daemon=True,
        ).start()

    def _load_library_worker(
        self,
        generation: int,
        api: DeluxeSyncApi,
        profile: dict[str, Any],
    ) -> None:
        payload = None
        error: Exception | None = None
        try:
            payload = api.get_library(profile)
        except Exception as caught:
            error = caught

        try:
            self._library_bridge.completed.emit(
                (generation, api, profile, payload, error)
            )
        except RuntimeError:
            return

    def _library_loaded(self, result: object) -> None:
        generation, api, profile, payload, error = result
        if generation != self._load_generation:
            return

        self._loading = False
        self.refresh_button.setEnabled(True)

        if error is not None:
            if isinstance(error, AuthorizationError):
                self._api = None
                self._profile = None
                self.model.removeRows(0, self.model.rowCount())
                self._library_summary = str(error)
                LOGGER.warning(
                    "Server library authorization failed status=%s error_type=%s",
                    error.status,
                    type(error).__name__,
                )
            elif isinstance(error, ApiError):
                self._library_summary = _("Could not load the server library: {error}").format(error=error)
                LOGGER.warning(
                    "Server library load failed status=%s error_type=%s",
                    error.status,
                    type(error).__name__,
                )
            else:
                self._library_summary = _("Could not load the server library.")
                LOGGER.error(
                    "Unexpected server library load failure error_type=%s",
                    type(error).__name__,
                )
            self.status_label.setText(self._library_summary)
            self._selection_changed()
            return

        if not isinstance(payload, dict):
            self._library_summary = _("Could not load the server library: invalid response.")
            self.status_label.setText(self._library_summary)
            self._selection_changed()
            return

        self._api = api
        self._profile = profile
        self._logical_cache.clear()
        self._populate(payload)
        self._selection_changed()

    def _populate(self, payload: dict[str, Any]) -> None:
        books = payload.get("books")
        if not isinstance(books, list):
            books = []

        self.table.setSortingEnabled(False)
        self.model.removeRows(0, self.model.rowCount())

        for book in books:
            if not isinstance(book, dict):
                continue
            row = [QStandardItem(value) for value in _book_cells(book)]
            row[0].setData(book, Qt.ItemDataRole.UserRole)
            self.model.appendRow(row)

        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.SortOrder.AscendingOrder)

        visible = int(payload.get("visible_count") or self.model.rowCount())
        raw = int(payload.get("raw_count") or 0)
        logical = int(payload.get("logical_count") or 0)
        linked_raw = int(payload.get("linked_raw_count") or 0)
        self._library_summary = " · ".join(
            (
                ngettext("{count} visible book", "{count} visible books", visible).format(count=visible),
                ngettext("{count} raw record", "{count} raw records", raw).format(count=raw),
                ngettext("{count} linked book", "{count} linked books", logical).format(count=logical),
                ngettext(
                    "{count} linked raw record",
                    "{count} linked raw records",
                    linked_raw,
                ).format(count=linked_raw),
            )
        )
        self.status_label.setText(self._library_summary)
        LOGGER.info(
            "Server library loaded visible_count=%s raw_count=%s logical_count=%s linked_raw_count=%s",
            visible,
            raw,
            logical,
            linked_raw,
        )

    def _selected_book(self) -> dict[str, Any] | None:
        current = self.table.currentIndex()
        if not current.isValid():
            return None
        source = self.proxy.mapToSource(current)
        item = self.model.item(source.row(), 0)
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return value if isinstance(value, dict) else None

    def _selection_changed(self, *_args) -> None:
        book = self._selected_book()
        self.versions_button.setEnabled(
            not self._loading
            and self._details_loading_id is None
            and isinstance(book, dict)
            and book.get("kind") == "logical"
        )

    def show_linked_versions(self) -> None:
        book = self._selected_book()
        if not book or book.get("kind") != "logical":
            return

        try:
            logical_book_id = int(book.get("logical_book_id"))
        except (TypeError, ValueError):
            self.status_label.setText(_("This linked book has an invalid server id."))
            return

        details = self._logical_cache.get(logical_book_id)
        if details is not None:
            self._open_linked_versions(details)
            return

        if self._details_loading_id is not None:
            return

        try:
            if self._api is None or self._profile is None:
                self._api, self._profile = self._connection()
        except AuthorizationError as error:
            self.status_label.setText(str(error))
            return

        self._details_loading_id = logical_book_id
        self.versions_button.setEnabled(False)
        self.status_label.setText(_("Loading linked versions…"))
        generation = self._load_generation
        Thread(
            target=self._load_details_worker,
            args=(
                generation,
                logical_book_id,
                self._api,
                self._profile,
            ),
            name="DeluxeSyncLinkedVersionsLoad",
            daemon=True,
        ).start()

    def _load_details_worker(
        self,
        generation: int,
        logical_book_id: int,
        api: DeluxeSyncApi,
        profile: dict[str, Any],
    ) -> None:
        details = None
        error: Exception | None = None
        try:
            details = api.get_logical_book(logical_book_id, profile)
        except Exception as caught:
            error = caught

        try:
            self._details_bridge.completed.emit(
                (generation, logical_book_id, details, error)
            )
        except RuntimeError:
            return

    def _linked_details_loaded(self, result: object) -> None:
        generation, logical_book_id, details, error = result
        if generation != self._load_generation:
            return

        if self._details_loading_id == logical_book_id:
            self._details_loading_id = None

        if error is not None:
            if isinstance(error, ApiError):
                self.status_label.setText(_("Could not load linked versions: {error}").format(error=error))
                LOGGER.warning(
                    "Linked versions load failed logical_book_id=%s status=%s error_type=%s",
                    logical_book_id,
                    error.status,
                    type(error).__name__,
                )
            else:
                self.status_label.setText(_("Could not load linked versions."))
                LOGGER.exception(
                    "Unexpected linked versions load failure logical_book_id=%s error_type=%s",
                    logical_book_id,
                    type(error).__name__,
                    exc_info=error,
                )
            self._selection_changed()
            return

        if not isinstance(details, dict):
            self.status_label.setText(_("Could not load linked versions: invalid response."))
            self._selection_changed()
            return

        self._logical_cache[logical_book_id] = details
        self.status_label.setText(self._library_summary)
        self._selection_changed()
        self._open_linked_versions(details)

    def _open_linked_versions(self, details: dict[str, Any]) -> None:
        dialog = LinkedVersionsDialog(details, self)
        center = getattr(self.action, "_center_dialog", None)
        if callable(center):
            center(dialog)
        dialog.exec()
