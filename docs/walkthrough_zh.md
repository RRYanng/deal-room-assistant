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
