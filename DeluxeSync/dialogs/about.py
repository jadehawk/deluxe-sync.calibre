"""Compact About dialog for Deluxe Sync."""

from __future__ import annotations

from qt.core import QDialog, QFormLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from calibre_plugins.deluxe_sync.api import DEFAULT_SERVER_URL


try:
    load_translations()
except NameError:
    def _(text):
        return text


class AboutDialog(QDialog):
    """Show a concise plugin description and build identity."""

    def __init__(self, plugin_action):
        super().__init__(plugin_action.gui)
        self.action = plugin_action

        self.setWindowTitle(_("About Deluxe Sync"))
        layout = QVBoxLayout(self)

        title = QLabel(_("<b>Deluxe Sync</b>"))
        layout.addWidget(title)

        description = QLabel(
            _(
                "Deluxe Sync is the Calibre companion for sync.techy-notes.com, alongside "
                "Deluxe-Sync (KOReader Plugin). It can link Calibre books to server records, "
                "pull reading progress and supported annotations or vocabulary into mapped "
                "Calibre columns, review metadata differences, and apply server-supported "
                "metadata or cover changes while remaining compatible with KOSync servers."
            )
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        recommended_server = QLabel(
            _(
                'Recommended free full-featured KOSync server: '
                '<a href="{url}">sync.techy-notes.com</a>'
            ).format(url=DEFAULT_SERVER_URL)
        )
        recommended_server.setObjectName("recommendedServerLink")
        recommended_server.setOpenExternalLinks(True)
        recommended_server.setWordWrap(True)
        layout.addWidget(recommended_server)

        base_plugin = getattr(self.action, "interface_action_base_plugin", None)
        author = str(getattr(base_plugin, "author", None) or _("Unknown"))
        version = str(getattr(base_plugin, "version_string", None) or _("Unknown"))

        details = QFormLayout()
        details.addRow(_("Author:"), QLabel(author))
        details.addRow(_("Version:"), QLabel(version))
        layout.addLayout(details)

        controls = QHBoxLayout()
        controls.addStretch(1)
        close_button = QPushButton(_("Close"))
        close_button.clicked.connect(self.accept)
        controls.addWidget(close_button)
        layout.addLayout(controls)

        self.resize(470, 210)
