# 配置参考

配置分为部署默认值、当前检索表单和任务保存的不可变配置。修改今天的表单，不会改变已有任务。

## 社区版环境变量

部署模板见仓库的 [.env.example](https://github.com/paperseek/paperseek/blob/main/.env.example)。

| 变量 | 用途 |
| --- | --- |
| `PAPERSEEK_DATABASE` | SQLite 路径，默认 `~/.paperseek/paperseek.db` |
| `PAPERSEEK_LLM_API_TYPE` | `openai_responses`、`openai_chat` 或 `anthropic_messages` |
| `PAPERSEEK_LLM_BASE_URL` | 模型提供方 API 基础地址 |
| `PAPERSEEK_LLM_MODEL` | 对应提供方支持的模型 ID |
| `PAPERSEEK_LLM_API_KEY` | 模型凭据 |
| `PAPERSEEK_OPENALEX_API_KEY` | OpenAlex Key 或 Key 池 |
| `PAPERSEEK_SEMANTIC_SCHOLAR_API_KEY` | 可选的 Semantic Scholar Key |
| `PAPERSEEK_PUBMED_API_KEY` | 可选的 NCBI Key |
| `PAPERSEEK_WOS_API_KEY` | Web of Science Starter Key |
| `PAPERSEEK_WOS_DB` | WoS 集合，例如 `WOS` |
| `PAPERSEEK_GOOGLE_SCHOLAR_API_KEY` | Serper Key 或 Key 池 |
| `PAPERSEEK_CONTACT_EMAIL` | 支持联系邮箱的数据源客户端使用的邮箱 |
| `PAPERSEEK_SOURCE_PROXIES` | 仅部署端使用的 JSON：数据源 ID 映射到 HTTP(S) 代理地址或地址列表 |

直接运行 CLI 或服务时读取进程环境。Docker Compose 会加载可选 `.env`。仅复制模板不会配置直接启动的 Python 进程。

### PowerShell 示例

```powershell
$env:PAPERSEEK_LLM_API_TYPE = "openai_chat"
$env:PAPERSEEK_LLM_BASE_URL = "https://your-provider.example/v1"
$env:PAPERSEEK_LLM_MODEL = "your-model-id"
$env:PAPERSEEK_LLM_API_KEY = "<your-key>"
paperseek serve
```

请替换全部占位符。长期运行时优先使用操作系统或服务管理器的私有配置。上面的地址是示例，并非真实可调用的服务。

### OpenAlex 与 Serper 多 Key

在对应环境变量或 Key 输入框中，用空白、逗号或分号分隔多个 Key。PaperSeek 会轮换请求的起始 Key，并区分鉴权失败与临时失败。

重复填写同一个 Key 不会增加可用容量；提供方自身的访问政策仍然适用。

### 数据源代理与 IP 池

在 API/服务端和 worker 的环境中设置 `PAPERSEEK_SOURCE_PROXIES`，然后重启相应进程。PowerShell 示例：

```powershell
$env:PAPERSEEK_SOURCE_PROXIES = '{"pubmed":["http://user:password@proxy-a.example:8080","http://user:password@proxy-b.example:8080"],"arxiv":"http://proxy.example:8080"}'
```

可用的数据源 ID 为 `openalex`、`arxiv`、`crossref`、`semanticscholar`、`pubmed`、`googlescholar`、`paperhub`、`wos`。未指定的数据源使用直连；也可填写由代理商管理 IP 轮换的单个网关地址。支持 HTTP 和 HTTPS 代理地址，`http://` 代理也能通过隧道访问 HTTPS 数据源。用户名和密码中的保留字符需要 URL 编码，参见 [HTTPX 代理配置](https://www.python-httpx.org/advanced/proxies/)。

每个客户端随机选择起始代理，随后按请求轮换，已有的有限重试也使用该池。请求间隔、数据源与凭据共享的冷却时间和 `Retry-After` 仍然生效；代理池不会增加 API Key 的配额。配置的代理不可用时不会静默退回直连，HTTP 407 表示代理鉴权失败。模型请求使用独立连接，数据源连接测试则使用同一份代理配置。

将此配置保存在私有环境或密钥存储中，不通过浏览器设置，也不会写入任务配置。不要提交真实代理凭据。移除变量、设为空字符串或 `{}` 即可禁用显式数据源代理。

## 每次检索的设置

表单包含研究问题、来源、领域、预算、模型连接、初排序与来源专属选项。详见[检索](../guide/search.md)、[数据源](../guide/sources.md)和[模型](../guide/models.md)。

协议必须与地址匹配。实现 Chat Completions 的服务不一定支持 Responses 或 Anthropic Messages。OpenAI 兼容服务的基础地址通常以 `/v1` 结尾；基础地址输入框不要再拼接 `/chat/completions`。

## 可选排序服务

本地融合不需要外部初排序接口。在社区版或在线版自带密钥模式中使用 Embedding + Rerank、System One 时，需要填写相应连接。它们的 Key 与模型 ID 可以和主语言模型不同。

## 在线版配置

PaperSeek 服务提供模型列表，连接由站点管理；自带密钥模式可配置通过服务器地址校验的公共接口。部署密钥、内部路由和允许列表属于运维设置，不是用户表单配置。

详见[在线版指南](../online/index.md)。
