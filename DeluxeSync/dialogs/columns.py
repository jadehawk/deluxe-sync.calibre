"""Calibre custom-column mapping configuration for Deluxe Sync."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from calibre.gui2.preferences.create_custom_column import CreateNewCustomColumn
from qt.core import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from calibre_plugins.deluxe_sync.logger import get_logger
from calibre_plugins.deluxe_sync.settings import (
    get_column_mappings,
    set_column_mappings,
)


try:
    load_translations()
except NameError:
    def _(text):
        return text


LOGGER = get_logger("columns")


@dataclass(frozen=True)
class MappingSpec:
    key: str
    label: str
    help_text: str
    datatypes: frozenset[str]
    create_lookup: str
    create_heading: str
    create_datatype: str


MAPPING_SPECS = (
    MappingSpec(
        "progress",
        _("Reading progress"),
        _("Shows the book's reading percentage."),
        frozenset({"float"}),
        "#ds_progress",
        _("DS Progress"),
        "float",
    ),
    MappingSpec(
        "status",
        _("Reading status"),
        _("Shows Not started, Reading, or Finished."),
        frozenset({"text"}),
        "#ds_status",
        _("DS Status"),
        "text",
    ),
    MappingSpec(
        "last_location",
        _("Last location"),
        _("Shows where you last stopped reading."),
        frozenset({"text", "comments"}),
        "#ds_last_location",
        _("DS Last Location"),
        "comments",
    ),
    MappingSpec(
        "last_sync",
        _("Last sync"),
        _("Shows when progress was last updated."),
        frozenset({"datetime"}),
        "#ds_last_sync",
        _("DS Last Sync"),
        "datetime",
    ),
    MappingSpec(
        "annotations",
        _("Annotations & highlights"),
        _("Backs up server highlights and notes as HTML."),
        frozenset({"comments"}),
        "#ds_annotations",
        _("DS Annotations"),
        "comments",
    ),
)

MAPPING_SPECS += (

    MappingSpec(
        "vocabulary",
        _("Vocabulary"),
        _("Backs up words captured while reading as HTML."),
        frozenset({"comments"}),
        "#ds_vocabulary",
        _("DS Vocabulary"),
        "comments",
    ),
)

SPEC_BY_KEY = {spec.key: spec for spec in MAPPING_SPECS}
CREATE_COLUMN_SENTINEL = "__create_deluxe_sync_column__"


class ColumnMappingsPage(QWidget):
    """Persist mappings without enabling any book synchronization yet."""

    def __init__(
        self,
        plugin_action,
        *,
        on_back: Callable[[], None],
        on_mapping_changed: Callable[[], None] | None = None,
    ):
        super().__init__()
        self.action = plugin_action
        self.on_back = on_back
        self.on_mapping_changed = on_mapping_changed
        self.combos: dict[str, QComboBox] = {}
        self._last_real_selection: dict[str, str] = {}
        self._saved_mappings = get_column_mappings()
        self._custom_column_creator = None
        self.restart_required = False

        layout = QVBoxLayout(self)

        header_row = QHBoxLayout()
        back_button = QPushButton(_("← Back"))
        back_button.clicked.connect(self.on_back)
        header_row.addWidget(back_button)

        heading = QLabel(_("<b>Column Mappings</b>"))
        header_row.addWidget(heading)
        header_row.addStretch(1)
        layout.addLayout(header_row)

        intro = QLabel(
            _("Choose which Calibre columns Deluxe Sync should use. You can select an "
              "existing column or create the recommended one.")
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        mapping_group = QGroupBox(_("Server data → Calibre custom columns"))
        mapping_layout = QFormLayout(mapping_group)
        mapping_layout.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )

        available_columns = self._available_columns()
        for spec in MAPPING_SPECS:
            combo = QComboBox()
            combo.setMinimumWidth(430)
            combo.addItem(
                _("Create new Deluxe Sync column ({lookup})").format(lookup=spec.create_lookup),
                CREATE_COLUMN_SENTINEL,
            )
            combo.addItem(_("Not mapped"), "")

            compatible = [
                column
                for column in available_columns
                if column["datatype"] in spec.datatypes
            ]
            for column in compatible:
                key = column["key"]
                name = column["name"]
                datatype = column["datatype"]
                combo.addItem(f"{key} — {name} ({datatype})", key)

            saved_key = str(self._saved_mappings.get(spec.key) or "")
            saved_index = combo.findData(saved_key) if saved_key else combo.findData("")
            if saved_key and saved_index < 0:
                combo.addItem(_("{key} — missing/incompatible").format(key=saved_key), saved_key)
                saved_index = combo.count() - 1
            if saved_index < 0:
                saved_index = combo.findData("")

            combo.setCurrentIndex(saved_index)
            self._last_real_selection[spec.key] = str(combo.currentData() or "")
            combo.currentIndexChanged.connect(
                lambda index, mapping_key=spec.key: self._mapping_selection_changed(
                    mapping_key,
                    index,
                )
            )
            self.combos[spec.key] = combo

            label = QLabel(_("{label}:").format(label=spec.label))
            label.setToolTip(spec.help_text)
            combo.setToolTip(spec.help_text)
            mapping_layout.addRow(label, combo)

        self.creation_status_label = QLabel("")
        self.creation_status_label.setWordWrap(True)
        mapping_layout.addRow(self.creation_status_label)

        layout.addWidget(mapping_group)

        note = QLabel(
            _("Choose a column for each item, or leave it Not mapped. If you create a new "
              "column, Calibre must restart before Deluxe Sync can use it.")
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        reset_button = QPushButton(_("Clear Mappings"))
        reset_button.clicked.connect(self.clear_mappings)
        button_row.addWidget(reset_button)
        layout.addLayout(button_row)

        layout.addStretch(1)

    def _current_db(self):
        gui = getattr(self.action, "gui", None)
        return getattr(gui, "current_db", None)

    def _get_column_creator(self):
        if self._custom_column_creator is not None:
            return self._custom_column_creator

        gui = getattr(self.action, "gui", None)
        if gui is None:
            return None

        try:
            self._custom_column_creator = CreateNewCustomColumn(gui)
        except Exception as error:
            LOGGER.warning(
                "Unable to initialize Calibre custom-column creator error_type=%s",
                type(error).__name__,
            )
            return None
        return self._custom_column_creator

    def _column_metadata(self) -> dict:
        metadata: dict = {}
        db = self._current_db()
        if db is None:
            LOGGER.debug("Column mapping page opened without an active Calibre library")
            return metadata

        try:
            current = db.custom_field_metadata(include_composites=False)
        except TypeError:
            current = db.custom_field_metadata(False)
        except Exception as error:
            LOGGER.warning(
                "Unable to enumerate Calibre custom columns error_type=%s",
                type(error).__name__,
            )
            current = {}

        if isinstance(current, dict):
            metadata.update(current)

        if self._custom_column_creator is not None:
            try:
                creator_columns = self._custom_column_creator.current_columns()
            except Exception as error:
                LOGGER.warning(
                    "Unable to refresh newly-created custom columns error_type=%s",
                    type(error).__name__,
                )
            else:
                if isinstance(creator_columns, dict):
                    metadata.update(creator_columns)

        return metadata

    def _available_columns(self) -> list[dict[str, str]]:
        metadata = self._column_metadata()
        columns: list[dict[str, str]] = []
        for key, info in metadata.items():
            if not isinstance(info, dict):
                continue
            datatype = str(info.get("datatype") or "").strip()
            if not datatype:
                continue
            columns.append(
                {
                    "key": str(key),
                    "name": str(info.get("name") or key),
                    "datatype": datatype,
                }
            )

        columns.sort(key=lambda item: (item["name"].casefold(), item["key"].casefold()))
        LOGGER.debug("Enumerated %s Calibre custom columns", len(columns))
        return columns

    def _find_column(self, lookup_key: str) -> dict[str, str] | None:
        target = lookup_key.casefold()
        for column in self._available_columns():
            if column["key"].casefold() == target:
                return column
        return None

    def _select_mapping_column(
        self,
        spec: MappingSpec,
        lookup_key: str,
        *,
        name: str | None = None,
        datatype: str | None = None,
    ) -> bool:
        combo = self.combos[spec.key]
        index = combo.findData(lookup_key)
        if index >= 0:
            combo.setCurrentIndex(index)
            return True

        column = self._find_column(lookup_key)
        resolved_name = name or (column["name"] if column else spec.create_heading)
        resolved_datatype = datatype or (
            column["datatype"] if column else spec.create_datatype
        )
        if resolved_datatype not in spec.datatypes:
            return False

        combo.addItem(
            f"{lookup_key} — {resolved_name} ({resolved_datatype})",
            lookup_key,
        )
        combo.setCurrentIndex(combo.count() - 1)
        return True

    def _restore_mapping_selection(self, mapping_key: str, lookup_key: str) -> None:
        combo = self.combos[mapping_key]
        index = combo.findData(lookup_key)
        if index < 0:
            index = combo.findData("")
        combo.blockSignals(True)
        combo.setCurrentIndex(index)
        combo.blockSignals(False)

    def _mapping_selection_changed(self, mapping_key: str, index: int) -> None:
        spec = SPEC_BY_KEY.get(mapping_key)
        if spec is None:
            return

        combo = self.combos[mapping_key]
        selected = combo.itemData(index)

        if selected == CREATE_COLUMN_SENTINEL:
            previous = self._last_real_selection.get(mapping_key, "")
            if not self.create_custom_column(mapping_key):
                self._restore_mapping_selection(mapping_key, previous)
            return

        self._last_real_selection[mapping_key] = str(selected or "")
        self._mapping_changed(index)

    def _set_creation_status(self, message: str) -> None:
        self.creation_status_label.setText(message)

    def create_custom_column(self, mapping_key: str) -> bool:
        spec = SPEC_BY_KEY.get(mapping_key)
        if spec is None:
            return False

        existing = self._find_column(spec.create_lookup)
        if existing is not None:
            if self._select_mapping_column(
                spec,
                existing["key"],
                name=existing["name"],
                datatype=existing["datatype"],
            ):
                message = (
                    _("{key} already exists and is now selected for {label}.").format(
                    key=existing["key"],
                    label=spec.label,
                )
                )
                self._set_creation_status(message)
                LOGGER.info(
                    "Recommended custom column already exists lookup=%s mapping=%s",
                    existing["key"],
                    spec.key,
                )
                return True

            self._set_creation_status(
                _("{key} already exists but its type ({datatype}) is not compatible with {label}.").format(
                    key=existing["key"],
                    datatype=existing["datatype"],
                    label=spec.label,
                )
            )
            LOGGER.warning(
                "Recommended custom column exists with incompatible type lookup=%s "
                "datatype=%s mapping=%s",
                existing["key"],
                existing["datatype"],
                spec.key,
            )
            return False

        creator = self._get_column_creator()
        if creator is None:
            self._set_creation_status(
                _("Calibre's custom-column creator is not available for this library.")
            )
            return False

        LOGGER.info(
            "Create custom column requested lookup=%s heading=%r datatype=%s mapping=%s",
            spec.create_lookup,
            spec.create_heading,
            spec.create_datatype,
            spec.key,
        )
        display = {
            "description": (
                _("Deluxe Sync {label}. Created by the Deluxe Sync plugin.").format(label=spec.label)
            )
        }
        if spec.key in {"annotations", "vocabulary"}:
            display.update({
                "heading_position": "hide",
                "interpret_as": "html",
            })

        try:
            result = creator.create_column(
                lookup_name=spec.create_lookup,
                column_heading=spec.create_heading,
                datatype=spec.create_datatype,
                is_multiple=False,
                display=display,
                generate_unused_lookup_name=False,
                freeze_lookup_name=True,
            )
        except Exception as error:
            LOGGER.warning(
                "Custom column creation failed lookup=%s error_type=%s",
                spec.create_lookup,
                type(error).__name__,
            )
            self._set_creation_status(
                _("Could not create {lookup}: {error_type}.").format(lookup=spec.create_lookup, error_type=type(error).__name__)
            )
            return False

        if not result:
            self._set_creation_status(_("Could not create {lookup}.").format(lookup=spec.create_lookup))
            return False

        result_code = result[0]
        result_detail = result[1] if len(result) > 1 else ""

        if result_code == CreateNewCustomColumn.Result.COLUMN_ADDED:
            created_key = str(result_detail or spec.create_lookup)
            self._select_mapping_column(
                spec,
                created_key,
                name=spec.create_heading,
                datatype=spec.create_datatype,
            )
            self.restart_required = True
            self._set_creation_status(
                _("Created {key} and selected it for {label}. Calibre must restart before "
                  "the new column can be used. When you close Preferences, Deluxe Sync "
                  "will offer Restart Calibre, Close, or Cancel.").format(
                    key=created_key,
                    label=spec.label,
                )
            )
            LOGGER.info(
                "Custom column created lookup=%s datatype=%s mapping=%s "
                "restart_required=true",
                created_key,
                spec.create_datatype,
                spec.key,
            )
            return True

        if result_code == CreateNewCustomColumn.Result.DUPLICATE_KEY:
            duplicate = self._find_column(spec.create_lookup)
            if duplicate is not None and self._select_mapping_column(
                spec,
                duplicate["key"],
                name=duplicate["name"],
                datatype=duplicate["datatype"],
            ):
                self._set_creation_status(
                    _("{key} already exists and is now selected for {label}.").format(
                    key=duplicate["key"],
                    label=spec.label,
                )
                )
                return True

        if result_code == CreateNewCustomColumn.Result.CANCELED:
            self._set_creation_status(_("Creation of {lookup} was canceled.").format(lookup=spec.create_lookup))
            return False

        if result_code == CreateNewCustomColumn.Result.MUST_RESTART:
            self.restart_required = True
            self._set_creation_status(
                _("Calibre requires a restart before another custom-column change. "
                  "Close Preferences and restart Calibre.")
            )
            return False

        result_name = getattr(result_code, "name", str(result_code))
        self._set_creation_status(
            _("Calibre did not create {lookup}: {result}.").format(lookup=spec.create_lookup, result=result_name)
        )
        LOGGER.warning(
            "Custom column not created lookup=%s result=%s",
            spec.create_lookup,
            result_name,
        )
        return False

    def _mapping_changed(self, _index: int) -> None:
        if self.on_mapping_changed is not None:
            self.on_mapping_changed()

    def current_mappings(self) -> dict[str, str]:
        mappings: dict[str, str] = {}
        for spec in MAPPING_SPECS:
            selected = self.combos[spec.key].currentData()
            if selected == CREATE_COLUMN_SENTINEL:
                selected = self._last_real_selection.get(spec.key, "")
            mappings[spec.key] = str(selected or "")
        return mappings

    def summary_text(self) -> str:
        mappings = self.current_mappings()
        configured = sum(1 for value in mappings.values() if value)
        return _("{configured} of {total} mappings configured").format(configured=configured, total=len(MAPPING_SPECS))

    def clear_mappings(self) -> None:
        for spec in MAPPING_SPECS:
            combo = self.combos[spec.key]
            index = combo.findData("")
            combo.setCurrentIndex(index)
            self._last_real_selection[spec.key] = ""
        LOGGER.debug("Column mappings cleared in configuration draft")
        self._mapping_changed(0)

    def save_settings(self) -> None:
        mappings = self.current_mappings()
        set_column_mappings(mappings)
        LOGGER.info(
            "Column mappings saved configured=%s total=%s",
            sum(1 for value in mappings.values() if value),
            len(MAPPING_SPECS),
        )
