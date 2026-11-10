# 命令行

`paperseek` 命令与 Web 界面使用相同的应用服务。执行真实检索前，先在进程环境中配置模型与数据源凭据。

## 六个命令

| 命令 | 用途 |
| --- | --- |
| `serve` | 启动本地 HTTP 服务，默认同时启动 worker |
| `worker` | 使用指定本地数据库运行独立 worker |
| `search` | 在终端执行问题并返回 JSON |
| `doctor` | 输出配置与安装诊断 |
| `migrate` | 迁移本地数据或转换旧配置 |
| `version` | 显示安装版本 |

使用 `paperseek <命令> --help` 查看当前安装的完整参数。

## 执行检索

```sh
paperseek search "How does open-source funding affect maintenance?" --source openalex --search-budget medium --output json
```

数据源 ID：`openalex`、`arxiv`、`crossref`、`semanticscholar`、`pubmed`、`wos`、`googlescholar`、`paperhub`、`wanfang`、`nssd`、`yiigle`。

预算使用 `fast`、`medium`、`high`、`max`。支持学科筛选的数据源使用 `--field-id`；文本范围提示使用 `--field-hint`；支持引用扩展时，可用 `--citation-enabled` 或 `--no-citation-enabled` 控制。

```sh
paperseek search "Efficient long-context language models" --source arxiv --field-id cs.CL --search-budget fast
```

模型覆盖参数包括 `--llm-provider`、`--llm-api-type`、`--llm-model` 和 `--llm-base-url`。密钥应从环境读取，避免把真实值写入命令历史。

## 指定数据库

```sh
paperseek doctor --database /data/paperseek/paperseek.db --json
paperseek serve --database /data/paperseek/paperseek.db --port 8765
```

默认路径为 `~/.paperseek/paperseek.db`。需要 Web、CLI 和 MCP 访问同一批历史时，请统一数据库路径。

诊断报告可用于检查缺失配置，但不代表后续每次上游请求都会成功。另见[配置参考](../reference/configuration.md)与[迁移指南](../self-hosting/upgrade.md)。
