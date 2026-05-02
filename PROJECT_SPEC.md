# 软著申请材料自动生成系统 — 项目规格

> 本文档是项目**唯一权威规格**。所有架构决策、字段定义、接口、约定全部以此为准。
> 新 Claude 会话从头接手本项目时，**先完整读本文档**，再读 TaskList 看进度。

---

## 1. 业务背景

用户做软著代理业务，已有一批被中国版权保护中心驳回的案例（根因：申请表字段填错、三份材料互相打架）。本系统目标：

**给定公司名 + 统一社会信用代码 + 成立日期 + 软著数量**，批量生成 N 份**可直接提交、且能过审**的标准软著 zip。

---

## 2. 2025 软著合规硬约束（必须遵守）

| 项目 | 要求 |
|---|---|
| 申请表 | 固定 4 页格式；手抄声明区/签字/身份证号/经办人/盖章位置全部留空 |
| 源代码 PDF | ≥ 3000 行（前 30 页 + 后 30 页，每页 ≥ 50 行） |
| 用户手册 PDF | ≥ 60 页（前 30 页 + 后 30 页，每页 ≥ 30 行） |
| 材料清单页数 | 生成 PDF 后**实际计数**再回填申请表第 4 页 |
| 4 份材料一致性 | 全部从同一份 ProjectSpec JSON 渲染 |
| 代码风格 | package/namespace 要贴合业务（如 `com.lanting.defect.xxx`），不能泛通用 |
| 截图 | HTML UI 模板里版权水印 = 当前申请公司名，不能串号 |

---

## 3. 技术栈

| 层 | 选型 | 原因 |
|---|---|---|
| 后端 | FastAPI (Python 3.11+) | async 原生，并发好 |
| 数据库 | SQLAlchemy 2.0 async（开发 SQLite+aiosqlite / 线上 PostgreSQL+asyncpg） | 方言无关，切换只改 DATABASE_URL |
| 任务队列 | asyncio + Semaphore（**不引入 Redis/Celery**） | 简化部署 |
| 鉴权 | JWT (python-jose) + bcrypt 密码哈希 | 标准方案 |
| 账号管理 | Admin Token via header，纯 API（**无管理 UI**） | 简化前端 |
| LLM 客户端 | openai-python SDK（OpenAI 兼容协议） | 可对接 DeepSeek / Qwen / Kimi 等 |
| docx 渲染 | python-docx | 操作原生 docx 模板 |
| PDF 渲染 | ReportLab（源码）+ WeasyPrint（手册） | 一个代码风、一个富文本 |
| 截图 | Playwright Python（Chromium） | 稳定的 headless 浏览器 |
| 前端 | 单页 HTML + Alpine.js + Tailwind（CDN） | 零构建 |
| 部署 | systemd + Nginx 反代 + Let's Encrypt | 无 Docker |

---

## 4. 目录结构

```
soft-copyright-system/
├── PROJECT_SPEC.md           # ← 本文档
├── README.md                 # 快速开始（部署/本地跑）
├── requirements.txt
├── .env.example              # 配置样本
├── .gitignore
├── run.sh                    # 开发启动脚本
│
├── app/
│   ├── __init__.py
│   ├── main.py               # FastAPI 入口
│   ├── config.py             # 从 .env 读配置
│   ├── db.py                 # SQLAlchemy 引擎 + session
│   ├── models.py             # User / Job / JobFile
│   ├── schemas.py            # Pydantic 入出参
│   ├── auth.py               # JWT + 密码哈希 + 依赖项
│   ├── admin_api.py          # /admin/users/* 账号 CRUD
│   ├── job_api.py            # /jobs/* 任务提交/状态/下载
│   ├── llm.py                # OpenAI 兼容客户端封装
│   ├── spec.py               # ProjectSpec 生成（LLM 调用）
│   ├── region.py             # 信用代码 → 省份/城市
│   ├── worker.py             # 异步任务调度 + Semaphore
│   ├── pipeline.py           # 一份软著的完整生成流水线
│   │
│   ├── renderers/
│   │   ├── __init__.py
│   │   ├── application_form.py   # 申请表.docx 渲染器
│   │   ├── features.py           # 功能特点.docx 渲染器
│   │   ├── source_code.py        # 源代码.pdf 渲染器（含 LLM 分文件生成）
│   │   └── user_manual.py        # 用户手册.pdf 渲染器（拼装截图）
│   │
│   └── screenshot/
│       ├── __init__.py
│       ├── capture.py        # Playwright 自动化
│       └── templates/        # HTML UI 模板（可插入 Spec 字段）
│           ├── base.html
│           ├── login.html
│           ├── home.html
│           └── module_*.html # 各业务模块页
│
├── templates/                # 原始 docx 模板（带占位符版）
│   ├── application_form.docx   # 从 申请表签字.docx 改造
│   └── features.docx           # 从 功能特点.docx 改造
│
├── static/
│   └── index.html            # 单页前端（登录 + 提交任务 + 查询 + 下载）
│
└── data/
    ├── app.db                # SQLite
    └── generated/{job_id}/   # 每次任务的输出
        └── {软件名}/
            ├── 申请表.docx
            ├── 功能特点.docx
            ├── 用户手册.pdf
            └── 源代码.pdf
```

---

## 5. 数据模型

### 5.1 数据库表（users / jobs / job_files）

所有表通过 SQLAlchemy 2.0 async 访问。上层代码通过 `app/user_store.py` 访问 users（保留抽象便于未来替换），jobs 相关直接用 `get_session` 依赖。

**线上环境：PostgreSQL**（DATABASE_URL=`postgresql+asyncpg://...`）
**开发环境：SQLite**（DATABASE_URL=`sqlite+aiosqlite:///./data/app.db`）

### 5.2 表结构

```python
# User
id (int, pk, autoincrement)
username (str, unique, index)
password_hash (str)
is_active (bool)
created_at (datetime)

# Job (一次软著批量任务)
id (uuid, pk)
user_id (fk User)
company_name (str)
uscc (str)              # 统一社会信用代码
established_date (date) # 公司成立日期
quantity (int)
keywords (json, list[str])   # 用户提供的系统关键词，可空
language (str, nullable)     # 用户指定编程语言，可空
status (str)            # pending/running/success/failed/partial
progress (int)          # 0-100
created_at (datetime)
finished_at (datetime, nullable)
error (text, nullable)

# JobFile (每个子软著的产物清单)
id (int, pk)
job_id (fk Job)
software_name (str)
spec (json)             # 完整 ProjectSpec
zip_path (str, nullable)
status (str)            # pending/generating/done/failed
error (text, nullable)
```

### 5.2 ProjectSpec（核心数据结构）

```python
{
  # 基本信息
  "software_name": "兰亭印刷品缺陷智能检测与分析平台",
  "software_abbr": "",                        # 可空
  "version": "V1.0",
  "software_category": "应用软件",
  "tech_category": "人工智能软件",            # 物联网/人工智能/大数据/...
  "completion_date": "2025-11-28",
  "publish_status": "未发表",

  # 环境
  "language": "C++",                          # 主编程语言
  "language_list": ["C++", "HTML", "JavaScript", "SQL"],
  "ide": "Eclipse",
  "web_server": "nginx1.22",
  "database": "MySQL5.7",
  "dev_os": "windows8, macos14+",
  "run_os": "winserver2012+",
  "hardware_dev": {"cpu": "2.5GHz+", "ram": "8G+", "disk": "50G+"},
  "hardware_run": {"cpu": "双核2GHz+", "ram": "4G+", "disk": "200G+"},

  # 内容
  "purpose": "解决...",                       # 开发目的 (1-2 句)
  "industry": "面向印刷制造、质量控制...",    # 面向领域/行业
  "main_description": "该平台是...",          # 软件主要功能整段描述
  "tech_features": "计算机视觉, AI算法, ...", # 技术特点关键词（逗号分隔）
  "functions": [                              # 10 个核心功能
    {"name": "图像采集", "desc": "..."},
    ...
  ],

  # 源代码（Phase 3 回填）
  "source_lines": 0,                          # 真实总行数
  "source_files": [                           # LLM 生成的所有代码文件
    {"path": "src/main/java/com/lanting/App.java", "content": "..."},
    ...
  ],

  # UI 页面（Phase 4 用）
  "ui_pages": [
    {"id": "login", "title": "登录", "fields": [...]},
    {"id": "home", "title": "主页", "menu": [...], "table_cols": [...], "table_rows": [...]},
    {"id": "module_image_capture", "title": "图像采集", "fields": [...], "table_rows": [...]},
    ...
  ],

  # 著作权人
  "owner": {
    "name": "武汉兰亭印务有限公司",
    "uscc": "91420112MA7DX2ME10",
    "type": "企业法人",
    "cert_type": "统一社会信用代码证书",
    "nationality": "中国",
    "province": "湖北",
    "city": "武汉",
    "established_date": "2020-06-15"
  },

  # 页数（Phase 3/4 回填）
  "source_pdf_pages": 0,
  "manual_pdf_pages": 0,
}
```

---

## 6. LLM 使用约定

### 6.1 配置（.env）

```
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=sk-xxx
LLM_MODEL=deepseek-chat
LLM_MAX_CONCURRENCY=3
```

### 6.2 Prompt 分工

- **spec.py 第一阶段**：输入公司名/信用代码/关键词 → 输出 ProjectSpec JSON（不含代码/ui_pages）。要求返回严格 JSON。
- **source_code.py 骨架阶段**：输入 ProjectSpec → 输出文件路径+职责 JSON 列表（10-20 个文件）。
- **source_code.py 填充阶段**：对每个文件，输入路径+职责+全局 ProjectSpec → 输出完整代码（200-400 行）。
- **user_manual.py ui_pages 阶段**：输入 ProjectSpec → 输出 ui_pages JSON（页面字段、表格列、示例数据）。

所有 LLM 调用统一通过 `llm.py` 的 `call_json()` / `call_text()`，自动重试、限并发。

---

## 7. API 设计

### 7.1 鉴权

所有业务 API 要求 `Authorization: Bearer <jwt>`。
账号管理 API 要求 `X-Admin-Token: <env ADMIN_TOKEN>`。

### 7.2 账号管理（Admin Token）

```
POST   /admin/users              新建 {username, password}
GET    /admin/users              列表
PATCH  /admin/users/{id}         改 {password?, is_active?}
DELETE /admin/users/{id}         删除
```

### 7.3 用户接口（JWT）

```
POST  /auth/login                {username, password} → {access_token}
POST  /jobs                      新建任务 {company_name, uscc, established_date, quantity, keywords?, language?}
GET   /jobs                      当前用户任务列表
GET   /jobs/{id}                 任务详情 + 子文件状态
GET   /jobs/{id}/stream          SSE 推送进度
GET   /jobs/{id}/download        下载整批 zip
GET   /jobs/{id}/files/{file_id}/download  下载单份 zip
```

---

## 8. 并发与限流

- 全局 `asyncio.Semaphore(LLM_MAX_CONCURRENCY)` 限制 LLM 并发。
- 全局 `asyncio.Semaphore(BROWSER_MAX)` 限制 Playwright 实例数（默认 2）。
- Job 提交后立即返回，后台 `asyncio.create_task()` 执行 `pipeline.run(job_id)`。
- Pipeline 内部按子软著顺序执行，每子软著内部并行生成 4 份材料。
- 进程重启后通过 `worker.resume_pending()` 扫 status=running 的任务继续。

---

## 9. 申请表默认值（全部同意，写死）

| 字段 | 默认值 |
|---|---|
| 软件作品说明 | 原创 |
| 发表状态 | 未发表 |
| 开发方式 | 单独开发 |
| 权利取得方式 | 原始取得 |
| 权利范围 | 全部 |
| 程序鉴别材料 | 一般交存 |
| 文档鉴别材料 | 一般交存 |
| 申请办理方式 | 由著作权人申请 |
| 证书份数 | 1 份正本，0 份副本 |
| 著作权人类别 | 企业法人 |
| 版本号 | V1.0 |
| 软件分类 | 应用软件 |
| 开发完成日期 | 当前日期往前推 1-6 个月随机 |

**留空区域**（用户手填）：
- 申请表第 3 页"以上划线部分内容请在下方手抄写" → 空
- "申请人（机构）盖章" → 空
- "经办人签名" → 空
- "身份证号码" → 空
- 签字日期 → 打印生成当天日期

---

## 10. 阶段进度

> 进度以 TaskList 为准（`TaskList` 命令查看）。本节仅简述里程碑。

- **Phase 1**（✅ 完成）：FastAPI 骨架 + JWT + 账号 CRUD API + 前端登录页。烟雾测试全绿。
- **Phase 2**（✅ 完成）：`app/llm.py` OpenAI 兼容客户端（Semaphore 限流 + JSON 重试）；`app/region.py` 信用代码→省市（覆盖 300+ 地级市）；`app/spec.py` 主题补齐 + 业务元数据 ProjectSpec 生成。单元测试通过。真实 LLM 调用需配 .env 后跑。
- **Phase 3**（✅ 完成）：3 个渲染器
  - `app/renderers/application_form.py`：基于原 docx（`templates/application_form_template.docx`）按单元格坐标写入，勾选框用 ☑/☐ 文本替换
  - `app/renderers/features.py`：功能特点 docx 渲染（两表结构）
  - `app/renderers/source_code.py`：两阶段 LLM 生成（骨架+文件填充）→ ReportLab 排版（A4、带行号、页眉软件名、页脚公司名）
  - 规范保障：≥ 60 页（55 行/页），不够时自动补充常量文件
  - 中文字体自动探测（macOS/Linux 路径）
  - 离线烟雾测试通过（67 页 PDF 中英文渲染正常、docx 所有字段精准定位）
- **Phase 4**（✅ 完成）：
  - HTML UI 模板：`app/screenshot/templates/` 下 `base.css / login.html / home.html / module.html`（后台管理系统风格，主题色按软件名稳定 hash 出 6 种商务配色）
  - Playwright 截图引擎：`app/screenshot/capture.py`，全局单例 Browser + Semaphore 限流 + `capture_html` 截图 + `html_to_pdf` 直接打印 PDF（替代 WeasyPrint 避免 GLib/Pango 原生依赖）
  - 手册渲染：`app/renderers/user_manual.py`
    - 一次 LLM 调用产出 UI 数据（登录页文案 / 主页指标+表格 / 10 模块详情 / FAQ / 字典）
    - 并行渲染 12 张截图，base64 data URI 嵌入手册 HTML
    - 手册模板 `templates/user_manual.html`（封面、目录、8 章、每模块强制新起一页）
    - 页眉 `软件名+版本+页码`，页脚 `【公司名】`（Playwright headerTemplate/footerTemplate）
    - 规范保障：首次渲染后若 < 60 页，循环扩充字典（最多 6 轮），63+ 页达标测试通过
  - 依赖移除：WeasyPrint 被替换为 Playwright 内置 PDF
- **Phase 9**（✅ 完成）：历史记录分页 + 软删除
  - `Job.is_deleted` 字段（bool，默认 false）+ 在线迁移
  - `GET /jobs` 支持 q（模糊）、group(all/active/history)、status、page、page_size 返回 `{items,total,page,page_size}`
  - `DELETE /jobs/{id}` 软删（is_deleted=True）；运行中/排队中的任务不允许删
  - GET 单个任务 + SSE 流忽略已删除
  - 前端拆两节：**正在进行**（activeJobs 轮询刷新）+ **历史记录**（分页/搜索/状态筛选）
  - 每条历史记录右上角红色"删除"按钮 + 警告弹窗（列出软删语义 + 磁盘文件仍保留）
  - 统计卡重设为：进行中 / 排队等待 / 历史记录总数（当前筛选） / 预计剩余耗时

- **Phase 8**（✅ 完成）：串行队列 Worker
  - `app/worker.py`：重写为 `asyncio.Queue` + 单消费者协程，FIFO 严格串行（同一时刻只有 1 个 job running）
  - `submit(job_id)` 和 `submit_retry(job_id, file_id)` 都是入队操作
  - `resume_pending()` 清理旧 JobFile + 按 `created_at` 顺序重入队
  - `main.py` lifespan 里 `start_worker()` 启动消费者
  - 前端展示"队列第 X 位"徽章（pending 任务显示它前面的 running + 更早的 pending 数量）
  - E2E 测试：3 任务串行跑通 + 新任务 + 重试混合 FIFO 正确

- **Phase 7**（✅ 完成）：
  - **7a 模板扩展**：新增 4 个 HTML 模板（`detail.html` / `form.html` / `approval.html` / `chart.html`，chart 用 Chart.js CDN 渲染真实折线/柱/环/堆积面积图）；加 `Job.template` 字段（basic/rich）、`JobFile.progress` 字段；`app/db.py` 在线迁移（SQLite/PG 兼容 ALTER TABLE）；`_UI_RICH_PROMPT` 让 LLM 为每模块产出 1-4 个子页；`_normalize_rich_subpages` 控制总截图数 ∈ [18, 60]
  - **7b Pipeline 进度细化 + 重试**：`_ProgressTracker` 同时更新 Job.progress 和每个 JobFile.progress（节流 1.5s / 2%）；新增 `pipeline.retry_file(job_id, file_id)` 重做单份 + 重建 all.zip；`worker.submit_retry(job_id, file_id)` 后台调度；`POST /jobs/{job_id}/files/{file_id}/retry` API 端点
  - **7c 前端**：创建任务表单加"手册模板"下拉（basic/rich）；详情面板每份软著独立进度条；失败时"继续"按钮调 retry API；轮询时若面板展开则同步刷新子任务详情
- **Phase 5**（✅ 完成）：
  - `app/pipeline.py`：完整生成流水线（generate_specs → 并行 source+manual → 依赖 spec 回填后再出 app_form+features → 打 zip）
  - `app/worker.py`：asyncio.create_task 调度 + 启动时 resume_pending
  - `app/job_api.py`：新增 POST /jobs 自动提交 worker、GET /jobs/{id}/stream SSE、GET /jobs/{id}/download 批量 zip、GET /jobs/{id}/files/{id}/download 单份 zip
  - `static/index.html`：展开详情、进度条、单份/批量下载按钮、2s 轮询刷新
  - E2E mock 测试：14.6 秒跑完 2 份软著，zip 结构正确（all.zip → {软件名}.zip → {软件名}/{4 份材料}）
  - SSE 端到端验证：实时推送 running/failed/success 状态变化
- **Phase 6**：Linux 部署（systemd + Nginx + HTTPS）

---

## 11. 接手指南（新会话继续开发）

1. `TaskList` 看当前进度
2. 读本文档（PROJECT_SPEC.md）
3. 读 `app/` 下已有代码
4. 从 in_progress 任务继续；如果没有则领取第一个 pending 任务
5. 每个 Phase 完成后：更新本文档第 10 节 + 更新 TaskList
