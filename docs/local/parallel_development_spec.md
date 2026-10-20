# Bilingual Book Maker - 并行开发规范与接口契约文档 (Parallel Dev Spec)

> **版本**：v1.0  
> **适用阶段**：Stage 2 并行开发阶段（Issue 2、Issue 3、Issue 4、Issue 5）  
> **核心原则**：文件边界严格隔离、契约优先、对 `book_maker/` 原核心代码零侵入。

---

## 目录
1. [代码边界与文件所有权划分](#1-代码边界与文件所有权划分)
2. [统一数据模型与状态机规范](#2-统一数据模型与状态机规范)
3. [REST & SSE API 接口详细契约](#3-rest--sse-api-接口详细契约)
4. [双模式鉴权与安全协议约定](#4-双模式鉴权与安全协议约定)
5. [配置中心与持久化存储映射规范](#5-配置中心与持久化存储映射规范)
6. [任务执行器与 CLI 子进程调度规约](#6-任务执行器与-cli-子进程调度规约)

---

## 1. 代码边界与文件所有权划分

为确保并行开发期间 **0 Git 冲突**，所有文件建立严格归属权，禁止跨边界修改他人负责的核心逻辑文件：

| 模块 / Issue | 负责文件路径 | 禁止触碰 |
| :--- | :--- | :--- |
| **Issue 1（前置脚手架）** | `server/main.py`<br>`server/config.py`<br>`server/database.py`<br>`server/models.py`<br>`server/schemas.py`<br>`requirements-web.txt` | 奠基阶段合入后冻结接口签名 |
| **Issue 2（任务调度与引擎）** | `server/services/runner.py`<br>`server/routers/tasks.py` | 不得修改 `settings.py` 或 `auth.py` |
| **Issue 3（双模式鉴权）** | `server/middleware/auth.py`<br>`server/routers/auth.py` | 不得侵入任务执行与设置业务逻辑 |
| **Issue 4（配置中心）** | `server/services/settings.py`<br>`server/routers/settings.py` | 不得直接调用任务执行器 |
| **Issue 5（Web 前端界面）** | `server/static/**`（含 `index.html`, `js/`, `css/` 等） | 纯静态资产，仅消费 API，不包含 Python 代码 |

---

## 2. 统一数据模型与状态机规范

### 2.1 数据库结构 (SQLite: `data/bbm_tasks.db`)

#### 表 1：`tasks`（书籍翻译任务表）
```sql
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,                       -- 任务UUID (如 32位或 nanoid)
    filename TEXT NOT NULL,                   -- 上传原始文件名 (如 "book.epub")
    file_path TEXT NOT NULL,                  -- 上传文件存储路径 (data/uploads/{id}.epub)
    output_path TEXT,                         -- 翻译完成产物路径 (data/output/{id}_bilingual.epub)
    file_type TEXT NOT NULL,                  -- 文件格式: "epub", "pdf", "txt", "srt"
    status TEXT NOT NULL DEFAULT 'pending',   -- 状态: pending, running, completed, failed, cancelled
    progress_current INTEGER DEFAULT 0,       -- 当前已翻译段落数
    progress_total INTEGER DEFAULT 0,         -- 总需翻译段落数
    model_provider TEXT,                      -- 使用的翻译服务商 (如 "openai", "gemini")
    target_language TEXT DEFAULT 'zh-hans',   -- 目标翻译语言
    prompt_template TEXT,                     -- 自定义提示词模板（可为空）
    error_message TEXT,                       -- 失败原因摘要
    log_path TEXT NOT NULL,                   -- 任务日志落盘文件路径 (data/logs/{id}.log)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

#### 表 2：`app_settings`（系统全局选项表，键值型）
```sql
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,                     -- 配置键名 (如 "default_target_lang", "concurrency")
    value TEXT NOT NULL,                      -- JSON序列化配置值
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### 2.2 任务状态机生命周期 (Task State Machine)
```
       ┌───────────┐
       │  PENDING  │ ──(调度器拾取)──> ┌───────────┐
       └─────┬─────┘                   │  RUNNING  │
             │                         └─────┬─────┘
         (用户取消)                ┌─────────┼─────────┐
             │                     ▼         ▼         ▼
             │                ┌─────────┐┌────────┐┌───────────┐
             └───────────────>│CANCELLED││ FAILED ││ COMPLETED │
                              └─────────┘└────────┘└───────────┘
```

---

## 3. REST & SSE API 接口详细契约

所有 API 前缀统一为 `/api`。

### 3.1 统一返回与错误格式
成功请求返回 HTTP 200/201；业务异常遵循标准 JSON 格式：
```json
{
  "detail": "错误描述信息",
  "code": "SPECIFIC_ERROR_CODE"
}
```

---

### 3.2 鉴权模块 (Auth API) - Issue 3 承接，Issue 5 调用

#### 1. 查询系统鉴权状态
- **`GET /api/auth/status`**
- **请求头**：无需或带 Cookie
- **响应体**：
  ```json
  {
    "protected_mode": true,      // true: 配置了AUTH_PASSWORD, false: 开放免密模式
    "authenticated": true        // 当前请求是否已登录
  }
  ```

#### 2. 用户登录
- **`POST /api/auth/login`**
- **请求体**：
  ```json
  {
    "password": "user_input_password"
  }
  ```
- **响应**：
  - 成功：`200 OK`，并在响应头写入 `Set-Cookie: bbm_session=<token>; HttpOnly; SameSite=Lax; Path=/`。
  - 密码错误：`401 Unauthorized`，`{"detail": "密码错误"}`。

#### 3. 用户登出
- **`POST /api/auth/logout`**
- **响应**：`200 OK`，清空 Cookie。

---

### 3.3 任务调度模块 (Tasks API) - Issue 2 承接，Issue 5 调用

#### 1. 批量创建/上传任务
- **`POST /api/tasks`**
- **Content-Type**: `multipart/form-data`
- **请求参数**：
  - `files`: File[] (支持多选上传)
  - `target_lang`: string (可选，默认跟随全局配置)
  - `provider`: string (可选，覆盖当前使用的 Provider)
  - `prompt`: string (可选，本次翻译额外提示词)
- **响应体 (`201 Created`)**：
  ```json
  [
    {
      "id": "task_20261020_001",
      "filename": "atomic_habits.epub",
      "status": "pending",
      "created_at": "2026-10-20T10:00:00Z"
    }
  ]
  ```

#### 2. 获取任务列表
- **`GET /api/tasks?status=all&limit=50&offset=0`**
- **响应体**：
  ```json
  {
    "total": 1,
    "items": [
      {
        "id": "task_20261020_001",
        "filename": "atomic_habits.epub",
        "file_type": "epub",
        "status": "running",
        "progress_current": 142,
        "progress_total": 850,
        "model_provider": "openai",
        "target_language": "zh-hans",
        "error_message": null,
        "created_at": "2026-10-20T10:00:00Z",
        "updated_at": "2026-10-20T10:05:32Z"
      }
    ]
  }
  ```

#### 3. 取消正在运行/排队的任务
- **`POST /api/tasks/{task_id}/cancel`**
- **响应体**：
  ```json
  { "id": "task_20261020_001", "status": "cancelled" }
  ```

#### 4. 删除任务记录（可连带删除物理文件）
- **`DELETE /api/tasks/{task_id}?delete_file=true`**
- **响应体**：`204 No Content`

#### 5. 任务实时日志流 (Server-Sent Events)
- **`GET /api/tasks/{task_id}/logs/stream`**
- **Content-Type**: `text/event-stream`
- **数据帧格式**：
  ```
  data: {"time": "10:05:30", "line": "Translating chunk [142/850]..."}\n\n
  data: {"time": "10:05:32", "line": "Chunk [142/850] completed, tokens: 320"}\n\n
  ```

#### 6. 翻译结果下载
- **`GET /api/tasks/{task_id}/download`**
- **响应**：直接返回双语输出文件流 (`application/octet-stream` 或 `application/epub+zip`)。

---

### 3.4 配置中心模块 (Settings API) - Issue 4 承接，Issue 5 调用

#### 1. 获取全局系统配置
- **`GET /api/settings/general`**
- **响应体**：
  ```json
  {
    "default_target_lang": "zh-hans",
    "bilingual_style": "default",       // "default"(译文下), "reversed"(译文上), "only"(单语)
    "concurrency_limit": 1,             // 同时运行的书籍任务数量 (默认1，防超频)
    "max_retries": 3
  }
  ```

#### 2. 更新全局系统配置
- **`PUT /api/settings/general`**
- **请求体**：同上字段（支持增量提交）。

#### 3. 获取所有 Provider 配置列表（敏感 Key 已脱敏）
- **`GET /api/settings/providers`**
- **响应体**：
  ```json
  {
    "active_provider": "openai",
    "providers": {
      "openai": {
        "api_style": "openai",
        "base_url": "https://api.openai.com/v1",
        "default_models": ["gpt-4o", "gpt-4o-mini"],
        "has_api_key": true,
        "masked_api_key": "sk-proj-****AbCd"
      },
      "gemini": {
        "api_style": "gemini",
        "default_models": ["gemini-1.5-pro", "gemini-1.5-flash"],
        "has_api_key": false,
        "masked_api_key": null
      }
    }
  }
  ```

#### 4. 更新/保存特定 Provider 配置
- **`PUT /api/settings/providers/{provider_name}`**
- **请求体**：
  ```json
  {
    "base_url": "https://api.openai.com/v1",
    "api_key": "sk-proj-actual-real-key-if-modified", // 若不修改则不传或传空
    "default_models": ["gpt-4o", "gpt-4o-mini"],
    "set_as_active": true
  }
  ```
- **行为**：持久化写入 `data/config/bbm_providers.json`。

#### 5. 测试 Provider 连通性 (Ping Test)
- **`POST /api/settings/providers/{provider_name}/test`**
- **请求体**：
  ```json
  {
    "base_url": "https://api.openai.com/v1",
    "api_key": "sk-proj-...",
    "model": "gpt-4o-mini"
  }
  ```
- **响应体**：
  - 成功：`{"success": true, "message": "连接正常，延迟 420ms"}`
  - 失败：`{"success": false, "message": "401 Unauthorized: Invalid API Key"}`

---

## 4. 双模式鉴权与安全协议约定

### 4.1 环境变量规则
- 变量名：`AUTH_PASSWORD`
- 当 `AUTH_PASSWORD` 未定义、空字符串或仅包含空格时：
  - 系统处于 **开放模式** (`protected_mode = false`)。
  - 所有请求跳过鉴权中间件，全局直接通过。
- 当 `AUTH_PASSWORD` 有有效值时：
  - 系统处于 **保护模式** (`protected_mode = true`)。

### 4.2 保护模式校验逻辑（按顺序检测）
1. **豁免白名单**：
   - 静态资源路径：`/static/*`、`/favicon.ico`
   - 鉴权端点：`GET /api/auth/status`、`POST /api/auth/login`
2. **API 令牌校验 (Bearer Token)**：
   - 提取请求头 `Authorization`。
   - 若符合 `Bearer <AUTH_PASSWORD>`，则验证通过（供外部脚本、Calibre 自动化调用）。
3. **网页 Cookie 校验**：
   - 提取 Cookie `bbm_session`。
   - 若包含由服务端签发的有效加密 Session（包含时间戳且未过期），则验证通过。
4. **拒绝响应**：
   - 任何未通过校验的 API 请求，统一返回 HTTP `401 Unauthorized`。

---

## 5. 配置中心与持久化存储映射规范

### 5.1 目录划分 (`data/`)
服务运行时统一将数据持久化在 `data/` 目录中：
```
data/
├── bbm_tasks.db              # SQLite 数据库
├── config/
│   ├── bbm_providers.json    # 与现存 bbm_providers.json 格式完全兼容的配置文件
│   └── general.json          # 网页端通用参数
├── uploads/                  # 待处理书籍暂存目录
├── output/                   # 翻译完成产物目录
└── logs/                     # 各任务独立执行日志 (如 {task_id}.log)
```

### 5.2 兼容性映射
`server/services/settings.py` 在保存 Provider 时：
- 直接写入 `data/config/bbm_providers.json`。
- 如果用户通过界面配置了新的 API Key，系统会自动将该 Key 注入到对应的环境映射或者写入 `bbm_providers.json` 的相应字段中，完全遵照 `book_maker/provider_loader.py` 的读取标准。

---

## 6. 任务执行器与 CLI 子进程调度规约

### 6.1 子进程调用规则
Task Runner （Issue 2）调用 `book_maker` 时，统一采用 Python 异步子进程 `asyncio.create_subprocess_exec`，并传入命令行参数：
```bash
python -m book_maker \
  --book_name /app/data/uploads/{task_id}.epub \
  --output_path /app/data/output/{task_id}_bilingual.epub \
  --language {target_language} \
  --model_provider {provider} \
  --custom_provider_path /app/data/config/bbm_providers.json
```

### 6.2 实时段落进度捕获规约
`runner.py` 监听子进程的 `stdout` 行输出，并写入 `data/logs/{task_id}.log`：
- 使用正则匹配原生输出中的进度信息（如 `[N/M]` 或 `Progress:` 标记）。
- 每次捕获到进度递增时，原子性更新 SQLite 中该任务的 `progress_current` 与 `progress_total`。
- 子进程正常退出码为 `0` 时，标记任务为 `completed`；非零退出时，截取 stderr 尾部 5 行记录为 `error_message` 并标记为 `failed`。
