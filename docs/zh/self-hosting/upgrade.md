# 升级至 1.0.0-beta4

已在使用更早的 1.0 测试版？请[直接原地升级](#from-an-earlier-1-0-beta)，无需迁移。

与 0.2 相比，1.0 系列是一次完整的架构重构，Python 运行时、前端、本地存储和集成都包含不兼容变更。用户数据有明确迁移路径；旧 Python 导入路径、内部 API 与旧前端接口不属于兼容承诺。本页其余部分说明如何把 0.2 安装升级至 1.0.0-beta4。

## 0.2 安装需要调整什么

| 范围 | 从 0.2 升级的要求 |
| --- | --- |
| 运行环境 | 使用 Python 3.12 或更新版本，在独立环境中安装 |
| 启动入口 | 使用 `paperseek serve` 及当前六命令 CLI，更新调用废弃命令的脚本 |
| 外部集成 | 客户端改用 `/api/v1`；需要 MCP 时安装同版本 `paperseek-mcp` |
| 配置 | 一次性转换仍有效的旧环境变量，检查忽略项，再将新名称加载到进程环境 |
| 存储 | 显式迁移 0.2 数据库；新 SQLite 结构变化由 Alembic 管理 |
| 运行中任务 | 切换前完成或取消旧任务，不假定旧版本的未完成任务或检查点可由新工作流继续执行 |

新应用分离检索核心、任务服务和部署适配，保存的任务由 worker 租约、事件、检查点与产物共同管理。背景见[更新日志](../changelog.md#v1-0-0-beta)与[架构说明](../developers/contributing.md#architecture)。

本页适用于社区版本地安装。在线版用户不需要执行本地数据库迁移。

## 升级前

1. 完成或取消活动任务，再停止旧应用与 worker。
2. 将数据库、产物及配置备份到源码目录之外。
3. 在独立环境中安装 1.0.0-beta4。
4. 先验证迁移副本，再替换旧安装。

确认保存的问题、结果数量和导出文件之前，请保留原备份。

## 迁移 0.2 数据库

明确指定来源、目标和备份：

```sh
paperseek migrate --from-v0.2 /backup/old-paperseek.db --database /data/paperseek/paperseek.db --backup /backup/old-paperseek.v02.db
```

不指定独立 `--database` 时，命令会以源路径为目标。演练时建议使用独立目标，便于检查。

阅读迁移报告，不要用尚未验证的输出替换原数据库。

## 转换旧环境文件

配置转换需要单独执行：

```sh
paperseek migrate --legacy-env /backup/old.env --output-env /private/paperseek.env
```

检查转换结果和被忽略的配置。生成的文件不会被 `paperseek serve` 自动加载，需要导出到进程环境，或交给服务管理器加载。

不要将 `--legacy-env` 与数据库迁移参数混用。真实 Key 和迁移报告均不应提交到 Git。

## 验证新安装

```sh
paperseek version
paperseek doctor --database /data/paperseek/paperseek.db --json
paperseek serve --database /data/paperseek/paperseek.db
```

打开历史记录，检查一个旧任务并导出结果，再使用自己的提供方配置执行一次小规模新检索。

## 从更早的 1.0 测试版升级 {#from-an-earlier-1-0-beta}

1.0.0-beta4 沿用更早 1.0 测试版的数据库结构、配置和命令行，可以原地升级，无需迁移。

1. 完成或取消活动任务，停止服务及 worker，并备份数据库。
2. 安装新版本：

   ```sh
   python -m pip install --upgrade "paperseek==1.0.0b4"
   ```

   如使用 MCP 适配器，在同一条命令中加上 `"paperseek-mcp==1.0.0b4"`。使用 Docker 时，在 `.env` 中把 `PAPERSEEK_VERSION` 设为 `1.0.0-beta4`，再依次执行 `docker compose pull` 和 `docker compose up -d`。
3. 启动服务，并用 `paperseek version` 确认版本。

升级后最多同时运行 4 个检索和综述任务，可用 `PAPERSEEK_MAX_CONCURRENT_RUNS` 修改。如果你的网络需要代理才能访问模型或数据源，请在 **设置 › 通用 › 网络代理** 中设置，或使用[网络代理](../reference/configuration.md#网络代理)中的环境变量。

尚未写出正文的综述，下次生成大纲时会重新提取证据，因此会再次调用写作模型；已有正文的综述保留原有证据。

随附的 `compose.yaml` 只向本机 127.0.0.1 发布端口。如需从其他设备访问，请使用新的 `compose.yaml`，在 `.env` 中设置 `PAPERSEEK_HOST=0.0.0.0`，并按[网络访问](index.md#network-access)中的说明保护访问。

## 回退

先停止新进程，再恢复完整的升级前数据及相应旧运行环境。不要直接让旧程序打开已经向前迁移的数据库。
