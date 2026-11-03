# Decisions log

Choices made without waiting for confirmation, with reasons. Newest at the bottom.

| # | Phase | Decision | Reason |
|---|-------|----------|--------|
| 1 | P0 | Conversation messages stay in the provider's native format; callers append `response.assistant_message` verbatim. | Claude's thinking blocks must be sent back unchanged; a neutral re-encoding would drop them. Swapping providers only touches `app/llm/`. |
| 2 | P0 | Server-side refusal fallback (`fallbacks: "default"`) on by default, `LLM_FALLBACKS=` disables it. | Recommended for current Claude models; a refused request is retried on a fallback model inside the same call. |
| 3 | P1 | Paragraph = block separated by blank lines; a heading (Markdown `#`, or one short line with no closing punctuation) is attached to the next paragraph. | Keeps section titles ("Traction") with the numbers below them, which helps both retrieval and the reader. Simple to explain. |
| 4 | P1 | PDF text is extracted with pypdf `extraction_mode="layout"`. | Default mode loses blank lines between paragraphs; layout mode keeps them, so paragraph numbers match what a reader sees. |
| 5 | P1 | Extra CSV columns are allowed and kept in the chunk text; only the 4 fixed CRM columns are required. | Real CRM exports have extra columns; rejecting them would be annoying. |
| 6 | P1 | `AUTOINCREMENT` IDs on documents/chunks/metrics/crm_records. | SQLite otherwise reuses the ID of a just-deleted row, so after a file replacement an old citation could point at different text. |
| 7 | P1 | `DB_PATH` relative paths resolve against the project root. | Starting the server from another directory must not silently create a second, empty database. |
| 8 | P2 | BM25 ranks only chunks with score > 0; chunks with no shared term get no BM25 contribution in RRF. | Otherwise zero-score chunks would get arbitrary BM25 ranks and noise in the fused score. |
| 9 | P2 | RRF with k = 60, top_k default 6. | Standard RRF constant; rank-based, so BM25 and cosine scores need no normalization. |
| 10 | P2 | Tokenizer: lowercase, keep dates/numbers whole (`1,250` → `1250`), small stopword list, light plural folding, plus an extra ISO token for written dates (`June 30, 2026` → `2026-06-30`). | Queries mix ISO dates and numbers with prose; without the ISO token a `2026-06-30` query matched nothing in the samples. |
| 11 | P2 | Embedding model cache = default Hugging Face cache (`~/.cache/huggingface`). Vectors store the model name; search re-embeds chunks with a missing or different-model vector. | Downloaded once, reused afterwards. Changing `EMBEDDING_MODEL` never mixes incompatible vectors. |
| 12 | P3 | Model config split: `LLM_EXTRACT_MODEL` (default `claude-opus-5-5`) and `LLM_CHAT_MODEL` (default `claude-sonnet-5-5`); one shared `LLM_EFFORT`. | Requested split; extraction accuracy matters most, chat is called more often. |
| 13 | P3 | The model returns only `value_raw`; the program computes `value` (`app/numbers.py`: "$1.2M", "1,250", "sixty", "2.5bn"). Unreadable → `value = NULL`, shown as unclear, never compared. | "Rules belong to the program." The conflict check depends on this number, so it must be deterministic and testable. |
| 14 | P3 | Company, document and location of a metric come from the database (via `chunk_id`), never from the model. | The model could otherwise attach a number to the wrong document or company. |
| 15 | P3 | Units: count metrics → `count`; money metrics → currency code (from the model or the `$/€/£` symbol); `other` → model unit, lowercased. | The model might say "businesses" in one file and "customers" in another; without fixed units the 60 vs 80 conflict could be missed. |
| 16 | P3 | `as_of_date` is kept only if that date is written in the same chunk (ISO or "June 30, 2026"); otherwise it becomes NULL ("unclear basis"). | Stops the model from silently using the document date as the statistic date. |
| 17 | P3 | A metric item is dropped (and logged in `agent_logs`) if: chunk not in this document, quote not verbatim in the chunk, or `value_raw` not inside the quote. | Every stored number must be traceable to real source text. |
| 18 | P3 | Extraction runs after the upload is stored. If it fails (no key, API error), the upload still succeeds, the response carries `extraction.status = "failed"`, and `POST /api/documents/{id}/extract` retries. | Losing an uploaded file because the model was unreachable would be worse than a missing metric. |
| 19 | P3 | "Missing" = a key metric (`registered_customers`, `paying_customers`, `revenue`, `funding_raised`) with no stored metric at all. A metric with only unclear basis is not missing. | It exists in the materials; the problem is its basis, which is shown separately. |
| 20 | P3 | Agreement (same value from two sources) is not a conflict. | Only different values for the same basis need a human decision. |
