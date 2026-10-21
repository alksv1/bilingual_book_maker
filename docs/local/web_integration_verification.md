# Web 模块集成验证（Issue #13）

## 执行契约

- 网页保存的 Provider Key 存储在数据目录的 `config/keys.json`，Provider 定义中的 `env_key` 指向专属变量。runner 在每次创建翻译子进程时重新读取已保存凭据，仅注入子进程环境，不修改服务器的 `os.environ`，也不通过命令参数传 Key。
- 对同一个 `env_key`，**非空手动环境变量优先于网页保存的 Key**；空白环境值可由有效的已保存 Key 补全。设置页密钥状态与连接测试使用相同优先级。无关的通用 `OPENAI_API_KEY` 不覆盖网页 Provider 的专属变量。
- 子进程工作目录为 `CONFIG_DIR`，CLI 在此发现 `bbm_providers.json`；更新凭据无需重启服务器。
- 自定义提示词是 `user` 模板文本，不是服务器文件路径。非空模板必须包含 `{text}`，另外可用 `{language}`、`{crlf}`。字面花括号写作 `{{` 和 `}}`。提交无效模板时 API 返回可读的 HTTP 422，不创建上传文件或任务；留空使用 CLI 默认提示词。

## 离线回归

在独立 Python 环境安装核心与 Web 依赖：

```bash
python -m pip install -r requirements.txt -r requirements-web.txt
python -m pip install pytest pytest-asyncio httpx
# Windows 中文系统建议先设置 PYTHONUTF8=1，避免测试读取 UTF-8 文件时使用 GBK。
python -m pytest -q tests/test_server_scaffold.py tests/test_auth.py tests/test_settings.py tests/test_tasks_runner.py tests/test_web_integration.py tests/test_frontend_contract.py
```

CI 的 `web-integration` 作业显式安装这些依赖，并清空继承的真实 Provider secrets。测试只用假密钥，不调用真实翻译服务。

本次本地结果：Web 六个模块合计 **126 passed**；共享提示词回归（`test_prompt_file.py`、`test_prompt_sections.py`）**98 passed**；合并运行 **224 passed**（仅有现有 `fitz` 弃用警告）。Windows 使用 `PYTHONUTF8=1`。GitHub Actions 作业已配置，但尚未推送触发远端运行。

额外 CLI 回归（上述两个提示词模块 + `test_cli.py`）在 UTF-8 下为 **251 passed, 1 failed**：`test_an_unknown_provider_names_both_config_files` 硬编码断言 `.bbm/providers.json`，而 Windows 错误消息由 `Path` 输出 `.bbm\\providers.json`。对应测试和 `provider_loader.py` 均未在本次修改；这属于现有跨平台路径断言问题，不计为本次全部测试通过。

全仓库默认离线套件最终结果：**3080 passed, 29 failed, 397 skipped, 51 deselected, 3 xfailed**（Windows、UTF-8，约 19 分钟）。失败分布为：2 项 unknown-provider 路径分隔符断言、1 项 docling 图片路径改写、26 项 `test_prompt_capture_server.py` 本地捕获端点请求收到 HTTP 502。路径相关文件未被本次修改；捕获端点 502 的根因尚未定位，不能声称全部失败均与本次无关。完整本地日志保留在忽略目录 `.venv/full-test-utf8-results.txt`。跳过及 deselect 项未计为通过。

覆盖的模块边界：

1. settings API 保存凭据、模型和默认语言 → runner → 真实受控子进程读取环境；包括持久化重载、空值与同名环境变量优先级。
2. 保护模式下任务与配置各路由、SSE、文件下载的匿名/错误凭据拒绝，以及 Bearer/登录 Cookie 成功访问。
3. 静态前端 API 路径、方法、multipart 字段与 SSE 查询参数，与 FastAPI 实际注册的契约对照。
4. 真实上传 → runner → 实际 CLI（只替换为离线 translator，阻止网络）→ 双语 TXT → 终态 SSE → 下载和删除。
5. 受控 PDF 子进程 → 默认 EPUB 产物移动、下载文件名和 MIME；不冒充实际 PDF/OCR 引擎测试。

## 真实运行 smoke（2026-10-21）

使用临时 `BBM_DATA_DIR` 启动本地 uvicorn。通过浏览器配置中心保存测试端点、密钥和端点提供的 `deepseek-v4.1-flash` 模型，未手动导出 Provider Key。

上传一句自造英文 TXT，并提交包含 `{text}`、`{language}` 的自定义提示词：

- 任务进入 `completed`；没有 `No API key` 错误。
- 下载 HTTP 200，文件名 `smoke_bilingual.txt`，类型 `text/plain`。
- 下载内容同时包含原文 `The small bird sings in the morning.` 和译文 `小鸟在清晨歌唱。`。
- 终态 SSE 接口 HTTP 200。

真实 smoke 会消耗接口额度，不纳入 CI。它证明所测端点的 Web 配置到真实翻译闭环，不代表其他 Provider、全部格式或 PDF 引擎已实测。

## 已知限制与独立后续项

- Web 不传 `--pdf_layout`，CLI 默认 `none`。legacy PDF loader 的默认主产物名为 `{stem}_bilingual.epub`，与 runner 一致；非默认 PDF 布局未通过网页开放。
- legacy PDF loader 可能只写出 TXT 后正常退出。Web 仍要求 EPUB，故 TXT-only 回退会报告任务失败，而不是提供一个不存在的 EPUB。不在本次新增产物扫描或切换 `--to-epub` pipeline。
- 真实 TXT smoke 完成后 UI 显示 100%，但段落计数仍为 `0 / 0`：真实 TXT 输出没有命中 runner 当前的进度计数协议。受控子进程的进度解析有测试，不等于所有真实 loader 都会输出可解析计数。
- runner 的数据库 general-settings 回退读取 `general`，settings 服务写入 `general_settings`；正常 JSON 配置路径可用，但数据库-only fallback 契约需另立 Issue 对齐。

上述后续项应独立跟踪，不扩大本次凭据及提示词契约修复。尚未自动发布 GitHub Issue 或评论。
