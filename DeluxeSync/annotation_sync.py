"""Stage 7 server annotation -> Calibre archival HTML synchronization."""

from __future__ import annotations

import json
from dataclasses import dataclass
from html import escape
from typing import Any


@dataclass(frozen=True)
class AnnotationArchive:
    """Deterministic HTML archive generated from current server annotations."""

    html: str
    count: int


@dataclass(frozen=True)
class AnnotationApplyResult:
    """Result of writing one annotation archive to one Calibre row."""

    changed: bool
    changed_book_ids: frozenset[int]


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _html_text(value: Any) -> str:
    return escape(_clean(value), quote=True).replace("\n", "<br>")


def _compact_json(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value).strip()


def _annotation_sort_key(annotation: dict[str, Any]) -> tuple[str, str, str, str]:
    created = _clean(annotation.get("created_datetime") or annotation.get("updated_datetime"))
    updated = _clean(annotation.get("updated_datetime"))
    document = _clean(annotation.get("document"))
    annotation_id = _clean(annotation.get("id"))
    return created, updated, document.casefold(), annotation_id.casefold()


def _metadata_parts(annotation: dict[str, Any]) -> list[str]:
    parts: list[str] = []

    chapter = _clean(annotation.get("chapter"))
    if chapter:
        parts.append(chapter)

    pageno = annotation.get("pageno")
    if isinstance(pageno, int) and not isinstance(pageno, bool) and pageno >= 0:
        parts.append(f"Page {pageno}")

    pageref = _clean(annotation.get("pageref"))
    if pageref:
        parts.append(f"Page ref {pageref}")

    timestamp = _clean(
        annotation.get("updated_datetime")
        or annotation.get("created_datetime")
    )
    if timestamp:
        parts.append(timestamp)

    device = _clean(
        annotation.get("updated_by_device")
        or annotation.get("origin_device")
    )
    if device:
        parts.append(device)

    document = _clean(annotation.get("document"))
    if document:
        parts.append(f"Source: {document}")

    annotation_id = _clean(annotation.get("id"))
    if annotation_id:
        parts.append(f"ID: {annotation_id}")

    return parts


def _position_parts(annotation: dict[str, Any]) -> list[str]:
    parts: list[str] = []
    page = _compact_json(annotation.get("page"))
    pos0 = _compact_json(annotation.get("pos0"))
    pos1 = _compact_json(annotation.get("pos1"))

    if page:
        parts.append(f"Page position: {page}")
    if pos0:
        parts.append(f"Start: {pos0}")
    if pos1:
        parts.append(f"End: {pos1}")
    return parts


def render_annotation_archive(payload: dict[str, Any] | None) -> AnnotationArchive:
    """Render current non-deleted server annotations as portable Calibre HTML.

    The complete value is regenerated from server state every run. It is never
    appended to an existing Calibre value, preventing duplicate archival rows.
    """

    source = payload if isinstance(payload, dict) else {}
    raw_annotations = source.get("annotations")
    if not isinstance(raw_annotations, list):
        raw_annotations = []

    annotations = [
        item
        for item in raw_annotations
        if isinstance(item, dict) and item.get("deleted") is not True
    ]
    annotations.sort(key=_annotation_sort_key)

    if not annotations:
        return AnnotationArchive(html="", count=0)

    blocks = ['<div class="deluxe-sync-annotations">', "<h3>Highlights &amp; Notes</h3>"]
    labels = {
        "highlight": "Highlight",
        "note": "Note",
        "bookmark": "Bookmark",
    }

    for annotation in annotations:
        kind = _clean(annotation.get("kind")).lower()
        label = labels.get(kind, "Annotation")
        css_kind = kind if kind in labels else "annotation"
        blocks.append(f'<div class="annotation annotation-{css_kind}">')
        blocks.append(f"<p><strong>{escape(label)}</strong></p>")

        selected_text = _clean(annotation.get("text"))
        if selected_text:
            blocks.append(f"<blockquote>{_html_text(selected_text)}</blockquote>")

        note = _clean(annotation.get("note"))
        if note:
            blocks.append(f"<p><strong>Note:</strong> {_html_text(note)}</p>")

        metadata = _metadata_parts(annotation)
        if metadata:
            blocks.append(
                "<p><small>"
                + " · ".join(escape(part, quote=True) for part in metadata)
                + "</small></p>"
            )

        positions = _position_parts(annotation)
        if positions:
            blocks.append(
                "<p><small>"
                + " · ".join(escape(part, quote=True) for part in positions)
                + "</small></p>"
            )

        blocks.append("</div>")

    blocks.append("</div>")
    return AnnotationArchive(html="".join(blocks), count=len(annotations))


def apply_annotation_archive(
    db: Any,
    book_id: int,
    lookup: str,
    archive: AnnotationArchive,
) -> AnnotationApplyResult:
    """Replace the mapped archive value only when the generated HTML changed."""

    clean_lookup = str(lookup or "").strip()
    if not clean_lookup:
        return AnnotationApplyResult(changed=False, changed_book_ids=frozenset())

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
        return AnnotationApplyResult(changed=False, changed_book_ids=frozenset())

    affected = api.set_field(clean_lookup, {int(book_id): desired})
    if affected:
        changed_ids = frozenset(int(value) for value in affected)
    else:
        changed_ids = frozenset({int(book_id)})
    return AnnotationApplyResult(changed=True, changed_book_ids=changed_ids)
