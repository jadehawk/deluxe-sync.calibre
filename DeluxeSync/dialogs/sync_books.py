"""Stage 4 selected-book progress synchronization dialog."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Thread
from typing import Any

from qt.core import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QObject,
    QPushButton,
    QStandardItem,
    QStandardItemModel,
    QTableView,
    QTimer,
    QVBoxLayout,
    pyqtSignal,
)

from calibre_plugins.deluxe_sync.annotation_sync import (
    AnnotationArchive,
    apply_annotation_archive,
    render_annotation_archive,
)
from calibre_plugins.deluxe_sync.api import (
    ApiError,
    AuthorizationError,
    CapabilityError,
    DeluxeSyncApi,
)
from calibre_plugins.deluxe_sync.bindings import get_library_bindings, save_binding_record
from calibre_plugins.deluxe_sync.incremental_sync import (
    COMPONENT_ANNOTATIONS,
    COMPONENT_METADATA,
    COMPONENT_PROGRESS,
    COMPONENT_VOCABULARY,
    change_components_for_binding,
    normalize_change_cursor,
    read_change_window,
    refresh_components_for_binding,
    update_change_tracking,
)
from calibre_plugins.deluxe_sync.logger import get_logger
from calibre_plugins.deluxe_sync.models import CalibreBook, selected_calibre_books
from calibre_plugins.deluxe_sync.progress_sync import (
    ProgressSnapshot,
    apply_progress_snapshot,
    snapshot_from_progress,
)
from calibre_plugins.deluxe_sync.vocabulary_sync import (
    VocabularyArchive,
    apply_vocabulary_archive,
    render_vocabulary_archive,
)
from calibre_plugins.deluxe_sync.settings import (
    get_active_server_profile,
    get_active_server_profile_id,
    get_column_mappings,
)


try:
    load_translations()
except NameError:
    def _(text):
        return text

    def ngettext(singular, plural, count):
        return singular if count == 1 else plural


LOGGER = get_logger("progress-sync")

_HEADERS = (
    _("Calibre title"),
    _("Result"),
    _("Progress"),
    _("Status"),
)

_PROGRESS_MAPPING_KEYS = ("progress", "status", "last_location", "last_sync")
_MAPPING_DATATYPES = {
    "progress": frozenset({"float"}),
    "status": frozenset({"text"}),
    "last_location": frozenset({"text", "comments"}),
    "last_sync": frozenset({"datetime"}),
    "annotations": frozenset({"comments"}),
    "vocabulary": frozenset({"comments"}),
}


@dataclass(frozen=True)
class _FetchResult:
    book: CalibreBook
    snapshot: ProgressSnapshot | None
    error: str = ""
    archive: AnnotationArchive | None = None
    annotation_error: str = ""
    vocabulary_archive: VocabularyArchive | None = None
    vocabulary_error: str = ""
    metadata_payload: dict[str, Any] | None = None
    metadata_error: str = ""
    metadata_review_needed: bool = False
    binding: dict[str, Any] | None = None
    change_cursor: int | None = None
    refresh_components: frozenset[str] = frozenset()
    not_on_server: bool = False


class _AsyncBridge(QObject):
    completed = pyqtSignal(object)


class SyncSelectedBooksDialog(QDialog):
    """Pull current server progress into mapped columns for selected bound books."""

    def __init__(
        self,
        plugin_action,
        *,
        auto_sync: bool = False,
        auto_show_completion: bool = False,
        books: list[CalibreBook] | None = None,
        scope_label: str = "",
        parent_dialog: QDialog | None = None,
    ):
        super().__init__(parent_dialog or plugin_action.gui)
        self.action = plugin_action
        self._generation = 0
        self._auto_show_completion = bool(auto_show_completion)
        self._explicit_books = list(books) if books is not None else None
        self._scope_label = str(scope_label or "").strip()
        self._loading = False
        self._library_uuid = ""
        self._bridge = _AsyncBridge(self)
        self._bridge.completed.connect(self._sync_loaded)

        self.setWindowTitle(_("Deluxe Sync — Sync Selected Books"))
        layout = QVBoxLayout(self)

        intro = QLabel(
            _(
                "Pull reading progress, annotation backups, and vocabulary from the server for the currently "
                "selected books that are already linked in Deluxe Sync. Only mapped Calibre "
                "columns are changed; this action never sends progress, metadata, annotations, or vocabulary "
                "to the server."
            )
        )
        intro.setWordWrap(True)
        if self._explicit_books is not None:
            intro.setText(
                _(
                    "Pull reading progress, annotation backups, and vocabulary from the server "
                    "for {scope}. Only linked books participate, only mapped Calibre columns "
                    "are changed, and this action never sends progress, metadata, annotations, "
                    "or vocabulary to the server."
                ).format(scope=self._scope_label or _("this batch"))
            )
        layout.addWidget(intro)

        self.status_label = QLabel(_("Ready."))
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.results_model = QStandardItemModel(0, len(_HEADERS), self)
        self.results_model.setHorizontalHeaderLabels(_HEADERS)
        self.results_table = QTableView()
        self.results_table.setModel(self.results_model)
        self.results_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.results_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.results_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.results_table.setAlternatingRowColors(True)
        self.results_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.results_table, 1)

        controls = QHBoxLayout()
        self.sync_button = QPushButton(_("Sync Selected Books"))
        self.sync_button.clicked.connect(
            lambda: self.sync_selected_books(show_completion=True)
        )
        controls.addWidget(self.sync_button)

        configure_button = QPushButton(_("Configure Columns"))
        configure_button.clicked.connect(self.configure_columns)
        controls.addWidget(configure_button)

        self.result_summary_label = QLabel("")
        self.result_summary_label.setWordWrap(False)
        controls.addWidget(self.result_summary_label, 1)

        close_button = QPushButton(
            _("Back to Dashboard") if parent_dialog is not None else _("Close")
        )
        close_button.clicked.connect(self.accept)
        controls.addWidget(close_button)
        layout.addLayout(controls)

        self.resize(860, 500)
        self._refresh_sync_availability()
        if auto_sync:
            QTimer.singleShot(0, self._auto_sync_if_ready)

    def _mapping_readiness(self) -> tuple[bool, dict[str, str], str]:
        gui = getattr(self.action, "gui", None)
        if bool(getattr(gui, "must_restart_before_config", False)):
            return (
                False,
                {},
                _(
                    "Restart Calibre to finish adding the new custom column before syncing."
                ),
            )

        db = getattr(gui, "current_db", None)
        if db is None:
            return False, {}, _("Open a Calibre library before syncing.")

        mappings = get_column_mappings()
        active = {
            key: str(value or "").strip()
            for key, value in mappings.items()
            if key in _MAPPING_DATATYPES and str(value or "").strip()
        }
        if not active:
            return (
                False,
                {},
                _("Map at least one Deluxe Sync custom column before syncing."),
            )

        try:
            metadata = db.custom_field_metadata(include_composites=False)
        except TypeError:
            metadata = db.custom_field_metadata(False)
        except Exception:
            metadata = {}

        unavailable: list[str] = []
        for key, lookup in active.items():
            info = metadata.get(lookup) if isinstance(metadata, dict) else None
            datatype = (
                str(info.get("datatype") or "").strip()
                if isinstance(info, dict)
                else ""
            )
            if datatype not in _MAPPING_DATATYPES[key]:
                unavailable.append(lookup)

        if unavailable:
            return (
                False,
                {},
                _(
                    "One or more mapped columns are not available in this Calibre "
                    "session. Restart Calibre or review Column Mappings."
                ),
            )

        return True, active, ""

    def _refresh_sync_availability(self, *, update_status: bool = True) -> bool:
        ready, _mappings, message = self._mapping_readiness()
        self.sync_button.setEnabled(ready and not self._loading)
        if update_status and not ready:
            self.status_label.setText(message)
            self._replace_rows([])
        return ready

    def _auto_sync_if_ready(self) -> None:
        if self._refresh_sync_availability():
            self.sync_selected_books(show_completion=self._auto_show_completion)

    def configure_columns(self) -> None:
        self.action.show_config()
        self._refresh_sync_availability()

    def _plugin_version(self) -> str:
        base_plugin = getattr(self.action, "interface_action_base_plugin", None)
        return str(getattr(base_plugin, "version_string", None) or "unknown")

    def _replace_rows(self, rows: list[tuple[str, str, str, str]]) -> None:
        self.results_model.setRowCount(0)
        for row in rows:
            self.results_model.appendRow([QStandardItem(value) for value in row])
        self.results_table.resizeColumnsToContents()
        self.results_table.horizontalHeader().setStretchLastSection(True)

    def sync_selected_books(self, *, show_completion: bool = False) -> None:
        if self._loading:
            return

        self.result_summary_label.setText("")

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
                _("Select one or more Calibre books, then choose Sync Selected Books.")
            )
            self._replace_rows([])
            return

        profile = get_active_server_profile()
        if not profile:
            self.status_label.setText(
                _("Server authentication required. Open Configure Deluxe Sync → Server Connection.")
            )
            self._replace_rows([])
            return

        ready, active_mappings, readiness_message = self._mapping_readiness()
        if not ready:
            self.sync_button.setEnabled(False)
            self.status_label.setText(readiness_message)
            self._replace_rows([])
            return

        server_url = str(profile.get("server_url") or "").strip()
        if not server_url:
            self.status_label.setText(_("Saved server profile is incomplete."))
            self._replace_rows([])
            return

        self._library_uuid = books[0].library_uuid
        profile_id = get_active_server_profile_id()
        bindings = get_library_bindings(self._library_uuid, profile_id)

        bound: list[tuple[CalibreBook, dict[str, Any]]] = []
        skipped_rows: list[tuple[str, str, str, str]] = []
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
                skipped_rows.append(
                    (book.title, _("Not linked"), "", "")
                )
                continue
            bound.append((book, binding))

        if not bound:
            self.status_label.setText(
                _("None of the selected books are linked in Deluxe Sync.")
            )
            self._replace_rows(skipped_rows)
            return

        self._generation += 1
        generation = self._generation
        self._loading = True
        self.sync_button.setText(_("Syncing…"))
        self.sync_button.setEnabled(False)
        self.status_label.setText(
            ngettext(
                "Reading server data for {count} linked book…",
                "Reading server data for {count} linked books…",
                len(bound),
            ).format(count=len(bound))
        )
        self._replace_rows(skipped_rows)

        Thread(
            target=self._load_worker,
            args=(
                generation,
                bound,
                skipped_rows,
                server_url,
                profile,
                active_mappings,
                show_completion,
            ),
            name="DeluxeSyncStage4Progress",
            daemon=True,
        ).start()

    def _load_worker(
        self,
        generation: int,
        bound: list[tuple[CalibreBook, dict[str, Any]]],
        skipped_rows: list[tuple[str, str, str, str]],
        server_url: str,
        profile: dict[str, Any],
        mappings: dict[str, str],
        show_completion: bool,
    ) -> None:
        results: list[_FetchResult] = []
        fatal_error: Exception | None = None
        requested_components: set[str] = set()
        unsupported_components: set[str] = set()
        try:
            api = DeluxeSyncApi(server_url, plugin_version=self._plugin_version())
            capabilities = api.discover_capabilities(profile)
            capability_values = capabilities.get("capabilities")
            capability_values = (
                capability_values if isinstance(capability_values, dict) else {}
            )
            progress_requested = any(
                str(mappings.get(key) or "").strip()
                for key in _PROGRESS_MAPPING_KEYS
            )
            annotations_mapped = bool(str(mappings.get("annotations") or "").strip())
            vocabulary_mapped = bool(str(mappings.get("vocabulary") or "").strip())
            annotations_requested = (
                annotations_mapped
                and capability_values.get("annotations") is True
            )
            vocabulary_requested = (
                vocabulary_mapped
                and capability_values.get("vocabulary_builder") is True
            )

            requested_components: set[str] = set()
            unsupported_components: set[str] = set()
            if progress_requested:
                requested_components.add(COMPONENT_PROGRESS)
            if annotations_requested:
                requested_components.add(COMPONENT_ANNOTATIONS)
            elif annotations_mapped:
                unsupported_components.add(COMPONENT_ANNOTATIONS)
            if vocabulary_requested:
                requested_components.add(COMPONENT_VOCABULARY)
            elif vocabulary_mapped:
                unsupported_components.add(COMPONENT_VOCABULARY)

            journal_supported = capability_values.get("change_journal") is True
            if journal_supported and requested_components:
                requested_components.add(COMPONENT_METADATA)
            library: dict[str, Any] | None = None
            library_loaded = False
            logical_cache: dict[int, dict[str, Any]] = {}
            change_windows: dict[tuple[str, int | None], Any] = {}
            vocabulary_title_cache: dict[str, str | None] = {}
            browseable_documents: frozenset[str] | None = None
            browseable_complete = False
            if capability_values.get("logical_library") is not True:
                server_profile = api.detect_server_profile(profile, capabilities)
                if server_profile in {"crosspoint", "standard"}:
                    try:
                        raw_index = api.get_browseable_documents(profile)
                    except (CapabilityError, ApiError):
                        raw_index = None
                    if isinstance(raw_index, dict):
                        raw_documents = raw_index.get("documents")
                        if isinstance(raw_documents, list):
                            document_ids = {
                                str(item.get("document") or "").strip()
                                for item in raw_documents
                                if isinstance(item, dict)
                                and str(item.get("document") or "").strip()
                            }
                            browseable_documents = frozenset(document_ids)
                            browseable_complete = (
                                server_profile == "standard"
                                or len(raw_documents) < 500
                            )

            def load_library() -> dict[str, Any] | None:
                nonlocal library, library_loaded
                if library_loaded:
                    return library
                library_loaded = True
                if capability_values.get("logical_library") is True:
                    library = api.get_library(
                        profile,
                        capabilities_response=capabilities,
                    )
                return library

            def load_logical_detail(logical_book_id: int) -> dict[str, Any] | None:
                if logical_book_id in logical_cache:
                    return logical_cache[logical_book_id]
                try:
                    detail = api.get_logical_book(logical_book_id, profile)
                except ApiError as error:
                    if error.status == 404:
                        return None
                    raise
                if isinstance(detail, dict):
                    logical_cache[logical_book_id] = detail
                    return detail
                return None

            def logical_member_documents(logical_book_id: int) -> set[str]:
                detail = load_logical_detail(logical_book_id)
                if not isinstance(detail, dict):
                    return set()
                members = detail.get("members")
                if not isinstance(members, list):
                    return set()
                return {
                    str(member.get("document") or "").strip()
                    for member in members
                    if isinstance(member, dict)
                    and str(member.get("document") or "").strip()
                }

            def missing_result(book: CalibreBook, binding: dict[str, Any]) -> _FetchResult:
                return _FetchResult(
                    book=book,
                    snapshot=None,
                    binding=dict(binding),
                    refresh_components=frozenset(),
                    not_on_server=True,
                )

            def is_missing_document_error(error: ApiError) -> bool:
                if error.status == 404:
                    return True
                return (
                    error.status == 403
                    and "field 'document' not provided" in str(error).casefold()
                )

            for book, binding in bound:
                document = str(binding.get("server_document") or "").strip()
                if (
                    browseable_complete
                    and browseable_documents is not None
                    and document not in browseable_documents
                ):
                    results.append(missing_result(book, binding))
                    continue

                snapshot = None
                progress_error = ""
                archive = None
                annotation_error = ""
                vocabulary_archive = None
                vocabulary_error = ""
                metadata_payload = None
                metadata_error = ""
                metadata_review_needed = False
                not_on_server = False
                refresh_components = frozenset(requested_components)
                change_cursor = None

                member_documents: set[str] = {document}
                logical_id = binding.get("logical_book_id")
                membership_changed = False
                if journal_supported:
                    normalized_cursor = normalize_change_cursor(
                        binding.get("last_change_cursor")
                    )
                    cache_key = (
                        "cursor" if normalized_cursor is not None else "baseline",
                        normalized_cursor,
                    )
                    window = change_windows.get(cache_key)
                    if window is None:
                        window = read_change_window(
                            api,
                            profile,
                            binding.get("last_change_cursor"),
                        )
                        change_windows[cache_key] = window
                    change_cursor = window.next_cursor

                    if (
                        window.full_refresh
                        and capability_values.get("logical_library") is True
                    ):
                        try:
                            effective_identity = api.get_effective_progress(
                                document,
                                profile,
                                capabilities_response=capabilities,
                                logical_book_id=logical_id,
                                library_response=load_library(),
                                logical_cache=logical_cache,
                            )
                        except ApiError:
                            effective_identity = None
                        if isinstance(effective_identity, dict):
                            resolved_logical_id = effective_identity.get("logical_book_id")
                            if (
                                not isinstance(resolved_logical_id, int)
                                or isinstance(resolved_logical_id, bool)
                                or resolved_logical_id <= 0
                            ):
                                resolved_logical_id = None
                            if resolved_logical_id != logical_id:
                                membership_changed = True
                                binding = dict(binding)
                                binding["logical_book_id"] = resolved_logical_id
                                logical_id = resolved_logical_id
                            if isinstance(logical_id, int):
                                member_documents.update(
                                    logical_member_documents(logical_id)
                                )

                    for change in window.changes:
                        if str(change.get("entity_type") or "").strip() != "logical_book":
                            continue
                        candidate_key = str(change.get("entity_key") or "").strip()
                        if not candidate_key.isdigit():
                            continue
                        candidate_logical_id = int(candidate_key)
                        if candidate_logical_id <= 0:
                            continue

                        operation = str(change.get("operation") or "").strip()
                        payload = change.get("payload")
                        payload = payload if isinstance(payload, dict) else {}

                        if candidate_logical_id == logical_id:
                            if operation == "dissolve" or (
                                operation == "detach"
                                and str(payload.get("detached_document") or "").strip()
                                == document
                            ):
                                membership_changed = True
                                binding = dict(binding)
                                binding["logical_book_id"] = None
                                logical_id = None
                                member_documents = {document}
                                continue
                            if operation in {"create", "detach"}:
                                current_documents = logical_member_documents(
                                    candidate_logical_id
                                )
                                if document in current_documents:
                                    member_documents.update(current_documents)
                                elif operation == "detach":
                                    membership_changed = True
                                    binding = dict(binding)
                                    binding["logical_book_id"] = None
                                    logical_id = None
                                    member_documents = {document}
                            continue

                        candidate_documents = logical_member_documents(
                            candidate_logical_id
                        )
                        if document not in candidate_documents:
                            continue
                        membership_changed = True
                        logical_id = candidate_logical_id
                        binding = dict(binding)
                        binding["logical_book_id"] = candidate_logical_id
                        member_documents.update(candidate_documents)

                    if (
                        annotations_requested
                        and isinstance(logical_id, int)
                        and not isinstance(logical_id, bool)
                        and logical_id > 0
                    ):
                        annotation_documents = {
                            str(
                                (
                                    change.get("payload")
                                    if isinstance(change.get("payload"), dict)
                                    else {}
                                ).get("document")
                                or ""
                            ).strip()
                            for change in window.changes
                            if str(change.get("entity_type") or "").strip()
                            == "annotation"
                        }
                        if any(
                            changed_document
                            and changed_document not in member_documents
                            for changed_document in annotation_documents
                        ):
                            member_documents.update(
                                logical_member_documents(logical_id)
                            )

                    vocabulary_changes = [
                        change
                        for change in window.changes
                        if str(change.get("entity_type") or "").strip() == "vocabulary"
                    ]
                    vocabulary_titles: dict[str, str] = {}
                    unresolved_vocabulary_change = False
                    effective_title = ""
                    if vocabulary_requested and vocabulary_changes:
                        for change in vocabulary_changes:
                            sync_id = str(change.get("entity_key") or "").strip()
                            if not sync_id:
                                unresolved_vocabulary_change = True
                                continue
                            if sync_id not in vocabulary_title_cache:
                                entry = api.get_vocabulary_entry_for_change(sync_id, profile)
                                vocabulary_title_cache[sync_id] = (
                                    str(entry.get("title") or "").strip()
                                    if isinstance(entry, dict)
                                    else None
                                )
                            title = vocabulary_title_cache.get(sync_id)
                            if title:
                                vocabulary_titles[sync_id] = title
                            else:
                                unresolved_vocabulary_change = True

                        try:
                            effective = api.get_effective_progress(
                                document,
                                profile,
                                capabilities_response=capabilities,
                                logical_book_id=logical_id,
                                library_response=load_library(),
                                logical_cache=logical_cache,
                            )
                        except ApiError:
                            effective = None
                        if isinstance(effective, dict):
                            effective_title = str(effective.get("title") or "").strip()
                        if not effective_title:
                            unresolved_vocabulary_change = True

                    refresh_components = refresh_components_for_binding(
                        window,
                        binding,
                        requested_components,
                        member_documents=member_documents,
                        effective_title=effective_title,
                        vocabulary_titles=vocabulary_titles,
                    )
                    if unresolved_vocabulary_change and vocabulary_requested:
                        refresh_components = frozenset(
                            set(refresh_components) | {COMPONENT_VOCABULARY}
                        )
                    if membership_changed:
                        refresh_components = frozenset(
                            set(refresh_components) | requested_components
                        )
                    if COMPONENT_METADATA in refresh_components:
                        metadata_review_needed = membership_changed or any(
                            COMPONENT_METADATA
                            in change_components_for_binding(
                                change,
                                binding,
                                member_documents=member_documents,
                            )
                            for change in window.changes
                        )

                if COMPONENT_METADATA in refresh_components:
                    try:
                        metadata_payload = api.get_document_metadata(
                            document,
                            profile,
                            capabilities_response=capabilities,
                        )
                        if not isinstance(metadata_payload.get("metadata"), dict):
                            raise ApiError(
                                "Server document-metadata response is invalid."
                            )
                    except ApiError as error:
                        if is_missing_document_error(error):
                            not_on_server = True
                        else:
                            metadata_error = str(error)

                if not_on_server:
                    results.append(missing_result(book, binding))
                    continue

                if COMPONENT_PROGRESS in refresh_components:
                    try:
                        progress_payload = api.get_effective_progress(
                            document,
                            profile,
                            capabilities_response=capabilities,
                            logical_book_id=logical_id,
                            library_response=load_library(),
                            logical_cache=logical_cache,
                        )
                        if progress_payload is None or progress_payload == {}:
                            not_on_server = True
                        else:
                            snapshot = snapshot_from_progress(progress_payload)
                    except ApiError as error:
                        if is_missing_document_error(error):
                            not_on_server = True
                        else:
                            progress_error = str(error)

                if not_on_server:
                    results.append(missing_result(book, binding))
                    continue

                if COMPONENT_ANNOTATIONS in refresh_components:
                    try:
                        annotation_payload = api.get_annotations_for_binding(
                            document,
                            profile,
                            capabilities_response=capabilities,
                            logical_book_id=logical_id,
                            library_response=load_library(),
                            logical_cache=logical_cache,
                        )
                        archive = render_annotation_archive(annotation_payload)
                    except ApiError as error:
                        if is_missing_document_error(error):
                            not_on_server = True
                        else:
                            annotation_error = str(error)

                if not_on_server:
                    results.append(missing_result(book, binding))
                    continue

                if COMPONENT_VOCABULARY in refresh_components:
                    try:
                        vocabulary_payload = api.get_vocabulary_for_binding(
                            document,
                            book.title,
                            profile,
                            capabilities_response=capabilities,
                            logical_book_id=logical_id,
                            library_response=load_library(),
                            logical_cache=logical_cache,
                        )
                        vocabulary_archive = render_vocabulary_archive(vocabulary_payload)
                    except ApiError as error:
                        if is_missing_document_error(error):
                            not_on_server = True
                        else:
                            vocabulary_error = str(error)

                if not_on_server:
                    results.append(missing_result(book, binding))
                    continue

                results.append(
                    _FetchResult(
                        book=book,
                        snapshot=snapshot,
                        error=progress_error,
                        archive=archive,
                        annotation_error=annotation_error,
                        vocabulary_archive=vocabulary_archive,
                        vocabulary_error=vocabulary_error,
                        metadata_payload=metadata_payload,
                        metadata_error=metadata_error,
                        metadata_review_needed=metadata_review_needed,
                        binding=dict(binding),
                        change_cursor=change_cursor,
                        refresh_components=refresh_components,
                    )
                )
        except Exception as error:
            fatal_error = error

        try:
            self._bridge.completed.emit(
                (
                    generation,
                    results,
                    skipped_rows,
                    mappings,
                    frozenset(requested_components),
                    frozenset(unsupported_components),
                    fatal_error,
                    show_completion,
                )
            )
        except RuntimeError:
            return

    def _sync_loaded(self, payload: object) -> None:
        if isinstance(payload, tuple) and len(payload) == 8:
            (
                generation,
                results,
                skipped_rows,
                mappings,
                active_components,
                unsupported_components,
                fatal_error,
                show_completion,
            ) = payload
            active_components = set(active_components)
            unsupported_components = set(unsupported_components)
        else:
            generation, results, skipped_rows, mappings, fatal_error, show_completion = payload
            active_components = set()
            if any(str(mappings.get(key) or "").strip() for key in _PROGRESS_MAPPING_KEYS):
                active_components.add(COMPONENT_PROGRESS)
            if str(mappings.get("annotations") or "").strip():
                active_components.add(COMPONENT_ANNOTATIONS)
            if str(mappings.get("vocabulary") or "").strip():
                active_components.add(COMPONENT_VOCABULARY)
            unsupported_components = set()
        if generation != self._generation:
            return

        self._loading = False
        self._refresh_sync_availability(update_status=False)

        if fatal_error is not None:
            if isinstance(fatal_error, AuthorizationError):
                message = _(
                    "Server authentication was rejected. Reconnect Deluxe Sync and try again."
                )
            else:
                message = _("Could not read server progress: {error}").format(
                    error=fatal_error
                )
            self.status_label.setText(message)
            self.sync_button.setText(_("Try Again"))
            self._replace_rows(skipped_rows)
            return

        gui = getattr(self.action, "gui", None)
        db = getattr(gui, "current_db", None)
        current_library_uuid = str(getattr(db, "library_id", "") or "").strip()
        if db is None or current_library_uuid != self._library_uuid:
            self.status_label.setText(
                _("The active Calibre library changed while progress was being read. Nothing was written.")
            )
            self.sync_button.setText(_("Try Again"))
            return

        rows = list(skipped_rows)
        updated_books = 0
        unchanged_books = 0
        failed_books = 0
        partial_books = 0
        not_on_server_books = 0
        changed_ids: set[int] = set()
        progress_requested = COMPONENT_PROGRESS in active_components
        annotations_requested = COMPONENT_ANNOTATIONS in active_components
        annotation_lookup = str(mappings.get("annotations") or "").strip()
        vocabulary_requested = COMPONENT_VOCABULARY in active_components
        vocabulary_lookup = str(mappings.get("vocabulary") or "").strip()
        requested_components: set[str] = set(active_components)

        for result in results:
            if bool(getattr(result, "not_on_server", False)):
                not_on_server_books += 1
                rows.append((result.book.title, _("Not on server — skipped"), "", ""))
                continue

            if not requested_components:
                rows.append(
                    (
                        result.book.title,
                        _("Skipped — no mapped data is supported by this server"),
                        "",
                        "",
                    )
                )
                continue

            component_errors: list[str] = []
            raw_refresh_components = getattr(result, "refresh_components", None)
            change_cursor = getattr(result, "change_cursor", None)
            if raw_refresh_components is None or (
                not raw_refresh_components and change_cursor is None
            ):
                refresh_components = set(requested_components)
            else:
                refresh_components = set(raw_refresh_components)
            component_success = not refresh_components
            successful_components: set[str] = set()
            failed_components: set[str] = set()
            changed = False
            snapshot = getattr(result, "snapshot", None)
            archive = getattr(result, "archive", None)
            vocabulary_archive = getattr(result, "vocabulary_archive", None)
            metadata_payload = getattr(result, "metadata_payload", None)
            metadata_review_needed = bool(
                getattr(result, "metadata_review_needed", False)
            )

            if COMPONENT_METADATA in refresh_components:
                metadata_error = str(getattr(result, "metadata_error", "") or "")
                if metadata_error:
                    failed_components.add(COMPONENT_METADATA)
                    component_errors.append(
                        _("Metadata: {error}").format(error=metadata_error)
                    )
                elif (
                    not isinstance(metadata_payload, dict)
                    or not isinstance(metadata_payload.get("metadata"), dict)
                ):
                    failed_components.add(COMPONENT_METADATA)
                    component_errors.append(_("Metadata: no server result"))
                else:
                    component_success = True
                    successful_components.add(COMPONENT_METADATA)

            if COMPONENT_PROGRESS in refresh_components:
                if getattr(result, "error", ""):
                    failed_components.add(COMPONENT_PROGRESS)
                    component_errors.append(
                        _("Progress: {error}").format(error=result.error)
                    )
                elif snapshot is None:
                    failed_components.add(COMPONENT_PROGRESS)
                    component_errors.append(_("Progress: no server result"))
                else:
                    try:
                        applied = apply_progress_snapshot(
                            db,
                            result.book.book_id,
                            mappings,
                            snapshot,
                        )
                    except Exception as error:
                        failed_components.add(COMPONENT_PROGRESS)
                        LOGGER.warning(
                            "Stage 4 Calibre write failed book_id=%s error_type=%s",
                            result.book.book_id,
                            type(error).__name__,
                        )
                        component_errors.append(
                            _("Progress column write failed: {error_type}").format(
                                error_type=type(error).__name__
                            )
                        )
                    else:
                        component_success = True
                        successful_components.add(COMPONENT_PROGRESS)
                        if applied.changed_fields:
                            changed = True
                            changed_ids.update(applied.changed_book_ids)

            if COMPONENT_ANNOTATIONS in refresh_components:
                annotation_error = str(getattr(result, "annotation_error", "") or "")
                if annotation_error:
                    failed_components.add(COMPONENT_ANNOTATIONS)
                    component_errors.append(
                        _("Annotations: {error}").format(error=annotation_error)
                    )
                elif archive is None:
                    failed_components.add(COMPONENT_ANNOTATIONS)
                    component_errors.append(_("Annotations: no server result"))
                else:
                    try:
                        annotation_applied = apply_annotation_archive(
                            db,
                            result.book.book_id,
                            annotation_lookup,
                            archive,
                        )
                    except Exception as error:
                        failed_components.add(COMPONENT_ANNOTATIONS)
                        LOGGER.warning(
                            "Stage 7 annotation archive write failed book_id=%s error_type=%s",
                            result.book.book_id,
                            type(error).__name__,
                        )
                        component_errors.append(
                            _("Annotation column write failed: {error_type}").format(
                                error_type=type(error).__name__
                            )
                        )
                    else:
                        component_success = True
                        successful_components.add(COMPONENT_ANNOTATIONS)
                        if annotation_applied.changed:
                            changed = True
                            changed_ids.update(annotation_applied.changed_book_ids)

            if COMPONENT_VOCABULARY in refresh_components:
                vocabulary_error = str(getattr(result, "vocabulary_error", "") or "")
                if vocabulary_error:
                    failed_components.add(COMPONENT_VOCABULARY)
                    component_errors.append(
                        _("Vocabulary: {error}").format(error=vocabulary_error)
                    )
                elif vocabulary_archive is None:
                    failed_components.add(COMPONENT_VOCABULARY)
                    component_errors.append(_("Vocabulary: no server result"))
                else:
                    try:
                        vocabulary_applied = apply_vocabulary_archive(
                            db,
                            result.book.book_id,
                            vocabulary_lookup,
                            vocabulary_archive,
                        )
                    except Exception as error:
                        failed_components.add(COMPONENT_VOCABULARY)
                        LOGGER.warning(
                            "Stage 8 vocabulary archive write failed book_id=%s error_type=%s",
                            result.book.book_id,
                            type(error).__name__,
                        )
                        component_errors.append(
                            _("Vocabulary column write failed: {error_type}").format(
                                error_type=type(error).__name__
                            )
                        )
                    else:
                        component_success = True
                        successful_components.add(COMPONENT_VOCABULARY)
                        if vocabulary_applied.changed:
                            changed = True
                            changed_ids.update(vocabulary_applied.changed_book_ids)

            binding = getattr(result, "binding", None)
            if (
                isinstance(binding, dict)
                and isinstance(change_cursor, int)
                and not isinstance(change_cursor, bool)
            ):
                try:
                    tracked_binding = update_change_tracking(
                        binding,
                        cursor=change_cursor,
                        successful_components=successful_components,
                        failed_components=failed_components,
                    )
                    save_binding_record(
                        tracked_binding,
                        str(binding.get("server_profile_id") or "").strip() or None,
                    )
                except Exception as error:
                    LOGGER.warning(
                        "Stage 9 cursor persistence failed book_id=%s error_type=%s",
                        result.book.book_id,
                        type(error).__name__,
                    )
                    component_errors.append(
                        _("Incremental sync state could not be saved; this book will be checked again.")
                    )

            progress_text = f"{snapshot.progress:.1f}%" if snapshot is not None else ""
            status_text = self._status_display(snapshot.status) if snapshot is not None else ""

            if not component_success:
                failed_books += 1
                detail = "; ".join(component_errors) or _("No sync result")
                rows.append(
                    (result.book.title, _("Error: {error}").format(error=detail), progress_text, status_text)
                )
                continue

            if changed:
                updated_books += 1
                result_text = _("Updated")
            else:
                unchanged_books += 1
                result_text = _("Already current")

            if (
                metadata_review_needed
                and COMPONENT_METADATA in successful_components
            ):
                result_text += _(" · Server metadata changed — review Sync Preview")
            if annotations_requested and archive is not None:
                result_text += ngettext(
                    " · {count} annotation",
                    " · {count} annotations",
                    archive.count,
                ).format(count=archive.count)
            if vocabulary_requested and vocabulary_archive is not None:
                result_text += ngettext(
                    " · {count} word",
                    " · {count} words",
                    vocabulary_archive.count,
                ).format(count=vocabulary_archive.count)
            if component_errors:
                partial_books += 1
                result_text += _(" · Warning: {error}").format(
                    error="; ".join(component_errors)
                )

            rows.append(
                (
                    result.book.title,
                    result_text,
                    progress_text,
                    status_text,
                )
            )

        if changed_ids:
            try:
                model = gui.library_view.model()
                model.refresh_ids(tuple(sorted(changed_ids)))
            except Exception as error:
                LOGGER.debug(
                    "Calibre library row refresh failed error_type=%s",
                    type(error).__name__,
                )

        self._replace_rows(rows)
        if not_on_server_books:
            summary = _(
                "Sync complete: {updated} updated, {unchanged} already current, "
                "{skipped} not linked, {missing} not on server, {failed} failed."
            ).format(
                updated=updated_books,
                unchanged=unchanged_books,
                skipped=len(skipped_rows),
                missing=not_on_server_books,
                failed=failed_books,
            )
        else:
            summary = _(
                "Sync complete: {updated} updated, {unchanged} already current, "
                "{skipped} not linked, {failed} failed."
            ).format(
                updated=updated_books,
                unchanged=unchanged_books,
                skipped=len(skipped_rows),
                failed=failed_books,
            )
        if unsupported_components:
            labels = []
            if COMPONENT_ANNOTATIONS in unsupported_components:
                labels.append(_("Annotations"))
            if COMPONENT_VOCABULARY in unsupported_components:
                labels.append(_("Vocabulary"))
            if labels:
                summary += _(" Unsupported server data skipped: {items}.").format(
                    items=", ".join(labels)
                )
        if partial_books:
            summary += _(" {count} completed with warnings.").format(count=partial_books)
        self.status_label.setText(_("Sync complete."))
        self.sync_button.setText(_("Sync Again"))
        self.result_summary_label.setText(summary)
        if show_completion:
            self._show_completion_summary(summary)

    def _show_completion_summary(self, summary: str) -> None:
        QMessageBox.information(self, _("Sync Complete"), summary)

    @staticmethod
    def _status_display(status: str) -> str:
        if status == "Finished":
            return _("Finished")
        if status == "Reading":
            return _("Reading")
        return _("Not started")
