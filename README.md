# Deluxe Sync for Calibre

Deluxe Sync brings your KOReader/KOSync reading data into Calibre and helps keep book metadata matched with the same books on your sync server.

It is the Calibre companion to the **Deluxe-Sync KOReader plugin** and works especially well with the free enhanced Techy-Notes sync server at [https://sync.techy-notes.com](https://sync.techy-notes.com). Standard KOSync servers are also supported where their available features allow it.

**Current plugin version: 0.1.0.0**

**Project home:** [jadehawk/deluxe-sync.calibre](https://github.com/jadehawk/deluxe-sync.calibre)

## What it can do

- Connect Calibre to a Techy-Notes or compatible KOSync server.
- Match Calibre books to books already stored on the sync server.
- Create a server record for a Calibre book when the connected enhanced server supports safe book registration.
- Pull reading progress and reading status into Calibre custom columns.
- Pull highlights, notes, bookmarks, and Vocabulary Builder words into optional Calibre HTML columns when the server supports them.
- Show your server library and linked book versions from inside Calibre.
- Review metadata differences before anything is changed.
- Send selected Calibre metadata such as title, author, series, ISBN, ASIN, and cover information to a compatible enhanced server.
- Keep Calibre-to-server book mappings backed up on supported enhanced servers so they can be recovered later.

Deluxe Sync adapts to the server you connect to. Enhanced Techy-Notes servers expose the full feature set; ordinary KOSync servers continue to work with the features they actually provide.

## Installation

1. Open the [Deluxe Sync for Calibre releases page](https://github.com/jadehawk/deluxe-sync.calibre/releases).
2. Download the latest `Deluxe_Sync_vX.X.X.X.zip` file.
3. In Calibre, open **Preferences → Plugins**.
4. Choose **Load plugin from file**.
5. Select the downloaded ZIP and confirm the installation.
6. Restart Calibre if Calibre asks you to.
7. Add **Deluxe Sync** to your toolbar or menu from Calibre's toolbar preferences if it is not already visible.

Deluxe Sync requires **Calibre 7.0 or newer**.

## First-time setup

Open **Deluxe Sync → Configuration → Server Connection**.

### Recommended: pair with Techy-Notes

If you use [sync.techy-notes.com](https://sync.techy-notes.com):

1. Sign in to your Techy-Notes account.
2. Open **Linked Services → Calibre (Deluxe Sync)**.
3. Generate a one-time pairing code.
4. In Calibre, choose **Pair with Techy-Notes** and enter that code.
5. Use **Test Connection** if you want to refresh the displayed server details.

The pairing belongs to this Calibre installation, so you do not need to create a separate login for every Calibre library.

### Other KOSync servers

You can also enter a normal KOSync username and password. Deluxe Sync stores the derived KOSync authentication value rather than keeping the plain-text password.

## Set up Calibre columns

Open **Deluxe Sync → Configuration → Column Mappings**.

You can map existing Calibre custom columns or let Deluxe Sync create the recommended ones:

- **Progress** — reading percentage.
- **Status** — reading state such as reading or finished.
- **Last Location** — the saved reading position when available.
- **Last Sync** — the server timestamp for the reading state.
- **Annotations & Highlights** — highlights, notes, and bookmarks in an HTML column.
- **Vocabulary** — Vocabulary Builder words in an HTML column.

When Deluxe Sync creates a new Calibre custom column, Calibre must restart before that column becomes available. The plugin offers a **Restart Calibre** button when this is needed.

## Match your books

Before a book can be synced, Deluxe Sync needs to know which Calibre book belongs to which server book.

1. Select one or more books in Calibre.
2. Open the Deluxe Sync dashboard.
3. Choose **Match to Server Book** or **Match / Create on Server**, depending on what your server supports.
4. Review the suggested matches.
5. Confirm the books you want linked.

Deluxe Sync uses stable Calibre library and book IDs for saved mappings. Separate Calibre libraries can map their own copies of a book independently.

## Sync reading data

Select books in Calibre and click the main **Deluxe Sync** toolbar button.

From the dashboard you can choose the selected books or all linked books in the current library, then use:

- **Sync Now** — pulls the latest supported reading data from the server into Calibre.
- **Review Changes** — previews metadata differences before any metadata update.
- **Match to Server Book / Match / Create on Server** — fixes or creates book mappings where supported.

The completion message tells you how many books were updated, already current, skipped, or failed.

### Reading progress direction

Reading progress, annotations, and vocabulary are **server → Calibre**.

Deluxe Sync does **not** push Calibre reading progress back to your KOSync server. Your e-reader or KOReader client remains the normal source of reading-position updates.

## Metadata review

For linked books, **Review Changes** compares Calibre and server metadata such as:

- Title
- Authors
- ISBN
- ASIN
- Series
- Series number
- Cover availability

You can choose which side should win for each field. Compatible enhanced servers can accept approved **Calibre → server** metadata updates.

Metadata changes are never used as a hidden way to update reading progress.

## Server compatibility

### Techy-Notes enhanced server

The recommended free server is:

[https://sync.techy-notes.com](https://sync.techy-notes.com)

It supports the complete Deluxe Sync companion workflow, including richer library browsing, saved Calibre mappings, metadata updates, book registration, annotations, vocabulary, and linked-book information when enabled on the account.

### Standard KOSync

Standard KOSync servers remain supported for the features they expose. Some servers provide only progress syncing and do not expose a browseable library, metadata editing, annotations, vocabulary, or safe book creation. Deluxe Sync hides or disables actions that the connected server cannot safely support.

## Privacy and logging

Deluxe Sync writes diagnostic messages to Calibre's console and to `deluxe-sync.log` in Calibre's configuration directory.

Sensitive values such as pairing codes, saved authorization values, plain-text passwords, client identifiers, and derived KOSync keys are redacted from plugin logs.

## Project links

- [Deluxe Sync for Calibre on GitHub](https://github.com/jadehawk/deluxe-sync.calibre) — downloads, source code, releases, and issue tracking.
- [Deluxe-Sync for KOReader](https://github.com/jadehawk/deluxe-sync.koplugin) — the KOReader companion plugin.
- [Techy-Notes Sync](https://sync.techy-notes.com) — recommended free enhanced KOSync server.
- [Techy Notes](https://techy-notes.com) — project site, guides, and notes.

## For developers

The installable plugin source lives in `DeluxeSync/`. Build the Calibre plugin ZIP with:

```powershell
python scripts/build_plugin.py
```

The resulting file is written to `dist/Deluxe_Sync_v<version>.zip`. The version is read from `DeluxeSync/__init__.py`, which is the single source of truth for the plugin version.

The repository also contains Calibre-runtime smoke tests under `scripts/` plus the i18n audit:

```powershell
python scripts/i18n_audit.py
```

Release automation validates the version, checks Python syntax and localization, builds the plugin archive, validates its contents, and creates a GitHub Release from the matching changelog section.
