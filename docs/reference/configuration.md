# Configuration reference

There are three configuration layers: deployment defaults, the search form, and the immutable settings saved with a run. Editing today's form does not change an existing run.

## Community environment variables

The repository's [.env.example](https://github.com/paperseek/paperseek/blob/main/.env.example) is the deployment template.

| Variable | Use |
| --- | --- |
| `PAPERSEEK_DATABASE` | Local SQLite path; defaults to `~/.paperseek/paperseek.db` |
| `PAPERSEEK_LLM_API_TYPE` | `openai_responses`, `openai_chat`, or `anthropic_messages` |
| `PAPERSEEK_LLM_BASE_URL` | Provider API base URL |
| `PAPERSEEK_LLM_MODEL` | A model ID supported by that provider |
| `PAPERSEEK_LLM_API_KEY` | Model credential |
| `PAPERSEEK_OPENALEX_API_KEY` | OpenAlex key or key pool |
| `PAPERSEEK_SEMANTIC_SCHOLAR_API_KEY` | Optional Semantic Scholar key |
| `PAPERSEEK_PUBMED_API_KEY` | Optional NCBI key |
| `PAPERSEEK_WOS_API_KEY` | Web of Science Starter key |
| `PAPERSEEK_WOS_DB` | WoS collection, for example `WOS` |
| `PAPERSEEK_GOOGLE_SCHOLAR_API_KEY` | Serper key or key pool |
| `PAPERSEEK_WANFANG_API_KEY` | Wanfang Data AppKey |
| `PAPERSEEK_CONTACT_EMAIL` | Contact email used by source clients that support it |
| `PAPERSEEK_PROXY_URLS` | HTTP(S) proxy pool, one URL per line or comma-separated; replaces the proxy saved in Settings |
| `PAPERSEEK_PROXY_SCOPE` | What goes through the pool: `models`, `ip_limited_sources`, `other_sources`; defaults to all three |
| `PAPERSEEK_MAX_CONCURRENT_RUNS` | Searches and reviews that run at the same time; defaults to `4`, `0` removes the limit |

Direct CLI/server execution reads the process environment. Docker Compose loads the optional `.env` file. A copied template alone does not configure a directly launched Python process.

### Example on PowerShell

```powershell
$env:PAPERSEEK_LLM_API_TYPE = "openai_chat"
$env:PAPERSEEK_LLM_BASE_URL = "https://your-provider.example/v1"
$env:PAPERSEEK_LLM_MODEL = "your-model-id"
$env:PAPERSEEK_LLM_API_KEY = "<your-key>"
paperseek serve
```

Replace all placeholders. Prefer your operating system or service manager's private secret configuration for unattended use. The URL above is deliberately not a working provider endpoint.

### OpenAlex and Serper key pools

Separate keys with whitespace, commas, or semicolons in the corresponding variable or key field. PaperSeek rotates the starting key and distinguishes authentication failures from transient failures.

Do not repeat a key to simulate extra capacity. Each provider still enforces its own access policies.

### Network proxy

Use a proxy when a model or data source cannot be reached directly from your network, or when a source limits your network address. Set it in **Settings › General › Network proxy**, or with `PAPERSEEK_PROXY_URLS` for a server deployment. The environment variable takes precedence, and Settings then shows the proxy as read-only. Restart the server and worker after changing it.

```powershell
$env:PAPERSEEK_PROXY_URLS = "http://user:password@proxy-a.example:8080,http://user:password@proxy-b.example:8080"
$env:PAPERSEEK_PROXY_SCOPE = "ip_limited_sources"
```

Choose what goes through the proxy:

| Scope | Covers | When it helps |
| --- | --- | --- |
| `models` | Language, embedding, rerank and decision models | The model provider cannot be reached directly. Local and LAN addresses always connect directly. |
| `ip_limited_sources` | arXiv, Crossref, PaperHub, NSSD, Chinese Medical Journals, and PubMed or OpenAlex without a key | These sources count requests per network address. A different exit helps when your address is shared with others or has been throttled. |
| `other_sources` | Semantic Scholar, Web of Science, Google Scholar, Wanfang Data, and sources with a key | These sources count usage per key or in a shared pool, so a proxy does not raise the limit. It only helps when they cannot be reached directly. |

Traffic outside the chosen scope follows the system proxy settings (`HTTPS_PROXY`, `NO_PROXY`). With several proxies, requests take them in turn, including retries. Request intervals, cooldowns and `Retry-After` still apply across the whole pool, so a pool never sends faster than a single address would.

HTTP and HTTPS proxy addresses are supported, and an `http://` proxy can tunnel HTTPS requests. Percent-encode reserved characters in user names and passwords; see [HTTPX proxy configuration](https://www.python-httpx.org/advanced/proxies/). A configured proxy that does not work never falls back to a direct connection, and HTTP 407 means proxy authentication failed. Connection tests in Settings use the same proxy.

Addresses saved in Settings are encrypted on this machine and are never shown again; Settings lists only each proxy's host and port. **Clear saved keys** in Settings › User also removes them. Never commit real proxy credentials.

### Concurrent tasks

`PAPERSEEK_MAX_CONCURRENT_RUNS` sets how many searches and reviews run at the same time; the rest wait in the queue. The default is `4`, and `0` removes the limit. Data sources and model providers enforce their own limits whatever this value is, and all running tasks share them, so a higher value mostly makes each task wait longer.

## Per-search settings

The form contains the research question, source, fields, budget, model connection, preliminary ranking, and source-specific options. See [search](../guide/search.md), [data sources](../guide/sources.md), and [models](../guide/models.md).

Protocol and base URL must agree. A server that implements Chat Completions does not necessarily implement Responses or Anthropic Messages. The usual OpenAI-compatible base URL ends in `/v1`; do not append `/chat/completions` when the field asks for a base URL.

## Optional ranking services

Local fusion needs no external preliminary-ranking endpoint. Embedding + Rerank and System One require their corresponding connections when used in Community or with your own key online. Their keys and model IDs can differ from the main language model.

## Online configuration

PaperSeek Service provides model lists and server-managed connections. With your own key, public provider connections are allowed after server-side address validation. Deployment secrets, internal routes, and allowlists are operator settings, not end-user fields.

See the [online guide](../online/index.md).
