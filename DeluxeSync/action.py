"""Interface action for Deluxe Sync."""

from calibre.gui2.actions import InterfaceAction
from qt.core import QDialog, QIcon, QPixmap

from calibre_plugins.deluxe_sync.dialogs.about import AboutDialog
from calibre_plugins.deluxe_sync.dialogs.dashboard import DashboardDialog
from calibre_plugins.deluxe_sync.dialogs.library import LibraryDialog
from calibre_plugins.deluxe_sync.dialogs.match_books import MatchBooksDialog
from calibre_plugins.deluxe_sync.dialogs.sync_books import SyncSelectedBooksDialog
from calibre_plugins.deluxe_sync.dialogs.sync_preview import SyncPreviewDialog
from calibre_plugins.deluxe_sync.logger import get_logger, log_path


try:
    load_translations()
except NameError:
    def _(text):
        return text


LOGGER = get_logger("action")


class DeluxeSyncAction(InterfaceAction):
    """Toolbar/menu action for the staged Deluxe Sync plugin."""

    name = "Deluxe Sync"
    action_spec = (
        name,
        None,
        _("Open the Deluxe Sync dashboard."),
        None,
    )
    action_add_menu = True
    action_type = "current"

    def genesis(self):
        version = getattr(self.interface_action_base_plugin, "version_string", "unknown")
        LOGGER.info(
            "Plugin initialized version=%s; persistent_log=%s",
            version,
            log_path(),
        )

        plugin_icon = self._load_plugin_icon("icon.png")
        if plugin_icon is not None:
            self.qaction.setIcon(plugin_icon)
            self.menuless_qaction.setIcon(plugin_icon)
            LOGGER.debug("Plugin toolbar icon loaded from packaged resource icon.png")
        else:
            LOGGER.warning("Plugin toolbar icon could not be loaded from icon.png")

        self.qaction.triggered.connect(self.show_dashboard)

        self.create_menu_action(
            self.qaction.menu(),
            "deluxe_sync_dashboard",
            _("Dashboard"),
            description=_(
                "Show server status, library link counts, review changes, and safe batch sync."
            ),
            triggered=self.show_dashboard,
        )

        menu = self.qaction.menu()
        menu.addSeparator()
        self.create_menu_action(
            menu,
            "deluxe_sync_configure",
            _("Configuration"),
            icon="config.png",
            description=_("Configure Deluxe Sync."),
            triggered=self.show_config,
        )

        menu.addSeparator()
        self.create_menu_action(
            menu,
            "deluxe_sync_about",
            _("About"),
            description=_("About Deluxe Sync."),
            triggered=self.show_about,
        )

    def _load_plugin_icon(self, resource_name: str) -> QIcon | None:
        """Load a QIcon from this plugin ZIP rather than Calibre's icon theme."""

        try:
            base_plugin = self.interface_action_base_plugin
            if base_plugin is not None:
                resources = base_plugin.load_resources([resource_name])
            else:
                resources = self.load_resources([resource_name])
            raw = resources.get(resource_name)
            if not raw:
                return None

            pixmap = QPixmap()
            if not pixmap.loadFromData(raw):
                return None

            icon = QIcon(pixmap)
            return None if icon.isNull() else icon
        except Exception as error:
            LOGGER.warning(
                "Plugin icon load failed resource=%s error_type=%s",
                resource_name,
                type(error).__name__,
            )
            return None

    def _center_dialog(self, dialog: QDialog) -> None:
        target_geometry = None
        if self.gui is not None and hasattr(self.gui, "frameGeometry"):
            try:
                target_geometry = self.gui.frameGeometry()
            except RuntimeError:
                target_geometry = None

        if target_geometry is None:
            screen = dialog.screen()
            if screen is not None:
                target_geometry = screen.availableGeometry()

        if target_geometry is None:
            return

        frame = dialog.frameGeometry()
        frame.moveCenter(target_geometry.center())
        dialog.move(frame.topLeft())

    def show_dashboard(self):
        dialog = DashboardDialog(self)
        self._center_dialog(dialog)
        dialog.exec()

    def sync_selected_books_now(self):
        dialog = SyncSelectedBooksDialog(
            self,
            auto_sync=True,
            auto_show_completion=True,
        )
        self._center_dialog(dialog)
        dialog.exec()

    def show_sync_selected_dialog(self):
        dialog = SyncSelectedBooksDialog(self)
        self._center_dialog(dialog)
        dialog.exec()

    def show_sync_preview_dialog(self):
        dialog = SyncPreviewDialog(self)
        self._center_dialog(dialog)
        dialog.exec()

    def show_match_books_dialog(self):
        dialog = MatchBooksDialog(self)
        self._center_dialog(dialog)
        dialog.exec()

    def show_library_dialog(self):
        dialog = LibraryDialog(self)
        self._center_dialog(dialog)
        dialog.exec()

    def show_stage_zero_dialog(self):
        """Backward-compatible entry point retained for existing smoke tests."""

        self.show_library_dialog()

    def show_config(self):
        self.interface_action_base_plugin.do_user_config(self.gui)

    def show_about(self):
        dialog = AboutDialog(self)
        self._center_dialog(dialog)
        dialog.exec()
