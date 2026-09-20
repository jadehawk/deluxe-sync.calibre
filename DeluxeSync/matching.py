"""Read-only Calibre ↔ server matching for Deluxe Sync Stage 3."""

from __future__ import annotations

from dataclasses import replace
from pathlib import PurePath
import re
import unicodedata
from typing import Any

from calibre_plugins.deluxe_sync.models import (
    CalibreBook,
    MatchCandidate,
    MatchResult,
    RemoteRecord,
)


_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_NON_IDENTIFIER = re.compile(r"[^A-Z0-9]+")


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.casefold()
    return " ".join(part for part in _NON_ALNUM.split(text) if part)


def normalize_identifier(value: Any) -> str:
    return _NON_IDENTIFIER.sub("", str(value or "").upper())


def normalize_filename(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        stem = PurePath(text).stem
    except Exception:
        stem = text.rsplit(".", 1)[0]
    return normalize_text(stem)


def _clean_index(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _remote_record(
    item: dict[str, Any],
    *,
    logical_book_id: int | None = None,
    logical_title: str = "",
    logical_authors: str = "",
) -> RemoteRecord:
    return RemoteRecord(
        document=str(item.get("document") or "").strip(),
        title=str(item.get("title") or "").strip(),
        authors=str(item.get("authors") or "").strip(),
        filename=str(item.get("filename") or "").strip(),
        isbn=str(item.get("isbn") or "").strip(),
        asin=str(item.get("asin") or "").strip(),
        series=str(item.get("series") or "").strip(),
        series_index=_clean_index(item.get("series_index")),
        logical_book_id=logical_book_id,
        logical_title=logical_title,
        logical_authors=logical_authors,
    )


def build_remote_records(
    library: dict[str, Any],
    logical_details: dict[int, dict[str, Any]] | None = None,
) -> list[RemoteRecord]:
    """Flatten raw and logical library entries while preserving logical membership."""

    logical_details = logical_details or {}
    records: list[RemoteRecord] = []
    seen: set[tuple[str, int | None]] = set()

    for item in library.get("books") or ():
        if not isinstance(item, dict):
            continue

        if item.get("kind") != "logical":
            record = _remote_record(item)
            key = (record.document, None)
            if record.document and key not in seen:
                records.append(record)
                seen.add(key)
            continue

        try:
            logical_id = int(item.get("logical_book_id"))
        except (TypeError, ValueError):
            continue

        logical_title = str(item.get("title") or "").strip()
        logical_authors = str(item.get("authors") or "").strip()
        details = logical_details.get(logical_id)
        members = details.get("members") if isinstance(details, dict) else None

        if isinstance(members, list) and members:
            for member in members:
                if not isinstance(member, dict):
                    continue
                record = _remote_record(
                    member,
                    logical_book_id=logical_id,
                    logical_title=str(
                        (details or {}).get("title") or logical_title
                    ).strip(),
                    logical_authors=str(
                        (details or {}).get("authors") or logical_authors
                    ).strip(),
                )
                key = (record.document, logical_id)
                if record.document and key not in seen:
                    records.append(record)
                    seen.add(key)
            continue

        # Keep an unbindable presentation candidate when logical details could not
        # be loaded. This lets the UI explain the possible match without inventing
        # a raw document identity.
        placeholder = _remote_record(
            item,
            logical_book_id=logical_id,
            logical_title=logical_title,
            logical_authors=logical_authors,
        )
        placeholder = replace(placeholder, document="")
        key = ("", logical_id)
        if key not in seen:
            records.append(placeholder)
            seen.add(key)

    return records


def _title_author_match(local: CalibreBook, remote: RemoteRecord) -> bool:
    local_title = normalize_text(local.title)
    remote_title = normalize_text(remote.display_title or remote.title)
    if not local_title or local_title != remote_title:
        return False

    local_authors = normalize_text(local.authors_text)
    remote_authors = normalize_text(remote.display_authors or remote.authors)
    return bool(local_authors and remote_authors and local_authors == remote_authors)


def _series_match(local: CalibreBook, remote: RemoteRecord) -> bool:
    local_series = normalize_text(local.series)
    remote_series = normalize_text(remote.series)
    if not local_series or local_series != remote_series:
        return False
    if local.series_index is None or remote.series_index is None:
        return True
    return abs(float(local.series_index) - float(remote.series_index)) < 0.0001


def _disambiguate_series(
    local: CalibreBook,
    records: list[RemoteRecord],
) -> list[RemoteRecord]:
    if len(records) <= 1 or not normalize_text(local.series):
        return records
    series_matches = [record for record in records if _series_match(local, record)]
    return series_matches or records


def _candidate(
    remote: RemoteRecord,
    reason: str,
    tier: int,
    *,
    automatic: bool,
) -> MatchCandidate:
    return MatchCandidate(
        remote=remote,
        reason=reason,
        tier=tier,
        automatic=automatic,
    )


def _bound_result(
    local: CalibreBook,
    binding: dict[str, Any],
    remote_records: list[RemoteRecord],
) -> MatchResult | None:
    document = str(binding.get("server_document") or "").strip()
    if not document:
        return None

    for remote in remote_records:
        if remote.document != document:
            continue
        updated = dict(binding)
        current_logical = remote.logical_book_id
        old_logical = binding.get("logical_book_id")
        if old_logical != current_logical:
            updated["logical_book_id"] = current_logical
        return MatchResult(
            local=local,
            state="already_enrolled",
            candidates=[
                _candidate(remote, "Existing binding", 0, automatic=True)
            ],
            binding=updated,
            binding_changed=updated != binding,
        )

    return None


def match_book(
    local: CalibreBook,
    remote_records: list[RemoteRecord],
    binding: dict[str, Any] | None = None,
) -> MatchResult:
    """Match one selected Calibre book without changing either library."""

    if isinstance(binding, dict):
        bound = _bound_result(local, binding, remote_records)
        if bound is not None:
            return bound

    asin = normalize_identifier(local.asin)
    if asin:
        matches = [
            remote
            for remote in remote_records
            if normalize_identifier(remote.asin) == asin
        ]
        matches = _disambiguate_series(local, matches)
        if matches:
            automatic = len(matches) == 1
            return MatchResult(
                local=local,
                state="matched" if automatic else "needs_review",
                candidates=[
                    _candidate(
                        remote,
                        "Exact ASIN" if automatic else "Exact ASIN (ambiguous)",
                        1,
                        automatic=automatic,
                    )
                    for remote in matches
                ],
                binding=binding,
            )

    isbn = normalize_identifier(local.isbn)
    if isbn:
        matches = [
            remote
            for remote in remote_records
            if normalize_identifier(remote.isbn) == isbn
        ]
        matches = _disambiguate_series(local, matches)
        if matches:
            automatic = len(matches) == 1
            return MatchResult(
                local=local,
                state="matched" if automatic else "needs_review",
                candidates=[
                    _candidate(
                        remote,
                        "Exact ISBN" if automatic else "Exact ISBN (ambiguous)",
                        2,
                        automatic=automatic,
                    )
                    for remote in matches
                ],
                binding=binding,
            )

    matches = [
        remote
        for remote in remote_records
        if _title_author_match(local, remote)
    ]
    matches = _disambiguate_series(local, matches)
    if matches:
        automatic = len(matches) == 1
        reason = (
            "Title + author + series"
            if automatic and normalize_text(local.series)
            and _series_match(local, matches[0])
            else "Title + author"
        )
        if not automatic:
            reason = "Title + author (ambiguous)"
        return MatchResult(
            local=local,
            state="matched" if automatic else "needs_review",
            candidates=[
                _candidate(remote, reason, 3, automatic=automatic)
                for remote in matches
            ],
            binding=binding,
        )

    local_filenames = {
        normalized
        for normalized in (normalize_filename(name) for name in local.filenames)
        if normalized
    }
    if local_filenames:
        matches = [
            remote
            for remote in remote_records
            if normalize_filename(remote.filename) in local_filenames
        ]
        if matches:
            return MatchResult(
                local=local,
                state="needs_review",
                candidates=[
                    _candidate(
                        remote,
                        "Filename hint — review required",
                        4,
                        automatic=False,
                    )
                    for remote in matches
                ],
                binding=binding,
            )

    return MatchResult(
        local=local,
        state="binding_missing" if isinstance(binding, dict) else "no_match",
        binding=binding,
    )


def match_books(
    local_books: list[CalibreBook],
    remote_records: list[RemoteRecord],
    bindings: dict[str, dict[str, Any]] | None = None,
) -> list[MatchResult]:
    bindings = bindings or {}
    return [
        match_book(book, remote_records, bindings.get(book.book_uuid))
        for book in local_books
    ]
