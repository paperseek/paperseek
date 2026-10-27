# MCP

在支持 Model Context Protocol 的助手中调用 PaperSeek。本地 MCP 服务将社区版应用能力提供为结构化工具。

## 安装匹配版本

```sh
python -m pip install "paperseek-mcp==1.0.0b4"
```

独立适配器依赖相同版本的 `paperseek`，复用社区版应用服务，不维护另一套检索引擎。

## 使用 stdio

客户端配置示例：

```json
{
  "mcpServers": {
    "paperseek": {
      "command": "paperseek-mcp",
      "args": ["--transport", "stdio"]
    }
  }
}
```

确保客户端能够找到已安装的命令。通过客户端的环境或密钥配置提供模型与数据源凭据，不要公开包含真实 Key 的配置文件。

MCP 进程与 CLI 使用相同的 `PAPERSEEK_*` 环境变量。只在 Web 表单中填写的配置不会自动传给这个独立进程，它也不会自动加载 `.env`，详见[配置参考](../reference/configuration.md)。连接前可在相同环境运行 `paperseek doctor --json`，不需要启动 Web 服务或登录 PaperSeek 账户。

## 可用工具

| 工具 | 用途 |
| --- | --- |
| `list_sources` / `get_source_capabilities` | 查看数据源及其能力 |
| `start_search` | 开始持久化任务，立即返回任务 ID |
| `get_search_status` / `get_search_events` | 查看状态与事件 |
| `get_search_results` | 分页读取结果 |
| `get_citation_graph` | 读取引用图谱 |
| `cancel_search` | 请求取消 |
| `export_results` | 导出完整结果，格式为 JSON、CSV、BibTeX 或 RIS |

连接会话不等于检索任务的生命周期。保存返回的 `run_id`，用它继续检查状态与读取结果；结果分页时跟随返回的游标，不要把第一页当成全部文献。

## 一次检索的调用流程

1. 用 `list_sources` 和 `get_source_capabilities` 获取数据源与筛选项 ID。
2. 调用 `start_search`，提供问题、数据源和预算（`fast`、`medium`、`high` 或 `max`），立即获得 `run_id` 和 `status`。重试提交时，复用非空的 `idempotency_key` 和相同参数，避免重复创建任务。
3. 每隔几秒调用 `get_search_status`。`completed`、`completed_with_warnings`、`failed`、`cancelled` 表示任务已结束；事件页为空不代表完成。
4. 用 `get_search_results` 读取结果，直到 `next_cursor` 为 `null`。事件和结果每页可取 1–200 项；完整的 JSON、CSV、BibTeX 或 RIS 使用 `export_results` 导出。

检索期间应保持 MCP 进程运行，其内置 worker 才能推进任务。断开连接不会主动取消检索，需要停止时调用 `cancel_search`。任务保存在本地数据库中，重启后使用同一数据库即可继续读取。默认路径为 `~/.paperseek/paperseek.db`，可通过 `--database` 或 `PAPERSEEK_DATABASE` 修改。

工具同时返回结构化 JSON 和兼容的文本表示。参数或执行错误通过 MCP 的 `isError` 返回，未知工具名称作为协议错误返回。读取类工具标记为只读，开始与取消检索声明其副作用；检索可能产生所配置服务商的 API 费用。

## 本地 HTTP

```sh
paperseek-mcp --transport streamable-http --host 127.0.0.1 --port 8766 --path /mcp
```

旧客户端仍可使用 `--transport sse-legacy`。新配置优先选择 stdio 或 Streamable HTTP。

非回环 HTTP 必须配置 `PAPERSEEK_MCP_SERVICE_TOKEN`、HTTPS 的 `PAPERSEEK_MCP_PUBLIC_URL`，以及明确的 `PAPERSEEK_MCP_ALLOWED_HOSTS` 和 `PAPERSEEK_MCP_ALLOWED_ORIGINS`。TLS 由反向代理终止，不要将未认证的本地服务直接开放到公网。

此 HTTP 入口用于私有社区版工作区。配置服务令牌后，客户端每个请求均须携带 `Authorization: Bearer <token>`。它不提供 OAuth 登录流程；只支持自动 OAuth 发现的客户端请改用 stdio。同一实例的客户端共享数据库和配置的模型凭据，因此它不是面向公众、按用户隔离的 BYOK 服务。

## 协议与排查

适配器使用[官方 Python SDK](https://github.com/modelcontextprotocol/python-sdk)处理 MCP 服务发现、传输和结构化工具消息。当前提供 tools；resources 和 prompts 是可选能力，这条检索流程不需要为了形式完整而增加它们。旧 SSE 仅用于兼容。

客户端无法启动时，先尝试填写命令的绝对路径并检查环境变量；HTTP 连接失败时，检查令牌和 Host/Origin 白名单。诊断日志输出到 stderr，stdio 的 stdout 专用于 MCP 消息。内部异常只向客户端返回简短失败提示，具体原因查看服务端日志。

## Agent Skill

安装、首次使用与配置说明已独立到 [Agent Skill 指南](skill.md)。通过 Skill 使用 CLI 不需要安装 MCP。
