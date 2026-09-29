# Changelog

## [0.1.0.1] - 2026-09-29

- Added bidirectional native Calibre Rating synchronization with half-star precision through the enhanced server book-feedback API.
- Added a dedicated **Summary / Review** custom-column mapping (`#ds_review` / **DS Review**) for private KOReader review notes without using Calibre's built-in Comments field.
- Added explicit rating/review clear handling, Review Changes policies, and regression coverage for feedback privacy and metadata separation.
- Aligned Calibre reading-state labels with the enhanced server's 1% active-reading rule: sub-1% cover/front-matter browsing remains **Not started** while exact progress, location, and last-sync data are preserved; 1% starts **Reading**, and manual completion or 100% remains **Finished**.
- Prevented orphan series numbers when Calibre has no series name, and repaired an existing server-side series index when both Calibre and the server have no series name.

## [0.1.0.0] - 2026-09-20

Deluxe Sync 0.1.0.0 is the first public Calibre companion release for Deluxe-Sync and compatible KOSync servers.

### Easier setup and server connection

- Added one-time Techy-Notes pairing for Calibre, plus standard KOSync username/password support.
- Added a server dashboard that identifies Enhanced, BookOrbit, Crosspoint, and Standard KOSync profiles and shows the connected server status and book count where available.
- Added plain-language configuration screens for server setup and Calibre custom-column mappings.
- Added automatic creation of the recommended progress, status, location, last-sync, annotations, and vocabulary columns, with a Calibre restart option when new columns are created.

### Book matching and library browsing

- Added server-library browsing and linked-version details for compatible servers.
- Added selected-book matching using saved mappings, ASIN, ISBN, title/author, series information, and filename hints.
- Added safe server-side book creation when an enhanced server explicitly advertises document registration.
- Added durable Calibre library/book mappings, including backup and recovery on compatible enhanced servers.

### Reading data in Calibre

- Added server-to-Calibre reading progress, reading status, last location, and last-sync updates.
- Added optional server-to-Calibre highlights, notes, bookmarks, and Vocabulary Builder archives in HTML custom columns.
- Added incremental refresh support on enhanced servers so already-linked books can avoid unnecessary full data reads.
- Added clear completion summaries for updated, already-current, skipped, and failed books.

### Metadata and covers

- Added a review screen for title, author, ISBN, ASIN, series, series number, and cover differences.
- Added per-field choices for Calibre wins, Server wins, or Do not sync.
- Added verified Calibre-to-server metadata updates on compatible enhanced servers.
- Added cover transfer support for compatible enhanced-server book creation and metadata workflows without changing reading progress.

### Usability and safety

- Added the compact Deluxe Sync dashboard as the main toolbar action, with direct Sync Now behavior and back-navigation from child windows.
- Added sensitive-value redaction in plugin logs.
- Added Calibre-native translation hooks and an i18n audit for user-visible strings.
- Reading progress, annotations, and vocabulary remain server-to-Calibre only; metadata updates never silently modify reading position.
