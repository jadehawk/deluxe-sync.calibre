"""Stage 8 server vocabulary -> Calibre archival HTML synchronization."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Any


@dataclass(frozen=True)
class VocabularyArchive:
    """Deterministic HTML archive generated from current server vocabulary."""

    html: str
    count: int


@dataclass(frozen=True)
class VocabularyApplyResult:
    """Result of writing one vocabulary archive to one Calibre row."""

    changed: bool
    changed_book_ids: frozenset[int]


def _clean_word(value: Any) -> str:
    """Normalize display whitespace without changing the word's spelling/case."""

    return " ".join(str(value or "").split())


def render_vocabulary_archive(payload: dict[str, Any] | None) -> VocabularyArchive:
    """Render current vocabulary as a portable, de-duplicated HTML list.

    The complete value is regenerated from server state every run. Words are
    normalized by whitespace and case-insensitive identity, then sorted
    deterministically so repeated syncs never append or reshuffle entries.
    """

    source = payload if isinstance(payload, dict) else {}
    raw_entries = source.get("entries")
    if not isinstance(raw_entries, list):
        raw_entries = []

    candidates: list[str] = []
    for item in raw_entries:
        if not isinstance(item, dict) or item.get("deleted") is True:
            continue
        word = _clean_word(item.get("word"))
        if word:
            candidates.append(word)

    candidates.sort(key=lambda word: (word.casefold(), word))
    words: list[str] = []
    seen: set[str] = set()
    for word in candidates:
        identity = word.casefold()
        if identity in seen:
            continue
        seen.add(identity)
        words.append(word)

    if not words:
        return VocabularyArchive(html="", count=0)

    items = "".join(f"<li>{escape(word, quote=True)}</li>" for word in words)
    html = (
        '<div class="deluxe-sync-vocabulary">'
        "<h3>Vocabulary</h3>"
        f"<ul>{items}</ul>"
        "</div>"
    )
    return VocabularyArchive(html=html, count=len(words))


def apply_vocabulary_archive(
    db: Any,
    book_id: int,
    lookup: str,
    archive: VocabularyArchive,
) -> VocabularyApplyResult:
    """Replace the mapped vocabulary archive only when generated HTML changed."""

    clean_lookup = str(lookup or "").strip()
    if not clean_lookup:
        return VocabularyApplyResult(changed=False, changed_book_ids=frozenset())

    api = getattr(db, "new_api", None)
    if api is None:
        raise RuntimeError("The active Calibre library does not expose the database API.")

    desired = archive.html
    try:
        current = api.field_for(clean_lookup, int(book_id), default_value=None)
    except TypeError:
        current = api.field_for(clean_lookup, int(book_id))

    current_text = "" if current is None else str(current)
    if current_text == desired:
        return VocabularyApplyResult(changed=False, changed_book_ids=frozenset())

    affected = api.set_field(clean_lookup, {int(book_id): desired})
    if affected:
        changed_ids = frozenset(int(value) for value in affected)
    else:
        changed_ids = frozenset({int(book_id)})
    return VocabularyApplyResult(changed=True, changed_book_ids=changed_ids)
