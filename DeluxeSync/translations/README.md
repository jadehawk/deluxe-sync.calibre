# Deluxe Sync translations

Deluxe Sync follows Calibre's gettext-style plugin localization model.

- User-visible Python strings use `_()`.
- Plural messages use `ngettext()`.
- Every Python module that uses translated strings calls `load_translations()`.
- English is the source language and runtime fallback.
- `messages.pot` is the generated English translation template.
- Per-language editable catalogs use `translations/<language>.po`.
- Compiled catalogs loaded by Calibre use `translations/<language>.mo`.
- There is intentionally no `en.json` and normally no `en.mo`; the English Python source strings are already the fallback.

## Translation workflow

Regenerate the English template whenever user-visible strings change:

```powershell
python scripts/extract_translations.py
```

Verify that the committed template is current without rewriting it:

```powershell
python scripts/extract_translations.py --check
```

Create a new language catalog from the template, for example Spanish:

```powershell
msginit --input=DeluxeSync/translations/messages.pot --locale=es --output-file=DeluxeSync/translations/es.po
```

When English source strings change, merge the updated template into an existing language catalog:

```powershell
msgmerge --update DeluxeSync/translations/es.po DeluxeSync/translations/messages.pot
```

Compile the translated catalog for Calibre:

```powershell
msgfmt DeluxeSync/translations/es.po --output-file=DeluxeSync/translations/es.mo
```

Tools such as Poedit can perform the same POT/PO editing and MO compilation workflow.

The plugin build automatically packages any `*.mo` files in this directory. Keep `messages.pot` and maintained `*.po` source catalogs in version control; generated `*.mo` files should be committed only when they are intentionally shipped as part of a translation release.
