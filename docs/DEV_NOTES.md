# 开发笔记（给下一个开发会话）

这份笔记记录在代码里看不出来的东西：怎么跑、端口怎么分、浏览器走查的边界、踩过的坑和开发规则。
功能和设计理由分别见 `README.md`、`docs/decisions.md`，当前版本状态和恢复步骤见 `docs/STATE_v1.1.md`。

## 项目结构

```
app/
  main.py         FastAPI 路由（单文件上传、批量上传、问答、Brief、Issues……）
  config.py       读环境变量和项目根目录的 .env；DB_PATH 的相对路径相对项目根目录
  db.py           SQLite 表结构（ID 用 AUTOINCREMENT，替换文件不会复用旧 ID）
  ingest.py       解析、切块（保留页码/段落/行号）、存储；同名文件替换（保留旧日期）
  batch.py        批量上传：后台单线程，先全部存储，再逐个抽取指标；进度只在内存里
  intake.py       上传建议（公司、类型、日期）和一致性警告
  resolve.py      公司名匹配；问题是关于哪家公司
  rag.py          embedding + BM25 混合检索（RRF）
  extract.py      指标抽取和校验
  rules.py        冲突、依据不明、缺失指标（Issues 的来源）
  agent.py        问答工具循环和引用核对
  brief.py        Brief（程序部分 + 经过核对的模型部分）
  citations.py    逐字引用核对
  numbers.py      数字归一化
  logs.py         agent_logs（每次提问都会写一条，所以数据库哈希会变）
  llm/            模型接口：Anthropic 客户端、FakeLLM
static/           前端：index.html、app.js（原生 JS）、style.css
samples/          SYNTHETIC SAMPLE 样例文件 + manifest.json
                  company_a = Tidewell（含第 4 份 tidewell_update_2026-08.txt，用来制造 60 vs 80 冲突）
                  company_b = Kitebridge
scripts/          make_samples.py、load_samples.py（加载 demo 数据）、verify.py（7 项检查）
tests/            pytest；conftest.py 默认把模型换成 FakeLLM
docs/             决策记录、经验教训、demo 脚本、截图、STATE_v1.1.md、本文件
data/             SQLite 数据库（git 忽略）
```

## 启动和测试

首次安装：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env    # 然后在 .env 里填 ANTHROPIC_API_KEY
```

启动 demo：

```bash
.venv/bin/uvicorn app.main:app --port 8000
```

重置 demo 数据（会删除库里所有公司和文件，并重新调用模型抽取指标）：

```bash
rm -f data/deal_room.db && .venv/bin/python scripts/load_samples.py
```

测试：

```bash
.venv/bin/python -m pytest            # 离线，FakeLLM，不调 API（v1.1 时 274 个）
.venv/bin/python -m pytest -m live    # 调真实模型（9 个），花钱，约 1 分钟
```

- 修 bug 时，先确认新测试在旧代码上会失败：`git stash push <改过的 app 文件>`，跑新测试，再 `git stash pop`。
- `scripts/verify.py` 会重写 `docs/verification.md`，v1.1 时没有重跑。

## 端口

| 端口 | 用途 | 规则 |
|---|---|---|
| 8000 | demo，main 的代码 + `data/deal_room.db` | 演示用，不要在上面做会改数据的测试 |
| 8001 | juanzong（另一个独立项目） | 不属于本项目，不要停、不要重启、不要用 |
| 8002 | 开发测试，配数据库副本 | `DB_PATH=data/<副本>.db .venv/bin/uvicorn app.main:app --port 8002` |
| 8003 及以上 | 临时（例如验证恢复步骤） | 用完就停 |

启动前先查端口有没有被占用：`lsof -iTCP:<端口> -sTCP:LISTEN -n -P`。

## 浏览器走查：能做和做不到的

内置浏览器（`mcp__Claude_Browser__*`）：

**能做**
- 真实点击按钮、链接，真实键盘输入文本框。
- 原生 `<select>`：先点击它，再用 `key` 按一次选项的首字母（例如 `t` 选 Tidewell、`k` 选 Kitebridge），最后按 `Return`。
- 日期输入框：点到月份那一段，按 `BackSpace` 清空，再按 `Tab` 到下一段继续清空，三段都能清掉。
- 用 `find` / `read_page` 拿元素 ref 来点击；用 `javascript_tool` 读状态做检查（只读）。
- `resize_window` 设成 1280x900 才能看清批量确认表，测完记得设回 `desktop`。

**做不到**
- **系统文件选择框**：没法驱动，也就测不了 Cmd 多选和真实拖拽。
  - 替代做法：把样例文件临时复制到 `static/_test/`，在页面里 `fetch` 回来，构造 `File` 放进 `#upload-file`，再触发 `change` 事件。
  - 只有这一步用脚本，后面的确认框和按钮都要真实点击。
  - 汇报时必须说明这一步用了脚本，提交前删掉 `static/_test/`。
- **下拉选项 "+ New company…"**：首字母按键选不到，只能用 `form_input` 设置，名字本身可以真实键入。
- `type` 动作和 `Down Down Return` 都改不了原生 select。
- **原生 `confirm()`**：被浏览器拦截，直接返回 false，不报错。所以 UI 里所有确认都做成了页面内的 dialog，有测试禁止使用原生弹窗。
- **点上传区域**：可能在 Claude 应用里弹出一个系统文件框，没人处理就一直开着。需要时请用户点取消。
- **Claude in Chrome**：之前一直没连上。
- **computer-use**：浏览器只有只读权限，不要绕过这个限制。

**容易点错的地方**
- **坐标基准**：用坐标点击时，以最近一次截图的坐标系为准；ref 点击报告的坐标是另一套，不要混用。
- **表格变高**：批量上传完成后，结果行会让表格变高，按钮位置跟着变，之前截图里的坐标就点不中了。
  - 关键按钮（Upload all、Keep working、Close）用 ref 点，或者重新截图再点。
  - 点完要检查状态，确认真的生效了。
- **Escape**：会取消整个批量确认表，这是设计如此，取消后要重新选文件。

## 踩过的坑

- **前后端版本不一致**：页面文件（`static/`）每次请求都从磁盘读，Python 代码只在服务启动时加载。
  - 在服务运行时切换分支或合并，页面会变成新的，接口还是旧的。
  - 所以合并到 main 之后必须重启 8000。
  - 开发分支只用 8002，或者用 worktree。
- **`.env` 只在启动时读**：改了 key 要重启服务。
- **启动后第一次上传要约 15 秒**：在加载 embedding 模型，界面会一直显示 Uploading。
- **浏览器缓存**：早期浏览器用了缓存的旧 JS，导致页面崩溃。现在 CSS/JS 的链接带内容哈希，页面启动失败时会显示红色提示。
- **SQLite 复用 ID**：不加 AUTOINCREMENT 时，删掉的 ID 会被复用，旧引用会指向新内容。现在已经修好，并有回归测试。
- **替换同名文件清空日期**：新日期为空时会把旧日期清掉（v1.1 已修复）。改替换逻辑时注意保留旧文件的属性。
- **批量上传的行数据**：行对象的字段要始终存在（初始值用 None），不要中途加字段或删字段，否则前端渲染会出错。
- **备份数据库**：服务运行时用 `sqlite3 data/deal_room.db ".backup '<目标>'"`，不要直接 `cp` 一个正在写的库。
- **数据库哈希**：每次提问都会写 agent_logs，所以哈希会变。比对数据要比文件和 Issues，或者和备份比哈希。
- **zsh 引号**：`git rev-parse v1-demo^{commit}` 在 zsh 里要加引号，否则会报 "Needed a single revision"。
- **模型调用失败**：一定要报成错误，不能伪装成正常结果（例如 "Which company do you mean?" 或 "No company recognized"）。

更早的问题见 `docs/lessons_learned.md`。

## 开发规则

- **分支**
  - 不直接在 main 上开发。新功能用 `feature/<名字>` 分支。
  - 有风险的改动之前，先给 main 打 tag 作为回退点（现有 `v1-demo`、`v1.1`）。
  - 用户确认之前不合并、不推 main。
  - 合并后分支可以删除，但要先确认它已完全合并：`git merge-base --is-ancestor <分支> main`。
- **worktree**：想让 8000 的 demo 完全不受影响，就在 worktree 里开发：
  ```bash
  git worktree add ../deal-room-<名字> -b feature/<名字>
  cp .env ../deal-room-<名字>/.env
  cp data/deal_room.db ../deal-room-<名字>/data/dev.db
  cd ../deal-room-<名字>
  DB_PATH=data/dev.db ../deal-room-assistant/.venv/bin/uvicorn app.main:app --port 8002
  ```
  - worktree 里没有 `.venv`、`.env` 和 `data/`（都被 git 忽略），所以要像上面这样复制，并借用主目录的 venv。
  - 用完删掉：`git worktree remove ../deal-room-<名字>`。
- **数据库副本**
  - 测试永远用副本，例如 `data/batch_test.db`、`data/dev.db`，用 `DB_PATH` 指定。
  - `data/deal_room.db` 是 demo 数据：Tidewell 3 个文件、Kitebridge 3 个文件，Issues 一共 4 个。
  - 测试结束删掉副本。
- **合并后**：重启 8000，然后检查以下几项：
  - 页面能启动；
  - Issues 是 4；
  - 两家公司各有 3 个文件；
  - 问 "How many paying customers does Tidewell have?" 答 60。
- **提交**
  - 提交信息用英文，末尾加 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。
  - 推送需要用户明确要求。
- **保密**
  - 仓库是 public 的。API key 只放在 `.env`（git 忽略），代码和文档里不写 key，也不写本机绝对路径。
  - 输出里出现 key 要打码。
- **产品语言**：界面和产品文档都用英文，样例数据标 `SYNTHETIC SAMPLE`，不硬编码 demo 答案。
- **汇报**
  - 没跑过的不说能用。
  - 用了脚本代替真实操作的步骤要说明。
  - 测试结果按实际输出报告。
