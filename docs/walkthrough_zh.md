# 开发讲解（中文）

每个阶段结束时追加一节。关键术语保留英文。

---

## P0 骨架（回顾）

- 建了 FastAPI app、SQLite schema（companies / documents / chunks / metrics / crm_records / agent_logs）、统一的 `llm/` 接口（`chat`、`complete_json`、`tool_results_message`）、Anthropic 实现和 FakeLLM。
- FakeLLM 按脚本顺序返回回复，并把每次调用记在 `.calls`，用于确定性测试。
- `pytest -m live` 在填好 key 后真实运行过：`1 passed`。

## P1 导入（回顾）

- 上传 → 校验元数据 → 解析切块（PDF 页+段、TXT/MD 段、CSV 行）→ 一个 transaction 写库。同名文件替换旧版本；替换文件有错时旧版本保留。
- CRM CSV 固定列 `date, contact, summary, next_steps`，缺列报清楚的错误。
- 测试 43 passed，并用真实 uvicorn + curl 验证过上传。

---

## P2 RAG 混合检索

### 改了什么
- 新增 `app/rag.py`：
  - `Embedder`：封装 sentence-transformers，默认 `all-MiniLM-L6-v2`（由 `EMBEDDING_MODEL` 配置）。首次运行下载，之后读 Hugging Face 缓存。加载失败抛 `EmbeddingError`，消息里写明模型名和原因。
  - `tokenize()`：BM25 用的分词。
  - `search()`：混合检索。
- `ingest_document` 先算好 embedding 再开 transaction 写库；替换文件时新 chunk 自然重新计算。
- `chunks` 表加了 `embedding_model` 列；搜索时如果发现缺 vector 或 vector 来自别的模型，会先补算（backfill）。
- 新接口 `GET /api/companies/{id}/search?q=...&k=6`（调试用）。

### 一次搜索从输入到输出
1. 检查公司存在、`q` 非空。
2. backfill：补算这家公司缺失的 vector。
3. SQL `WHERE ch.company_id = ?` 取出**这一家公司**的全部 chunk。别家公司的数据从这一步起就不在候选集中，所以不可能被排进结果。
4. BM25：对这些 chunk 临时建 `BM25Okapi`，算每个 chunk 的分数。只有和查询至少共享一个词（分数 > 0）的 chunk 才有 BM25 排名。
5. 向量：把查询 embed 成 vector，和每个 chunk 的 vector 做点积（vector 已归一化，点积就是 cosine），全部排名。
6. RRF 合并：`score = 1/(60 + vector_rank) + 1/(60 + bm25_rank)`；没有 BM25 排名的只有前一项。
7. 按 score 降序取 top k（默认 6）。每条结果返回 `chunk_id, document_id, filename, doc_type, loc_label, text, bm25_rank, bm25_score, vector_rank, vector_score, score`。

### 分词规则
- 转小写；日期 `2026-06-30` 和数字 `100`、`1.2` 整体保留，`1,250` 变成 `1250`。
- 去掉一小组英文停用词，复数做简单还原（customers → customer，businesses → business）。
- 额外规则：英文写的日期（`June 30, 2026`）再生成一个 ISO token `2026-06-30`。原因：调试时发现查询 `2026-06-30` 一个 BM25 命中都没有，因为原文是英文写法。

### 为什么 BM25 和向量要一起用
- BM25 擅长**精确词**：数字、日期、专有名词（"410"、"2026-06-30"、"Kitebridge"）。向量模型对数字几乎不敏感。
- 向量擅长**意思相近但用词不同**：查询 "last call" 和原文 "Intro call / Meeting" 用词不同，向量仍能把它们排上来。
- 两者互补。RRF 只看排名，不看原始分数：BM25 分数没有上限，cosine 在 -1 到 1 之间，量纲不同，直接相加没有意义；排名可以直接相加。

### RRF 真实例子（查询 "paying customers"，公司 Tidewell，初始 3 份文件）
| 排名 | chunk | BM25 rank | vector rank | RRF score |
|---|---|---|---|---|
| 1 | meeting_notes ¶4 "Most paying customers are restaurants…" | 1 | 1 | 1/61 + 1/61 = 0.03279 |
| 2 | meeting_notes ¶3 "…Tidewell had 60 paying customers…" | 2 | 2 | 1/62 + 1/62 = 0.03226 |
| 3 | deck p.2 ¶2 "The Problem…" | — | 3 | 1/63 = 0.01587 |

两种排名都同意的 chunk 分数大约翻倍，会明显排在只被一种方法找到的 chunk 前面。

查询 "registered customers" 时，deck p.3 ¶2（"100 small businesses have registered"）是 BM25 第 2、vector 第 6，RRF 后排第 3。这正是两种方法互补的效果：只用 vector 它排不进前 3。

### 错误处理
- embedding 模型加载失败：上传和搜索都返回 HTTP 503，并附带清楚的消息。上传时 embedding 在写库之前计算，所以失败不会损坏旧数据。
- `q` 为空返回 400；公司不存在返回 404。

### 怎么验证的（真实测试输出）
```
64 passed, 1 deselected in 10.74s
```
P2 新增的测试（`tests/test_rag.py` 和 API 测试）：
- **公司隔离**：8 个查询 × 2 家公司，`top_k=50`（等于取回全部结果），每条结果的 `company_id` 都是被查询的那家，且文件名不含另一家的前缀。
- **检索质量**（真实 embedding 模型）：
  - "paying customers" 前 3 含 `tidewell_meeting_notes.md ¶3`。
  - "registered customers" 前 3 含 `tidewell_deck.pdf p.3 ¶2`。
  - "last call next steps" 前 3 含 CRM 行（实际前 3 全是 CRM 行）。
  - "2026-06-30" 的 BM25 命中 deck 和会议笔记的那两段。
- **替换**：同名文件替换后，旧 chunk_id 和旧文本都不再出现在结果里。
- **RRF 数学**：用一个确定性的 stub embedder，验证每条结果的 score 都等于公式算出的值。
- **其他**：vector 是 384 维且归一化；缺失的 vector 在搜索时会被补算；模型名错误时报 `Could not load embedding model '...'`；接口的 400/404/503。

---

## P3 指标抽取 + 引用校验 + 矛盾/缺失规则

### 改了什么
- `app/llm/`：模型分成两套配置：`LLM_EXTRACT_MODEL`（默认 `claude-opus-5-5`，用于抽取）和 `LLM_CHAT_MODEL`（默认 `claude-sonnet-5-5`，用于问答和简报）。调用 `get_llm("extract")` 或 `get_llm("chat")` 取对应模型。
- 缺少凭证时，SDK 抛的 `TypeError` 和 401 都转成 `LLMAuthError("API key missing ...")`。
- 新增 `app/numbers.py`（数字标准化）、`app/citations.py`（引用逐字校验）、`app/extract.py`（指标抽取）、`app/rules.py`（矛盾/缺失/unclear basis）、`app/logs.py`（写 `agent_logs`）。
- 新接口：
  - `GET /api/companies/{id}/metrics`
  - `GET /api/companies/{id}/checks`（返回 conflicts / unclear_basis / missing）
  - `POST /api/documents/{id}/extract`（重新抽取）
- 上传接口在存好文件后自动抽取指标，响应里带 `extraction` 字段。

### 输入 → 输出（一次抽取）
1. **输入**：一个 document 的全部 chunk。prompt 里每段写成 `[chunk_id=16] (tidewell_meeting_notes.md ¶3)` 加原文。一个文档只调用模型一次（`complete_json`，用 JSON schema 强制输出格式）。
2. **模型返回**：每条指标包含 `chunk_id, metric, metric_label, value_raw, unit, currency, as_of_date, quote`。
3. **程序校验，任何一条不通过就丢弃，并写入 `agent_logs`**：
   - `chunk_id` 必须属于这个文档。
   - `quote` 在空白规范化后必须**逐字**出现在该 chunk 里（大小写、用词都不能改）。
   - `value_raw` 必须出现在 `quote` 里。
4. **程序补全和标准化**：
   - `value` 由程序根据 `value_raw` 计算（`$1.2M` → 1200000，`sixty` → 60，`1,250` → 1250）；算不出来就是 NULL。
   - unit：客户数统一为 `count`；金额类用币种（USD）。
   - `as_of_date` 只有当这个日期确实写在同一个 chunk 里时才保留，否则为 NULL，即 unclear basis。
   - company、document、位置都从数据库取，不信任模型给的值。
5. **输出**：替换这个文档原有的 metrics，并返回 `{proposed, kept, dropped:[{item, reason}]}`。

### 程序规则（`app/rules.py`）
- **矛盾**：同公司 + 同 metric（`other` 还要同 label）+ 同 as_of_date + 同 unit/currency + 数值不同。返回所有数值和每个来源（quote、loc_label、文件），不替人选哪个是对的。
- **不同 metric 永远不比较**：注册 100 和付费 60 不会被判为矛盾。
- **unclear basis**：没有 as_of_date，或数值读不出来。在界面上显示，但不参与矛盾判断。
- **缺失**：4 个关键指标里一条记录都没有的。只有 unclear basis 的指标不算缺失。

### 错误处理
- 抽取时模型失败（没有 key、网络、API 错误）：文件仍然保存，响应是 `extraction: {"status": "failed", "error": "API key missing: ..."}`，之后可以调用 `/extract` 重试。
- 模型给出编造的 quote、错误的 chunk_id、引文里不包含的数值：这条被丢弃，并写一条 `metric_dropped` 日志。

### 怎么验证的
- **FakeLLM 测试**：`tests/helpers.py` 里的 `fake_extractor` 模拟一个"认真的模型"，从 prompt 里找到真实的 chunk_id 再返回指标。用它在示例数据上跑完整的 API 流程：
  - 注册 100 vs 付费 60 → `conflicts == []`。
  - 上传第 4 份后 → 1 个矛盾：`paying_customers`，2026-06-30，values `[60, 80]`，两个来源分别是 `tidewell_meeting_notes.md ¶3` 和 `tidewell_update_2026-08.txt ¶2`。
  - `$1.2M` → unclear basis，值 1200000，原因 "no as-of date stated"，位置 `tidewell_deck.pdf p.4 ¶2`。
  - `missing == ["revenue"]`。
  - Kitebridge 的指标不会混进 Tidewell，反之亦然。
  - 替换第 4 份文件后，矛盾消失。
  - 模型没有 key 时，上传仍然成功。
- **校验单元测试**：编造的 quote、`value_raw` 不在 quote 里、chunk 属于别的文档 → 都被丢弃并有日志；日期没写在 chunk 里 → 变为 NULL；数字标准化有 13 个正例和 3 个"拒绝猜测"的例子。
- **真实模型测试（Opus 5.5，`pytest -m live`）**：对 Tidewell 的 deck、notes 和第 4 份做真实抽取，提出的 6 条指标全部通过校验，0 条被丢弃：
```
tidewell_deck.pdf {'status': 'ok', 'proposed': 4, 'kept': 4} []
tidewell_meeting_notes.md {'status': 'ok', 'proposed': 1, 'kept': 1} []
tidewell_update_2026-08.txt {'status': 'ok', 'proposed': 1, 'kept': 1} []
registered_customers 100.0 2026-06-30 tidewell_deck.pdf p.3 ¶2
paying_customers 60.0 2026-06-30 tidewell_meeting_notes.md ¶3
paying_customers 80.0 2026-06-30 tidewell_update_2026-08.txt ¶2
funding_raised 1200000.0 None tidewell_deck.pdf p.4 ¶2
conflicts: paying_customers 2026-06-30 values [60.0, 80.0]（两个来源）; missing: ['revenue']
1 passed, 104 deselected in 18.49s
```
  模型另外抽出了两条 `other`（"4 minutes"、"two states"），它们没有统计日，因此显示在 unclear basis 里。
- **全部离线测试**：`103 passed, 1 deselected`。
- **一个偶发问题**：同一轮 live 测试里，小的 JSON 测试（Sonnet 5.5）报了一次网络超时 "Request timed out or interrupted"。单独重跑通过，直接调用 Sonnet 也正常，判断是网络偶发问题，不是代码问题。

---

## P4 Agent 问答

### 改了什么
- 新增 `app/agent.py`：tool use 循环、4 个工具、引用校验、相关矛盾的附加。
- 新接口：`POST /api/companies/{id}/ask {"question": ...}` 和 `GET /api/runs/{run_id}/log`（查看一次运行的完整日志）。
- 问答使用 `LLM_CHAT_MODEL`（默认 `claude-sonnet-5-5`）。
- `tool_results_message` 新增可选的 `note` 参数，用于在最后一步前提醒模型。

### 一次提问的完整流程
1. 前端发来 `question` 和 URL 里的 company_id。
2. `run_agent`：把问题发给模型，同时提供 4 个工具：
   - `search_documents(query)`：混合检索，返回 chunk_id、位置和原文。
   - `get_metrics(metric?)`：返回指标，每条带 chunk_id 和 quote。
   - `get_conflicts()`：返回程序判定的矛盾，以及 unclear basis 的指标。
   - `get_crm_history()`：返回 CRM 记录，按日期从早到晚排序。
3. 模型返回 tool_use 时，服务端执行工具。**company_id 永远由服务端传入**：工具 schema 里根本没有这个参数；模型硬传的话会被删掉并记一条日志。
4. 每次工具调用都写进 `agent_logs`（输入、输出），可以通过 `/api/runs/{run_id}/log` 查看。
5. 最多 6 次模型调用。第 5 次之后返回工具结果时，附一句 "You have one step left…"，要求模型直接给出答案。
6. 模型的最终输出是 JSON：`{"claims": [{"text", "citations": [{"chunk_id", "quote"}]}], "open_items": [...]}`。解析失败时会提醒一次，让模型重发。
7. **程序校验每条引用**：quote 必须逐字出现在 chunk 中，且这个 chunk 属于本公司。不通过的引用被丢弃并记日志；一条 claim 的引用全部失败，这条 claim 也被丢弃。
8. 如果答案里引用的 chunk 恰好是某个矛盾的来源，程序会把这个矛盾（所有数值和来源）附在答案上。
9. 返回 `claims`（每条引用带 filename、loc_label、quote、chunk_text，供前端展开查看证据）、`open_items`、`conflicts`、`unclear_basis`、`tool_calls`、`run_id`。

### 错误处理
- 没有 API key：HTTP 503，消息以 `API key missing` 开头。SDK 原本抛的 `TypeError` 已在 `AnthropicLLM` 里转成 `LLMAuthError`。
- 其他模型错误：HTTP 502。
- 工具出错（参数为空、工具名不存在）：错误以 `is_error` 的形式交回给模型，它可以自己纠正。
- 用满 6 步、模型拒答、答案无法解析：都会生成一条 open item 说明原因，不会伪造答案。

### 怎么验证的
**FakeLLM 测试（`tests/test_agent.py`，全部离线）**：
- 工具只返回所选公司的数据；`get_metrics` 带 chunk_id 和 quote；CRM 记录按日期排序。
- 完整循环：先搜索，再引用 → 证据位置是 `tidewell_meeting_notes.md ¶3`。
- 模型传入 B 公司的 company_id → 工具仍然只返回 A 的文件，日志里有 `company_id_ignored`。
- 模型一直调用工具 → 正好 6 次调用后停止；第 6 次调用前的工具结果带有 "one step left" 提示。
- 编造的 quote（"600 paying customers"）和不存在的收入引用被丢弃，日志里有 2 条 `citation_dropped` 和 1 条 `claim_dropped`。
- 引用 B 公司的 chunk → 被丢弃，最终答案是 "Not found in the provided materials"。
- 引用的 chunk 属于某个矛盾 → 答案附上 `[60, 80]`。
- 第一次回答不是 JSON → 提醒一次后成功解析；工具报错能被模型看到。
- API：`/ask` 正常返回；`/runs/{id}/log` 能看到工具调用；没有 key 时返回 503 "API key missing"。

**真实模型测试（Sonnet 5.5，`pytest -m live -k real_agent`）**：示例数据（指标由 FakeLLM 抽取，问答由真实模型完成）的输出：
```
Q: How many paying customers does the company have?
  CLAIM: As of June 30, 2026, the company had 60 paying customers. | ['tidewell_meeting_notes.md ¶3']
Q: When was our last conversation with the company, and what are the next steps?
  CLAIM: The last logged conversation was on 2026-07-22 with Dana Whitfield, a meeting with the investment team. | ['tidewell_crm.csv row 3']
  CLAIM: The next step from that meeting is to ask for revenue figures and loss ratio data. | ['tidewell_crm.csv row 3']
Q: What is the company's revenue?
  CLAIM: The company's revenue figures were not shared in the meeting. | ['tidewell_meeting_notes.md ¶7']
  CLAIM: The CRM log lists asking for revenue figures as a next step. | ['tidewell_crm.csv row 3']
  OPEN: ['Revenue: Not found in the provided materials']
Q: How many paying customers ...（上传第 4 份之后）
  CLAIM: The meeting notes (dated 2026-07-22) say ... 60 paying customers as of June 30, 2026. | ['tidewell_meeting_notes.md ¶3']
  CLAIM: The August 2026 update (dated 2026-08-10) says ... 80 paying customers ... | ['tidewell_update_2026-08.txt ¶2']
  CONFLICTS: [('paying_customers', [60.0, 80.0])]
4 passed, 120 deselected in 28.45s
```
**开发中修过的问题**：第一次跑 live 测试时，revenue 问题把 6 步全用在搜索上，状态是 `step_limit`。测试当时显示通过，只是因为 step-limit 提示里有 "not found" 这几个字，那次通过不算数。加上"最后一步提醒"之后，模型正常给出了答案，live 测试现在也会断言 `status == "ok"`。

**全部离线测试**：`118 passed, 6 deselected`。

---

## P5 一键简报

### 改了什么
- 新增 `app/brief.py` 和接口 `POST /api/companies/{id}/brief {"requirements": "focus on traction and risks"}`，返回 JSON，外加一个 `markdown` 字段，方便复制或下载。

### 输入 → 输出
- **输入**：company_id 和用户的要求。要求为空时，使用默认要求："General overview: traction, recent communications, risks and open questions."
- **程序生成的部分（确定性的，每条都带出处）**：
  - `key_metrics`：4 个关键指标里有统计日、有数值的记录，带 quote 和位置。
  - `recent_communications`：最近 3 条 CRM 记录（从新到旧），quote 是整行原文。
  - `missing`：缺失的关键指标，例如 `revenue: Not found in the provided materials`。
  - `conflicts`：所有矛盾，列出全部数值和来源。
  - `unclear_basis`：没有统计日的指标。
- **模型写的部分**：用和问答相同的 tool 循环（`run_agent`，最多 6 步），只是 system prompt 不同。模型按要求写 2–4 个 section，每条 bullet 都必须带 chunk_id 和逐字 quote。程序用 `verify_items` 逐条校验，不通过的 bullet 被丢弃；一个 section 里的 bullet 全被丢弃时，整个 section 也去掉。
- **输出**：`sections, key_metrics, recent_communications, missing, conflicts, unclear_basis, open_items, tool_calls, run_id, markdown`。

### 错误处理
- 没有 key：503 "API key missing"；其他模型错误：502。
- 模型写的部分没能完成（step_limit / refused / unparseable）时：程序生成的部分照常返回，`open_items` 里说明写作部分不完整。

### 怎么验证的
**FakeLLM 测试（`tests/test_brief.py`）**：
- 用户的要求出现在发给模型的 prompt 里。
- 编造的 bullet（"Revenue grew 3x."）和没有引用的 bullet 被丢弃；整节都无效的 "Risks" section 不显示。
- 程序部分：关键指标是 {注册 100, 付费 60, 付费 80}；CRM 记录按 07-22、07-15、06-05 排序；缺失项 = revenue；矛盾 = paying_customers [60, 80]；unclear basis = funding_raised。
- **"每条都带出处"**：遍历简报里所有带来源的条目（section bullet、指标、CRM、unclear、矛盾的每个来源），逐一检查 quote 确实出现在对应的 chunk 里。
- Markdown 导出包含所有出处；接口能正常返回。

**真实模型测试（Sonnet 5.5，要求 "focus on traction and risks"，4 份文件都已上传）**，输出节选：
```
## Traction
- The deck reports 100 registered small businesses on the platform as of June 30, 2026; this is a registration count, not a paying-customer count. [tidewell_deck.pdf p.3 ¶2: "..."]
- Paying customers as of June 30, 2026 are 60 in the meeting notes. [tidewell_meeting_notes.md ¶3: "..."]
- The August 2026 update gives 80 paying customers as of the same date, so the two sources conflict. [tidewell_update_2026-08.txt ¶2: "..."]
## Risks
- The paying-customer count is inconsistent across company materials (60 vs 80). ...
- Revenue figures and loss ratio data had not yet been provided. ... [tidewell_crm.csv row 3: "Ask for revenue figures and loss ratio data."]
## Missing information
- revenue: Not found in the provided materials
## Conflicting information
- paying_customers as of 2026-06-30: values differ, not resolved: 60 [...¶3] / 80 [...¶2]
1 passed, 129 deselected in 18.24s
```
模型主动区分了注册数和付费数，矛盾两边都列出，没有替人选择。这次模型调用了一个不存在的工具 `json`，收到错误后自己恢复，继续完成了简报。

**全部离线测试**：`123 passed, 7 deselected`。
