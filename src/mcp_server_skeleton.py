"""MCP server skeleton: every result leads with a human-readable scope line.

What this demonstrates:
- typed response models that declare a one-line `display` first, because the
  client renders each result as a reference card from the start of the
  serialised object;
- honesty fields a preview cannot cut: matched vs measured vs total rows,
  `incomplete` when a figure is not a total, `unsupported` instead of a number;
- tools that never take a tenant argument (see tenant_isolation.py) and that
  bound whatever a language model asks for.

Excerpt from a private production codebase. Model-facing text is shortened,
and retrieval and table internals are stubbed and marked
"# STUB: production implementation is private".
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

from tenant_isolation import current_tenant


class Operation(StrEnum):
    COUNT = "count"
    SUM = "sum"
    # STUB: production implementation is private


SERVER_INSTRUCTIONS = (
    "One tenant's document collection. Use search_documents for what a document "
    "says and aggregate_table for a number computed over a table's rows."
)

MEASURED_SHARE_FLOOR = 0.5
"""Below this share of matched rows, an aggregate stops describing its rows."""

DISPLAY_SOURCES_SHOWN = 4
"""Source files named in a display string before it says "+N more"."""


def _incompleteness(over: str, measured: int, matched: int) -> str | None:
    """The sentence that must not be truncated away, or None.

    Phrased as what the number *is* rather than as a warning about it, because
    the consumer is a language model composing an answer: a fact is something
    it repeats, where "warning: incomplete" is something it can drop.
    """
    if matched <= 0 or measured >= matched:
        return None
    if measured / matched >= MEASURED_SHARE_FLOOR:
        return None
    if measured == 0:
        return f"no rows held a value for {over} - this is not a result"
    return (
        f"{over} computed over only {measured:,} of {matched:,} matched rows "
        "- not a total of the rows asked about"
    )


def _display_sources(names: Sequence[str]) -> str:
    """The source files, named, with a bounded tail."""
    unique: list[str] = []
    for name in names:
        if name not in unique:
            unique.append(name)
    if not unique:
        return "no tables"
    if len(unique) <= DISPLAY_SOURCES_SHOWN:
        return " + ".join(unique)
    shown = " + ".join(unique[:DISPLAY_SOURCES_SHOWN])
    return f"{shown} +{len(unique) - DISPLAY_SOURCES_SHOWN} more"


class Passage(BaseModel):
    """One retrieved passage; `display` first, because it is what the card shows."""

    display: str = Field(description="One line naming the source and match strength.")
    citation: str = Field(description="A ready-made reference to show the user.")
    filename: str = Field(description="The document it came from.")
    page: str | None = Field(default=None, description="The page as the document labels it.")
    section: str | None = Field(default=None, description="Headings above the passage.")
    score: float = Field(description="Relevance, 0 to 1. Higher is closer.")
    continuation: bool = Field(default=False, description="Continues the passage before it.")
    content: str = Field(description="The passage text.")


class Aggregation(BaseModel):
    """A number with what it was computed over; `display` leads, as in Passage."""

    display: str = Field(default="", description="The whole answer in one line. Show this.")
    citation: str = Field(description="A ready-made reference to show the user.")
    # STUB: production implementation is private
    incomplete: str | None = Field(default=None, description="Set when it is not a total.")
    unsupported: str | None = Field(default=None, description="Why nothing was computed.")
    result: float | None = Field(default=None, description="The computed value.")
    # STUB: production implementation is private
    matched_rows: int = Field(default=0, description="Rows the filters selected.")
    measured_rows: int = Field(default=0, description="Rows holding a value to compute over.")
    total_rows: int = Field(default=0, description="Rows in the table(s).")
    truncated: bool = Field(default=False, description="Loaded incompletely; a minimum.")
    # STUB: production implementation is private


def build_mcp_server() -> MCPServer:
    """Build the MCP server. No tool takes a tenant; each reads current_tenant()."""
    server = MCPServer(
        name="docsearch",
        title="Document search",
        instructions=SERVER_INSTRUCTIONS,
        version="0.1.0",
    )

    @server.tool(name="search_documents", description="Passages that answer a question.")
    async def search_documents(
        query: Annotated[str, Field(description="The user's question, in their words.")],
        top_k: Annotated[int, Field(description="How many passages to return.")] = 5,
    ) -> list[Passage]:
        tenant_id = current_tenant()

        # Bounded here rather than trusted: top_k arrives from a language model,
        # and a request for 500 passages is a way to pull a corpus through a
        # context window one question at a time.
        limit = max(1, min(int(top_k), 25))

        # The same service the HTTP API's search uses. Not a second implementation.
        hits = await _retrieve(tenant_id, query, top_k=limit)

        passages: list[Passage] = []
        for hit in hits:
            page = _format_page(hit)
            # Page first, filename second: the card shows only the first few
            # dozen characters, and the page is what tells two cards apart.
            citation = hit.filename if page is None else f"p.{page} - {hit.filename}"
            # Three decimals, because two round a strong match up to 1.00.
            strength = (
                "continuation" if hit.continuation else f"relevance {hit.score:.3f}"
            )
            passages.append(
                Passage(
                    display=f"{citation} — {strength}",
                    content=hit.content,
                    filename=hit.filename,
                    page=page,
                    section=hit.section_path,
                    score=round(hit.score, 4),
                    citation=citation,
                    continuation=hit.continuation,
                )
            )
        return passages

    # STUB: production implementation is private

    @server.tool(name="aggregate_table", description="One number computed over table rows.")
    async def aggregate_table(
        table_id: str | None = None,
        tables: list[str] | None = None,
        operation: str = Operation.COUNT.value,
        column: str | None = None,
        percentile: float | None = None,
        # STUB: production implementation is private
    ) -> Aggregation:
        tenant_id = current_tenant()
        # An unknown operation comes back as `unsupported`, never as a validation error.
        # STUB: production implementation is private
        chosen = Operation(operation)
        named = tables if tables else ([table_id] if table_id else [])
        # STUB: production implementation is private
        outcome = await _aggregate(tenant_id, named, chosen, column, percentile)

        single = getattr(outcome, "table", None)
        records = [single] if single is not None else outcome.tables
        total_rows = sum(record.row_count for record in records)
        truncated = single.truncated if single is not None else outcome.truncated

        # STUB: production implementation is private
        if column:
            over = f"{chosen.value}({column})"
        else:
            over = chosen.value

        if single is not None:
            source = (
                single.filename
                if single.table_name == single.filename
                else f"{single.table_name} - {single.filename}"
            )
        else:
            # Named rather than counted: "4 tables" gives a reader nothing to check.
            source = f"{len(records)} tables: " + ", ".join(
                record.table_name for record in records
            )

        citation = (
            f"{over} over {outcome.matched_rows:,} of {total_rows:,} rows in {source}"
        )
        if outcome.measured_rows < outcome.matched_rows:
            citation += f" ({outcome.measured_rows:,} held a value to compute over)"

        # The shortfall has to survive the citation being cut, so `incomplete` is
        # a field of its own and the citation leads with it rather than trailing it.
        incomplete = _incompleteness(over, outcome.measured_rows, outcome.matched_rows)
        if incomplete:
            citation = f"{incomplete} - {citation}"

        # Built from the same `outcome` as the citation, so it cannot describe a
        # different computation than the one that ran.
        labelled = _display_operation(chosen.value, column or None, percentile)
        sources = _display_sources([record.filename for record in records])
        if chosen is Operation.COUNT:
            head = f"{labelled} = {_display_value(outcome.value)} rows"
        else:
            head = (
                f"{labelled} = {_display_value(outcome.value)}"
                f" over {outcome.measured_rows:,} of {outcome.matched_rows:,} rows"
            )
        display = f"{head} — {sources}"
        if incomplete:
            display = f"not a total — {display}"
        if truncated:
            display += " (loaded incompletely; a minimum)"

        # STUB: production implementation is private
        return Aggregation(
            display=display,
            citation=citation,
            incomplete=incomplete,
            result=outcome.value,
            matched_rows=outcome.matched_rows,
            measured_rows=outcome.measured_rows,
            total_rows=total_rows,
            truncated=truncated,
        )

    # STUB: production implementation is private

    return server


def _display_operation(operation: str, column: str | None, percentile: float | None) -> str:
    """`median(amount)` rather than `percentile(amount)` when percentile is 0.5."""
    # STUB: production implementation is private
    raise NotImplementedError


def _display_value(value: float | None, unit: str | None = None) -> str:
    """A number as a person writes it, with a currency only when one is recorded."""
    # STUB: production implementation is private
    raise NotImplementedError


def _format_page(hit) -> str | None:
    """The printed page label when the document has one, else the physical page."""
    # STUB: production implementation is private
    raise NotImplementedError


async def _retrieve(tenant_id: str, query: str, *, top_k: int) -> list:
    """Hybrid retrieval: vector + full-text arms, rank fusion, cross-encoder rerank."""
    # STUB: production implementation is private
    raise NotImplementedError


async def _aggregate(tenant_id: str, table_ids: list[str], operation, column, percentile):
    """The closed aggregation grammar, compiled to parameterised SQL over JSONB rows."""
    # STUB: production implementation is private
    raise NotImplementedError
