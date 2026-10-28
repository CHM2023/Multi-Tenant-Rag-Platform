"""The ingestion contract: what every format extractor returns, and the pipeline entry point.

What this demonstrates:
- extractors return located text blocks, tables kept as data, and
  machine-readable warnings that never fail a document;
- a table's rows are a lazy iterable over the still-open stream, so a
  multi-million-row CSV is streamed into the database, never materialised;
- one registry routes each media type to exactly one extractor;
- one async entry point that every format goes through.

Excerpt from a private production codebase: types and signatures only.
Implementations are stubbed and marked "# STUB: production implementation is private".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import BinaryIO

# Shared helpers every extractor uses (row packing, header and section handling).
# STUB: production implementation is private


class WarningKind(StrEnum):
    """Why part of a document did not make it into the extracted text."""

    NO_EXTRACTABLE_TEXT = "NO_EXTRACTABLE_TEXT"
    OCR_BELOW_CONFIDENCE_FLOOR = "OCR_BELOW_CONFIDENCE_FLOOR"
    TEXT_FROM_OCR = "TEXT_FROM_OCR"
    ROW_CAP_TRUNCATED = "ROW_CAP_TRUNCATED"
    # STUB: production implementation is private


@dataclass(frozen=True, slots=True)
class ExtractionWarning:
    """One machine-readable gap in an extraction.

    A warning never fails a document. A 120-page PDF with 8 scanned pages is
    READY with 8 warnings, because 112 pages of answerable content is worth
    more than an all-or-nothing failure.
    """

    kind: WarningKind
    locator: str
    """Where: "page 42", "sheet Q3 Actuals", "slide 7". Human-addressable."""
    detail: str


@dataclass(frozen=True, slots=True)
class ExtractedBlock:
    """A contiguous run of text and where it came from, in reading order.

    Blocks are not chunks: the chunker decides how to group and split them.
    """

    text: str
    page_from: int | None = None
    page_to: int | None = None
    section_path: str | None = None
    # STUB: production implementation is private


@dataclass(frozen=True, slots=True)
class ExtractedTable:
    """A rectangular table found in a file, kept as data rather than as prose.

    Separate from blocks because a row is not a passage: retrieval cannot count,
    so rows go to a table and are queried with SQL.
    """

    name: str
    headers: tuple[str, ...]
    rows: Iterable[Sequence[str]]
    """The rows, which **may be a generator over the still-open stream**.

    Iterate exactly once, before the caller closes the file. A million-row CSV
    cannot be held in memory, so the alternative to streaming is not a list - it
    is a failed ingest.
    """
    # STUB: production implementation is private


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    blocks: list[ExtractedBlock]
    page_count: int | None = None
    warnings: tuple[ExtractionWarning, ...] = field(default=())
    tables: tuple[ExtractedTable, ...] = field(default=())


class Extractor(ABC):
    """Turns the bytes of one file format into located text."""

    @property
    @abstractmethod
    def mime_types(self) -> frozenset[str]:
        """Media types this extractor claims. Used to route a file to it."""

    @abstractmethod
    def extract(self, stream: BinaryIO, filename: str) -> ExtractionResult:
        """The caller rewinds the stream to 0; an extractor may seek, never closes it."""


class UnsupportedFormatError(Exception):
    # STUB: production implementation is private
    pass


class ExtractorRegistry:
    """Routes a media type to the extractor that handles it."""

    def __init__(self, extractors: list[Extractor]) -> None:
        self._by_mime_type: dict[str, Extractor] = {}
        for extractor in extractors:
            for mime_type in extractor.mime_types:
                existing = self._by_mime_type.get(mime_type)
                if existing is not None:
                    raise ValueError(
                        f"{type(extractor).__name__} and {type(existing).__name__} "
                        f"both claim {mime_type}"
                    )
                self._by_mime_type[mime_type] = extractor

    # STUB: production implementation is private

    def for_mime_type(self, mime_type: str, filename: str | None = None) -> Extractor:
        extractor = self._by_mime_type.get(mime_type)
        if extractor is None:
            raise UnsupportedFormatError(mime_type, filename)
        return extractor


class IngestMode(StrEnum):
    FULL = "full"
    TABLE = "table"
    DOCUMENT = "document"


@dataclass(frozen=True, slots=True)
class IngestOptions:
    """The effective settings for one ingest, and the mode that produced them."""

    mode: IngestMode = IngestMode.FULL
    # STUB: production implementation is private


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    document: object
    created: bool
    """False when the SHA-256 was already present - the idempotency path."""

    chunks_written: int
    warnings: tuple[ExtractionWarning, ...]

    duplicate_of: object | None = None
    """Set when a different file carrying the same text arrived, which the
    SHA-256 of the bytes cannot see."""

    rows_written: int = 0
    """Separate from chunks: a chunk is retrieved, a row is counted."""


class IngestionPipeline:
    # STUB: production implementation is private

    async def ingest(
        self,
        tenant_id: str,
        stream: BinaryIO,
        *,
        filename: str,
        mime_type: str,
        source_path: str | None = None,
        uploaded_by: str | None = None,
        options: IngestOptions | None = None,
    ) -> IngestOutcome:
        """Stages in order, some conditional or able to return early: byte-hash
        dedupe -> extract -> text-hash dedupe -> chunk -> memory guard -> embed
        -> store chunks and stream table rows -> normalise row entities."""
        # STUB: production implementation is private
        raise NotImplementedError
