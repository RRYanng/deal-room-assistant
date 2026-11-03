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
