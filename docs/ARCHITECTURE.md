# Architecture

This document describes how the Multi-Tenant RAG Platform is put together: the data model, the ingestion pipeline, normalisation, the retrieval algorithm, the MCP tool surface and the security model. It is written for engineers. It explains the design and the reasoning behind it, but deliberately omits the implementation, the tuned parameters and the model-facing text that make up the product.

## 1. Shape of the system

The system is two processes and one database:

- **API process** (FastAPI on Uvicorn). It serves the versioned HTTP API (`/v1/...`), the ingestion console (one static page) and the MCP endpoint (`/mcp`). It also runs the background ingest worker and the startup reapers. Models (embedder and reranker) are loaded once at startup, and startup refuses to proceed if the configured embedding model disagrees with the vectors already stored: dimension, model name and version are all checked.
- **PostgreSQL 17 with pgvector.** It holds documents, chunks and vectors, tabular rows, the normalised entity sidecar, the job queue and the credentials, all in one database. Full-text search and trigram matching are native Postgres features, so there is no second search engine to keep in sync.
- **Microsoft Copilot Studio**, outside the system, is the user-facing agent. It reaches the MCP endpoint over HTTPS with a per-tenant API key, and its generative orchestrator picks tools from their schemas and descriptions.

The HTTP API is the product boundary. The MCP server is a thin wrapper over the same service classes, so both surfaces compute answers the same way.

## 2. Data model

Every data table carries a `tenant_id`. Isolation is enforced in the application layer: every repository method takes the tenant as a parameter, and child rows are joined on both their parent key and the tenant.

| Table | Holds | Tenant scoping |
|---|---|---|
| `tenants` | One row per customer firm | The scope itself |
| `api_keys` | SHA-256 hash of each key, label, created / last used / revoked timestamps | `tenant_id` → `tenants`, cascading delete |
| `documents` | One row per uploaded file: byte hash, extracted-text hash, status (`PENDING` → `EXTRACTING` → `EMBEDDING` → `READY`, or `FAILED` / `DUPLICATE`), ingest mode and effective options, warnings, owner of the in-flight ingest | `tenant_id` → `tenants`; unique on (`tenant_id`, byte hash) |
| `doc_chunks` | Passages: text, token count, page range and printed page labels, heading path, chunk kind (prose or row), a 768-d vector, the model and version that produced it, generated English and "simple" tsvectors | `tenant_id`; **one partial HNSW index per tenant**; GIN indexes on both tsvectors |
| `doc_tables` | One row per table found in a file: the header list (which doubles as the column allow-list for queries), row count, truncation flag and the detected column roles | `tenant_id` |
| `doc_rows` | Raw rows as JSONB cells keyed by (table, row index) | `tenant_id`; (`tenant_id`, table) index, plus optional per-column expression indexes built on demand |
| `doc_row_entities` | Normalised sidecar, one row per data row: raw and normalised name, name stem, normalised address and unit, PO-box and "groupable" flags, ZIP5, rule version | `tenant_id`; partial indexes per matching key; trigram GIN indexes |
| `doc_column_meta` | Column meanings, units and code lists, a statistical profile, and where each came from (human, AI-suggested, AI-suggested then accepted) | `tenant_id` |
| `ingest_jobs` | Per-stage history of every ingest | `tenant_id` |
| `upload_sessions` | Resumable uploads, keyed on (`tenant_id`, file hash) so an interrupted upload resumes where it stopped | `tenant_id` |
| `ingest_tasks` | Queued ingests with the mode and options chosen per file | `tenant_id` |
| `index_maintenance` | VACUUM / REINDEX runs, at most one running per tenant | `tenant_id` → `tenants` |
| `schema_migrations` | Ordered migrations with a checksum per file | Global |

Two choices shape this model. Vectors are never the only copy of anything: rows stay as rows, and text stays alongside its embedding. And no table relies on a shared approximate index for tenant correctness (see §5).

## 3. Ingestion

### Entry paths

Three entry paths lead into one pipeline method:

- a synchronous upload endpoint;
- the console's queued path (resumable 8 MiB chunks, server-side assembly and hash verification, optional archive extraction, a cost estimate, then a queued ingest);
- a command-line folder ingester for bulk loads.

The queue is a table. One asyncio worker per process claims the oldest task with `FOR UPDATE SKIP LOCKED`. A message broker would provide the same guarantee with more moving parts, and running ingests concurrently would double the peak memory of the very step most likely to run out of it.

### Stages

1. **Byte dedupe.** The file's SHA-256 is computed by streaming it. A previous `FAILED` attempt for the same bytes is cleared so that it cannot block its own retry. Any other match returns the existing document.
2. **Extract.** A registry maps each media type to exactly one extractor: PDF (column-aware reading order, outline-derived headings, printed page labels, per-page OCR fallback), DOCX, PPTX (shapes in visual order, plus speaker notes), XLSX, CSV, images (OCR with a confidence floor) and plain text. Extractors never close the caller's stream, and they report problems as structured warnings rather than failing silently.
3. **Content dedupe.** A hash of the whitespace-normalised extracted text catches the same document saved under a different name. A match is marked `DUPLICATE` and never embedded.
4. **Chunk.** Chunks are split on structure (section changes and authored pages are hard boundaries), and tokens are counted with the embedding model's own tokenizer. Table blocks split between rows, carry their header as a repeated prefix, and never overlap.
5. **Memory guard.** Before any embedding work, the guard projects the memory the document's vectors will need, against what the container's cgroup actually allows. A file that will not fit is refused with the number of parts it should be split into.
6. **Embed** in one batched call, with an automatic retry at half the batch size if the GPU runs out of memory.
7. **Store** the chunks. Tables are streamed into `doc_rows` in committed batches, column roles are detected, and the normalisation pass runs (§4).

Every stage writes an `ingest_jobs` row. An exception marks the document `FAILED` with the reason. A startup reaper fails any in-progress document whose owning worker no longer has a database session, which it checks through Postgres's own session list rather than through a timeout.

### Very large CSVs

Delimited files are handled so that file size never turns into memory use:

- **The encoding is validated over the whole file** with an incremental decoder, trying several candidate encodings in order. A sniff of the first few kilobytes cannot decide the encoding of a multi-gigabyte file, and a single stray byte deep inside one was enough to break the earlier approach.
- **Rows are a lazy iterable** over the still-open stream. They are consumed inside the store stage and committed in batches, so the whole file is never materialised.
- **Ingest modes.** `table` embeds one table card (file name, columns and a few example rows) and keeps every row queryable by SQL. `full` additionally embeds the rows as packed chunks, for small tables whose rows need to be found by meaning. `document` stores no tables. Large files default to `table`, and choosing `full` has to be justified.
- **Row-correct splitting.** A file too large for the memory guard is split on row boundaries, not line boundaries, into a power-of-two number of parts balanced by bytes, each carrying the header and written as UTF-8. Cutting a quoted record mid-line would otherwise produce a "valid" row of shifted columns.

## 4. Normalisation

Counting *businesses* rather than *spellings* needs a canonical form. The normaliser is deliberately simple and auditable:

- **Deterministic rewrite, then exact equality.** There is no fuzzy score inside matching. Trigram similarity exists as a separate, explicitly requested filter that reports the threshold it used. The two mechanisms are never mixed, so an exact match can always be explained as exact.
- **Raw values are never overwritten.** Normalised forms live in a sidecar table next to the raw values they came from.
- **Names.** Case and punctuation are folded, `&` becomes `AND`, and dotted or spaced acronyms are rejoined (`L.L.C.`, `L L C` → `LLC`). Legal-form spellings are folded to one canonical form per type (`LIMITED LIABILITY COMPANY`, `L.L.C.` and `LLC` all become `LLC`), and a **stem** with the trailing legal form removed is kept as a separate, opt-in key. Two registrations of one name under different legal forms are a different question from two spellings of one registration, so stem matching is never the default.
- **Addresses.** Street types are expanded only in street-type position, directionals and unit designators are canonicalised, the unit is split off, PO boxes are recognised, and an address is only "groupable" when it is specific enough to mean one place.
- **Blocking.** Every match is blocked on ZIP5, so the comparison space stays bounded and two identical street lines in different towns never match.
- **Versioned.** The rule-set version is stored on every derived row. When rules change, a resumable backfill re-derives older rows, using the rows already written as its checkpoint.
- **Nothing auto-merges.** Groupings are computed at query time and reported with their raw variants, so a person can see why two records were counted as one.

## 5. Retrieval

### Passage questions (hybrid retrieval)

1. **Embed the query** with the model's query instruction. Passages are stored without one, as the model expects.
2. **Vector arm.** Cosine nearest neighbours over the tenant's `READY` chunks. The tenant predicate lets the planner use that tenant's own partial HNSW index. A shared index under a selective tenant filter can end its graph walk before reaching any of a small tenant's rows and return an empty set, which is why the index is per tenant. The search breadth is set per connection so that approximate search can return as many rows as were asked for.
3. **Prose reservation.** In a corpus dominated by spreadsheet row chunks, the vector arm's candidates can be entirely rows. A small number of candidate slots is therefore reserved for prose chunks.
4. **Full-text arm.** Postgres full-text search over a generated tsvector, with terms OR-joined and ranked with length normalisation. This arm finds exact tokens that dense embeddings are bad at: form numbers, codes and citations. If it fails, the search degrades to vector-only instead of failing.
5. **Fusion.** Reciprocal Rank Fusion with equal weights. Fusion uses rank and never score, because cosine similarity and text rank are on unrelated scales. After fusion the cosine score is recomputed for every hit, so that a reported score always means the same thing.
6. **Rerank.** A cross-encoder scores the fused shortlist, and its output passes through a sigmoid to give a 0–1 relevance score. This gives the agent calibrated bands for "answer from this" and "this is not in the collection". Off-topic queries fall from a mean top-1 score of 0.455 to 0.0001 under the reranker.
7. **Neighbour expansion.** When a strong hit opens an enumerated list, the chunks that continue it in the same document are appended, flagged as continuations and placed after their anchor. Expansion is forward-only and runs only when the following chunks really continue the list. Symmetric expansion was measured and made results worse.

### Counting questions (closed grammar)

Counting never goes through vectors. A retrieval call returns a few passages and never a total, and "how many" is not a question a passage can answer. Structured questions go to a closed grammar instead:

- a fixed set of operations (count, distinct count, sum, average, min, max, percentile, dispersion);
- typed filters, grouping and having-clauses;
- one statement across several tables when a split file must be answered as one, because averages do not survive being merged after the fact.

The grammar compiles to parameterised SQL over the JSONB rows. Column names are checked against each table's stored header list, and an unknown column is refused with the real columns listed. Large result pages are clamped as a disclosure control. No SQL is ever generated by a model.

### Scope disclosure

Every result type declares a human-readable `display` line first, because Copilot Studio previews the start of the serialised object when it renders a reference. The line gives what was computed, the value, the denominator and the source files, in that order, so the parts most likely to be cut are the ones a reader can most afford to lose. Every part of it also exists as a typed field:

- matched, measured and total rows;
- whether a table was loaded incompletely (the figure is then a minimum);
- `incomplete`, when a figure was computed over materially fewer rows than the filters matched;
- `unsupported`, a reason given in place of a wrong number;
- for matching operations, a criteria object read back from the statement that actually ran: which fields were compared, how strictly, what blocked the comparison and what was excluded.

## 6. MCP tool surface

The server uses the official MCP Python SDK over **stateless Streamable HTTP**. Every call is independent, which is what Copilot Studio sends, and it lets the middleware pin a tenant per request. DNS-rebinding protection is on, so allowed hosts are configuration. All tools are read-only. There is no upload or write path over MCP, and a test enforces that.

| Tool | Purpose | Returns |
|---|---|---|
| `search_documents` | Passages by meaning (hybrid retrieval above) | Passages with `display`, citation, file, printed page label, heading path, 0–1 score, continuation flag |
| `list_documents` | What is in the collection | Counts by file type (documents, pages, passages), file names, and the files that are **not** searchable with the reason for each |
| `list_tables` | Which tables exist and what their columns mean | Table cards: id, file, row count, columns, truncation flag, recorded column meanings |
| `aggregate_table` | Counting questions through the closed grammar | Value with `display`/citation, matched / measured / total rows, per-group and per-table breakdowns, `incomplete` / `unsupported` |
| `fetch_rows` | The records behind a figure | A page of rows with matched vs returned counts, the limit actually applied and any similarity threshold used |

The production server also exposes domain-specific tools that are out of scope here.

Design rules that apply to every tool:

- **No tool takes a tenant argument.** The tenant comes from the request's API key through a context variable, and reading it outside a request raises an error rather than falling back to a default.
- **Anticipated mistakes come back as readable tool errors** that tell the agent what to do next (for example, "table ids are minted per ingest: call `list_tables` again"), not as stack traces.
- **Citation and display come first** in every response model.
- The server's instructions and tool descriptions are the only lever on the orchestrator's behaviour. They are versioned in git, covered by regression tests and checked against the live server.

## 7. Security model

- **Credentials.** API keys are 256 bits of randomness. Only their SHA-256 is stored, and a fast hash is adequate for keys of that entropy. The plaintext is shown once, at issue. Revocation takes effect on the next request because every request is resolved against the database.
- **The tenant comes from the key, never from the request.** On `/v1`, a tenant header is optional and must agree with the key, otherwise the request gets a 403. On `/mcp`, the middleware sets the tenant and tools cannot name one.
- **Uniform failures.** Missing, unknown and revoked keys get an identical 401. Another tenant's document, table or upload id gets a 404, never a 403, so existence is not disclosed.
- **Data-layer scoping.** Every repository query is parameterised on the tenant, child rows are joined on both keys, and each tenant's vectors have their own partial index.
- **Files.** Upload staging is namespaced per tenant, and any path taken from a request is checked for containment. Archive extraction inspects the whole entry list before writing anything. It refuses absolute paths, `..`, links and device nodes, and enforces entry-count, size and expansion-ratio limits.
- **Onboarding is an operator action.** Unknown tenants are refused, not created. Provisioning builds the tenant's vector index *before* issuing its first key, so a failure leaves a tenant without a credential rather than a credential without an index.
- **No generated SQL.** The aggregation grammar is closed, columns are allow-listed per table, and values are always bound as parameters.
- **Outbound data.** The only outbound model call is an optional, off-by-default feature that drafts column descriptions. It sends column statistics and a handful of sample values, never whole rows, shows the exact payload and its cost first, and never applies a suggestion without confirmation.

## 8. Evaluation and quality gates

- **Retrieval fixture.** Questions over public documents, with expected answers written as (file, page) pairs, never chunk ids, so the fixture survives re-chunking. Off-topic probes measure whether the system can say "not here".
- **Metrics.** recall@5, recall@15 and MRR, each with an interval (Wilson for recall). At this fixture size, a difference of less than about 10 points is not treated as real.
- **Gate.** After an ingest, the gate checks that the tenant's index is present, then that approximate search agrees with an exact scan on sampled probes, then that the metrics have not regressed against a stored baseline by more than 0.02.
- **Agent behaviour.** Wording regression tests, scripted transcript replays against the live MCP server, and a demo-question catalogue read by people. Answer quality is judged by a person, not by a model.
