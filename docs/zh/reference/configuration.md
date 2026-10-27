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
| `PAPERSEEK_WANFANG_API_KEY` | 万方数据 AppKey |
| `PAPERSEEK_CONTACT_EMAIL` | 支持联系邮箱的数据源客户端使用的邮箱 |
| `PAPERSEEK_PROXY_URLS` | HTTP(S) 代理池，每行一个或用逗号分隔；设置后取代设置页中保存的代理 |
| `PAPERSEEK_PROXY_SCOPE` | 哪些请求走代理：`models`、`ip_limited_sources`、`other_sources`，默认三项都走 |
| `PAPERSEEK_MAX_CONCURRENT_RUNS` | 同时运行的检索和综述任务数，默认 `4`，`0` 表示不限制 |

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

### 网络代理

当你的网络无法直接访问模型或数据源，或者数据源对你的网络地址限速时，可以使用代理。在 **设置 › 通用 › 网络代理** 中填写；服务器部署也可以使用 `PAPERSEEK_PROXY_URLS`。环境变量优先，此时设置页中的代理只读。修改后需要重启服务端和 worker。

```powershell
$env:PAPERSEEK_PROXY_URLS = "http://user:password@proxy-a.example:8080,http://user:password@proxy-b.example:8080"
$env:PAPERSEEK_PROXY_SCOPE = "ip_limited_sources"
```

选择哪些请求通过代理发送：

| 范围 | 包括 | 什么时候有用 |
| --- | --- | --- |
| `models` | 大模型、嵌入、重排和决策模型 | 无法直接访问模型服务时。本机和局域网地址始终直连。 |
| `ip_limited_sources` | arXiv、Crossref、PaperHub、OpenReview、国家哲学社会科学学术期刊数据库、中华医学期刊库、ShowJCR 分区表下载，以及未填写密钥的 PubMed 和 OpenAlex | 这些数据源按网络地址计算请求次数。网络地址与他人共用或已被限速时，换一个出口会有帮助。 |
| `other_sources` | Semantic Scholar、Web of Science、Google Scholar、万方数据，以及已填写密钥的数据源 | 这些数据源按密钥或共享额度计算，代理不会增加额度，只在无法直接访问时需要。 |

未选中的请求按系统代理设置（`HTTPS_PROXY`、`NO_PROXY`）发送。填写多个代理时，请求（包括重试）会轮流使用它们。请求间隔、冷却时间和 `Retry-After` 对整个代理池统一生效，代理池发送请求的速度不会超过单个地址。

支持 HTTP 和 HTTPS 代理地址，`http://` 代理也能通过隧道访问 HTTPS。用户名和密码中的保留字符需要 URL 编码，参见 [HTTPX 代理配置](https://www.python-httpx.org/advanced/proxies/)。配置的代理不可用时不会退回直连，HTTP 407 表示代理鉴权失败。设置页中的连接测试也使用同一个代理。

在设置页保存的地址会在本机加密存储，之后不再显示，设置页只列出每个代理的主机和端口。**设置 › 用户** 中的 **清空已保存的密钥** 也会删除这些地址。不要提交真实代理凭据。

### 并发任务

`PAPERSEEK_MAX_CONCURRENT_RUNS` 决定同时运行多少个检索和综述任务，其余任务排队等待。默认 `4`，`0` 表示不限制。无论该值多大，数据源和模型服务都有各自的限速，且由所有运行中的任务共享，所以调得更高主要会让每个任务等待更久。

## 每次检索的设置

表单包含研究问题、来源、领域、预算、模型连接、初排序与来源专属选项。详见[检索](../guide/search.md)、[数据源](../guide/sources.md)和[模型](../guide/models.md)。

协议必须与地址匹配。实现 Chat Completions 的服务不一定支持 Responses 或 Anthropic Messages。OpenAI 兼容服务的基础地址通常以 `/v1` 结尾；基础地址输入框不要再拼接 `/chat/completions`。

## 可选排序服务

本地融合不需要外部初排序接口。在社区版或在线版自带密钥模式中使用 Embedding + Rerank、System One 时，需要填写相应连接。它们的 Key 与模型 ID 可以和主语言模型不同。

## 在线版配置

PaperSeek 服务提供模型列表，连接由站点管理；自带密钥模式可配置通过服务器地址校验的公共接口。部署密钥、内部路由和允许列表属于运维设置，不是用户表单配置。

详见[在线版指南](../online/index.md)。
