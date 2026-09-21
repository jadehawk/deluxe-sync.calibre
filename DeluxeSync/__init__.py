"""Deluxe Sync Calibre plugin entry point."""

from calibre.customize import InterfaceActionBase


class DeluxeSyncPlugin(InterfaceActionBase):
    """Calibre plugin metadata and configuration hook."""

    name = "Deluxe Sync"
    description = "Sync selected Calibre books with an enhanced KOReader Sync server."
    supported_platforms = ["windows", "osx", "linux"]
    author = "Jadehawk"
    version = (0, 1, 0, 1)
    version_string = ".".join(str(part) for part in version)
    minimum_calibre_version = (7, 0, 0)

    actual_plugin = "calibre_plugins.deluxe_sync.action:DeluxeSyncAction"

    def is_customizable(self):
        return True

    def config_widget(self):
        if not self.actual_plugin_:
            return None
        from calibre_plugins.deluxe_sync.config import ConfigWidget

        return ConfigWidget(self.actual_plugin_)

    def save_settings(self, config_widget):
        config_widget.save_settings()
