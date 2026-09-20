"""Compact, lazy-loading configuration landing page for Deluxe Sync."""

from __future__ import annotations

from pathlib import Path

from qt.core import (
    QGroupBox,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QTimer,
    QVBoxLayout,
    QWidget,
)

from calibre_plugins.deluxe_sync.logger import get_logger, log_path
from calibre_plugins.deluxe_sync.settings import (
    get_active_server_profile,
    get_column_mappings,
)


try:
    load_translations()
except NameError:
    def _(text):
        return text


LOGGER = get_logger("config")
MAPPING_KEYS = ("progress", "status", "last_location", "last_sync", "annotations", "vocabulary")


class ConfigWidget(QWidget):
    """Landing page with lazily-created focused configuration subpages."""

    validate_before_accept = True

    LANDING_SIZE = (660, 400)
    LANDING_MINIMUM = (600, 360)
    CONNECTION_SIZE = (1040, 860)
    CONNECTION_MINIMUM = (780, 620)
    COLUMNS_SIZE = (760, 520)
    COLUMNS_MINIMUM = (680, 440)

    def __init__(self, plugin_action):
        super().__init__()
        self.action = plugin_action
        self.connection_page = None
        self.columns_page = None
        self._restart_after_save = False
        self._restart_decision_made = False

        LOGGER.debug(
            "Configuration landing page opened log_file=%s",
            log_path(),
        )

        layout = QVBoxLayout(self)
        self.stack = QStackedWidget()
        self.stack.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Ignored,
        )
        layout.addWidget(self.stack)

        self.landing_page = self._build_landing_page()
        self.stack.addWidget(self.landing_page)

        self._refresh_landing()
        self.stack.setCurrentWidget(self.landing_page)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.stack.currentWidget() is self.landing_page:
            self._resize_for_landing()

    def _build_landing_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        heading = QLabel(_("<b>Deluxe Sync</b>"))
        layout.addWidget(heading)

        intro = QLabel(
            _("Choose the area you want to configure. Connection details and column "
              "mappings are kept on separate pages so this screen stays compact.")
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        server_group = QGroupBox(_("Server Connection"))
        server_layout = QVBoxLayout(server_group)
        self.server_summary_label = QLabel()
        self.server_summary_label.setWordWrap(True)
        server_layout.addWidget(self.server_summary_label)

        self.library_summary_label = QLabel()
        self.library_summary_label.setWordWrap(True)
        server_layout.addWidget(self.library_summary_label)

        server_button = QPushButton(_("Open Server Connection"))
        server_button.clicked.connect(self.show_connection)
        server_layout.addWidget(server_button)
        layout.addWidget(server_group)

        columns_group = QGroupBox(_("Column Mappings"))
        columns_layout = QVBoxLayout(columns_group)
        self.columns_summary_label = QLabel()
        self.columns_summary_label.setWordWrap(True)
        columns_layout.addWidget(self.columns_summary_label)

        columns_button = QPushButton(_("Open Column Mappings"))
        columns_button.clicked.connect(self.show_columns)
        columns_layout.addWidget(columns_button)
        layout.addWidget(columns_group)

        diagnostics = QLabel(_("Diagnostics log: {path}").format(path=log_path()))
        diagnostics.setWordWrap(True)
        layout.addWidget(diagnostics)

        layout.addStretch(1)
        return page

    def _current_library(self) -> tuple[str | None, str | None]:
        gui = getattr(self.action, "gui", None)
        db = getattr(gui, "current_db", None)
        if db is None:
            return None, None

        library_uuid = str(getattr(db, "library_id", "") or "").strip()
        raw_path = str(getattr(db, "library_path", "") or "").strip()
        library_name = Path(raw_path).name if raw_path else None
        return library_uuid or None, library_name or None

    def _local_connection_summary(self) -> tuple[str, str]:
        library_uuid, library_name = self._current_library()
        library_text = (
            _("Current library: {name}").format(name=library_name or _("Calibre Library"))
            if library_uuid
            else _("Current library: No active Calibre library")
        )

        profile = get_active_server_profile()
        if not profile:
            return (
                _("Not connected · authentication required"),
                library_text,
            )

        family = str(profile.get("server_family") or "").strip()
        server_type = str(profile.get("server_type") or "").strip()
        version = str(profile.get("server_version") or "").strip()

        cached_server = " / ".join(part for part in (family, server_type) if part)
        if version:
            cached_server = (
                f"{cached_server} · {version}"
                if cached_server
                else version
            )

        summary = _("Configured · connection not tested this session")
        if cached_server:
            summary = _("{summary}\nLast known server: {server}").format(summary=summary, server=cached_server)

        return summary, library_text

    def _local_columns_summary(self) -> str:
        mappings = get_column_mappings()
        configured = sum(
            1
            for key in MAPPING_KEYS
            if str(mappings.get(key) or "").strip()
        )
        return _("{configured} of {total} mappings configured").format(configured=configured, total=len(MAPPING_KEYS))

    def _refresh_landing(self) -> None:
        if self.connection_page is not None:
            self.server_summary_label.setText(self.connection_page.summary_text())
            library_name = (
                self.connection_page.library_name
                or _("No active Calibre library")
            )
            self.library_summary_label.setText(
                _("Current library: {name}").format(name=library_name)
            )
        else:
            connection_summary, library_summary = self._local_connection_summary()
            self.server_summary_label.setText(connection_summary)
            self.library_summary_label.setText(library_summary)

        if self.columns_page is not None:
            self.columns_summary_label.setText(self.columns_page.summary_text())
        else:
            self.columns_summary_label.setText(self._local_columns_summary())

    def _ensure_connection_page(self):
        if self.connection_page is None:
            from calibre_plugins.deluxe_sync.dialogs.connection import ConnectionPage

            LOGGER.debug("Creating Server Connection page on first navigation")
            self.connection_page = ConnectionPage(
                self.action,
                on_back=self.show_landing,
                on_status_changed=self._refresh_landing,
                on_layout_changed=lambda: QTimer.singleShot(0, self._resize_for_connection),
            )
            self.stack.addWidget(self.connection_page)
        return self.connection_page

    def _ensure_columns_page(self):
        if self.columns_page is None:
            from calibre_plugins.deluxe_sync.dialogs.columns import ColumnMappingsPage

            LOGGER.debug("Creating Column Mappings page on first navigation")
            self.columns_page = ColumnMappingsPage(
                self.action,
                on_back=self.show_landing,
                on_mapping_changed=self._refresh_landing,
            )
            self.stack.addWidget(self.columns_page)
        return self.columns_page

    def _center_dialog(self, window) -> None:
        target_geometry = None
        gui = getattr(self.action, "gui", None)
        if gui is not None and gui is not window and hasattr(gui, "frameGeometry"):
            try:
                target_geometry = gui.frameGeometry()
            except RuntimeError:
                target_geometry = None

        if target_geometry is None:
            screen = window.screen()
            if screen is not None:
                target_geometry = screen.availableGeometry()

        if target_geometry is None:
            return

        frame = window.frameGeometry()
        frame.moveCenter(target_geometry.center())
        window.move(frame.topLeft())

    def _resize_dialog(
        self,
        target: tuple[int, int],
        minimum: tuple[int, int],
        page_name: str,
    ) -> None:
        window = self.window()
        if window is self:
            return

        window.setMinimumSize(*minimum)
        target_width, target_height = target
        screen = window.screen()
        if screen is not None:
            available = screen.availableGeometry()
            target_width = min(target_width, max(minimum[0], available.width() - 40))
            target_height = min(target_height, max(minimum[1], available.height() - 60))
        target = (target_width, target_height)
        window.resize(*target)
        self._center_dialog(window)
        LOGGER.debug(
            "Configuration page=%s dialog_width=%s dialog_height=%s dialog_x=%s dialog_y=%s",
            page_name,
            target[0],
            target[1],
            window.x(),
            window.y(),
        )

    def _resize_for_landing(self) -> None:
        self._resize_dialog(
            self.LANDING_SIZE,
            self.LANDING_MINIMUM,
            "landing",
        )

    def _resize_for_connection(self) -> None:
        target_width, target_height = self.CONNECTION_SIZE
        if self.connection_page is not None:
            hint = self.connection_page.sizeHint()
            target_width = max(target_width, hint.width() + 40)
            target_height = max(target_height, hint.height() + 80)
        self._resize_dialog(
            (target_width, target_height),
            self.CONNECTION_MINIMUM,
            "server_connection",
        )

    def show_landing(self) -> None:
        self._refresh_landing()
        self.stack.setCurrentWidget(self.landing_page)
        QTimer.singleShot(0, self._resize_for_landing)

    def show_connection(self) -> None:
        page = self._ensure_connection_page()
        page.schedule_auto_test(1000)
        self.stack.setCurrentWidget(page)
        QTimer.singleShot(0, self._resize_for_connection)

    def show_columns(self) -> None:
        page = self._ensure_columns_page()
        self.stack.setCurrentWidget(page)
        QTimer.singleShot(
            0,
            lambda: self._resize_dialog(
                self.COLUMNS_SIZE,
                self.COLUMNS_MINIMUM,
                "column_mappings",
            ),
        )

    def _restart_pending(self) -> bool:
        gui = getattr(self.action, "gui", None)
        calibre_pending = bool(
            getattr(gui, "must_restart_before_config", False)
        )
        page_pending = bool(
            self.columns_page is not None
            and self.columns_page.restart_required
        )
        return calibre_pending or page_pending

    def _prompt_restart_choice(self) -> str:
        box = QMessageBox(self.window())
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(_("Restart Calibre Required"))
        box.setText(
            _(
                "A new Calibre custom column was created. Calibre must restart "
                "before the new column can be used by Deluxe Sync or shown "
                "everywhere."
            )
        )
        box.setInformativeText(
            _(
                "Choose Restart Calibre to save these settings and restart now, "
                "Close to save and close without restarting, or Cancel to keep "
                "this configuration window open."
            )
        )

        restart_button = box.addButton(
            _("Restart Calibre"),
            QMessageBox.ButtonRole.AcceptRole,
        )
        close_button = box.addButton(
            _("Close"),
            QMessageBox.ButtonRole.ActionRole,
        )
        cancel_button = box.addButton(
            _("Cancel"),
            QMessageBox.ButtonRole.RejectRole,
        )
        box.setDefaultButton(restart_button)
        box.setEscapeButton(cancel_button)
        box.exec()

        clicked = box.clickedButton()
        if clicked is restart_button:
            return "restart"
        if clicked is close_button:
            return "close"
        return "cancel"

    def validate(self) -> bool:
        if self._restart_decision_made:
            return True

        self._restart_after_save = False
        if not self._restart_pending():
            return True

        choice = self._prompt_restart_choice()
        if choice == "cancel":
            return False

        self._restart_after_save = choice == "restart"
        self._restart_decision_made = True
        return True

    def _restart_calibre(self) -> None:
        gui = getattr(self.action, "gui", None)
        quit_calibre = getattr(gui, "quit", None)
        if callable(quit_calibre):
            LOGGER.info("Restarting Calibre after custom-column creation")
            quit_calibre(restart=True)

    def save_settings(self) -> None:
        """Persist only settings from subpages that were actually opened."""

        if self.connection_page is not None:
            self.connection_page.save_settings()
        if self.columns_page is not None:
            self.columns_page.save_settings()

        restart_after_save = self._restart_after_save
        self._restart_after_save = False
        self._restart_decision_made = False
        if restart_after_save:
            self._restart_calibre()
