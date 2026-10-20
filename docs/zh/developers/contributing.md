# 参与贡献

欢迎修正文档、改进数据源适配器、提交可复现问题和聚焦的产品改动。可以先从小问题开始；影响较大的行为调整，建议先在 Issue 中讨论。

## 目录职责

| 位置 | 职责 |
| --- | --- |
| `backend/src/paperseek_core` | 召回、数据源、排序与模型协议 |
| `backend/src/paperseek_app` | 应用用例与持久化端口 |
| `backend/src/paperseek_community` | SQLite、本地产物、HTTP 服务与 CLI |
| `web` | 共用的 Vue 3 + TypeScript 产品界面 |
| `integrations/mcp` | 独立 MCP 适配器 |
| `integrations/skills` | Agent 使用说明 |
| `docs` | 中英文公共文档，包含在线版使用指南 |

在线版基于同一份社区版发行包，公共行为请在本仓库修复。

## 架构 {#architecture}

1.0 系列完成了向模块化单体与持久任务执行架构的重构。研究逻辑、应用操作和部署适配各自承担明确职责，并共同交付为可直接使用的本地应用。

### 依赖边界

检索核心实现查询迭代、召回、候选选择、引用扩展、排序与数据源／模型协议。它可以使用 Pydantic 和 httpx，但不依赖网页路由、账户身份或数据库。应用服务负责提交、取消、执行与查询，通过明确的端口访问存储。社区版与在线版各自提供具体的存储、凭据、网络和部署策略。

`paperseek` 发行包包含 `paperseek_core`、`paperseek_app`、`paperseek_community` 和已构建的网页；核心与应用层不单独发布。`paperseek-mcp` 是可独立安装、版本匹配的协议适配器，调用同一应用服务。

### 任务生命周期

保存的检索在提交时记录有效配置并创建任务。worker 领取任务及租约，执行异步工作流，通过应用事务提交事件、检查点与产物引用。所需数据提交成功后，任务才能以完成状态呈现最终结果。

前端读取任务快照，再通过 SSE 跟随有序事件。社区版使用兼容的已提交检查点恢复，复用已经完成的步骤或批次。取消与租约校验阻止失效执行提交迟到结果；不兼容的工作流检查点会被拒绝，不会按新结构猜测恢复。

社区版将这些记录保存在 SQLite 中，以 Alembic 管理迁移，文件产物保存在本地。在线版提供 PostgreSQL、Storage、账户隔离及自身的任务恢复策略。在线匿名 BYOK 的研究内容只保存在内存，不创建持久研究任务。这些差异属于适配层与应用策略，不应进入检索算法。

### 共享前端与发行

Vue 3 + TypeScript 页面通过 `/api/v1` 工作；OpenAPI 生成脚本同时产生 API 类型和模型预设。修改后端契约时，需要同步生成前端表示。

各版本的变化见[更新日志](../changelog.md)，已有本地安装请按[升级指南](../self-hosting/upgrade.md)迁移。新模块的文件职责和验证入口见[工作区维护说明（英文）](../../developers/architecture.md)。

## 开发环境

使用 Python 3.12+、uv，以及 `web/package.json` 声明的 Node/npm 版本。

```sh
uv sync --locked --project backend
npm --prefix web ci
npm --prefix web run typecheck
npm --prefix web run build -- --outDir ../backend/src/paperseek_community/static --emptyOutDir
uv run --locked --project backend paperseek serve
```

前端迭代时，在后端启动后运行 `npm --prefix web run dev`。Vite 将 `/api` 代理到 `http://127.0.0.1:8765`，请让 `paperseek serve` 使用默认端口。

## 验证改动

```sh
uv run --directory backend --locked ruff check --config pyproject.toml src ../integrations/mcp/src
uv run --directory backend --locked python tests/import_boundaries.py
uv run --directory backend --locked python -m unittest discover -s tests/behavior -p "test_*.py"
uv run --directory backend --locked python scripts/generate_openapi_types.py --check
npm --prefix web run typecheck
npm --prefix web run build
```

Python 的 Ruff 检查覆盖生产源码的语法、未使用名称及部分正确性规则；mypy 当前检查 `backend/pyproject.toml` 列出的七个传输、凭据和共享设置模块，并非全后端类型检查。Web 使用严格 TypeScript 检查。

按实际改动选择检查，完整发行检查参考 CI。真实提供方实验与确定性测试分开，迁移和集成测试不能连接生产数据库。

### 测试范围与维护

在能发现问题的最低层保留回归测试。新增前先找到负责该行为的现有测试，优先补充用例，避免在每个适配层重复执行完整流程。

| 边界 | 主要检查 |
| --- | --- |
| 检索、数据源 HTTP 映射、排序、综述证据 | 使用受控提供方的后端行为测试 |
| SQLite 事务、迁移、取消、租约与恢复 | 持久化和生命周期测试，包含真实数据库及阻塞中的 I/O |
| 用户交互、移动端布局与无障碍 | Community 共用浏览器测试 |
| Hosted 身份验证、账户隔离、积分与 PostgreSQL | 基于固定 Community wheel 的 Hosted 测试 |
| 安装包、CLI 启动与 MCP 传输 | 现有独立安装检查和干净 Docker 构建 |

断言当前用例真正关心的结果：数据源请求映射、排序结果、越权被拒绝、数据仍然保留或用户上限得到执行。允许扩展的响应只检查相关字段。不保留提示词哈希、整张预设参数表、展示文案、私有函数位置，以及只验证测试替身自身的测试。文案或内部组织方式调整不应迫使测试同步修改。并发使用事件和可控时钟；超时用于防止挂死，不作为性能基准。浏览器测试优先使用角色、标签和可观察结果。

测试按功能放在 `backend/tests/behavior/test_*.py`。修改前先找下表中的负责位置；共用样例放在 `fixtures/`，测试文件之间不互相导入。

| 功能 | 修改用例的位置 |
| --- | --- |
| 提交、默认配置与检索语法 | `test_submission.py`、`test_query_syntax.py`；API、CLI、MCP 只验证参数传递和错误转换 |
| 检索与模型 | `test_query_*`、`test_search_*`、`test_candidate_selection.py`、`test_ranking.py`；HTTP 映射与重试集中在 `test_source_http.py`、`test_model_http.py`、`test_http_retry.py` |
| 综述内容 | `test_review_evidence.py`、`test_review_planning.py`、`test_review_generation.py`、`test_review_selection.py`、`test_review_output.py` |
| 综述存储与请求 | `test_review_storage.py` 负责持久化和恢复；`test_review_api.py` 负责 HTTP、版本和凭据生命周期，不重复验证导出排版 |
| 任务执行 | `test_run_*`、`test_worker.py`、`test_results.py`；数据库迁移仍在 `tests/migration` |
| 共用界面 | `web/tests/browser`；Hosted 浏览器用例验证账户和服务模式如何接入这些组件 |

默认值只在定义该行为的功能测试中验证。其他场景明确指定自己的配置、对照传入参数，或只检查与当前结果有关的字段。例如，并发排队用例自行设置槽位数量，部署默认容量变化不应改变这个场景。已有场景能够发现缺陷时，直接扩展该场景；机械修改不必为了增加数量而新增测试。

开发时先运行受影响的模块：

```sh
uv run --directory backend --locked python -m unittest discover -s tests/behavior -p "test_review*.py" -v -b
uv run --directory backend --locked python -m unittest discover -s tests/behavior -p "test_*.py" -k cancel -v -b
npm --prefix web run test:browser -- reviews.spec.ts
```

CI 仍在 Linux 和 Windows 执行行为测试；仅检查源码的 lint、类型和依赖方向检查在 Linux 执行一次。浏览器失败轨迹保留七天。失败后先排查断言和测试环境，增加重试次数或跳过失败测试不算修复。产品行为有意改变时，同步调整对应测试和用户文档。

仓库组织可参考 [LangChain 各库内的测试目录](https://github.com/langchain-ai/langchain/tree/master/libs/core/tests)和 [Dify 分开的后端单元、集成测试](https://github.com/langgenius/dify/tree/main/api/tests)。它们的 CI 会选择受影响的包或组件。本项目借鉴明确归属和局部运行，不照搬大型 CI 设施。[Vue 的公共行为测试指南](https://vuejs.org/guide/scaling-up/testing.html)和 [pytest 对不稳定测试的分析](https://docs.pytest.org/en/stable/explanation/flaky.html)说明了断言范围与隔离方法。

## 修改文档站

```sh
npm ci
npm run docs:dev
npm run docs:build
npm run docs:check
npm run docs:lint
cd web
npx playwright test --config playwright.docs.config.ts scrolling.spec.ts
```

英文页面位于 `docs/`，中文对应页面位于 `docs/zh/`。两棵树逐文件对应；某个页面只存在一种语言时 `docs:check` 会失败。行为变化时同步更新两种语言。站点配置和主题位于 `docs/.vitepress/`。

### 页面类型

每个页面只承担一项任务。动笔前先选好类型，并让页面只做这件事：

| 类型 | 回答的问题 | 结构 | 示例 |
| --- | --- | --- | --- |
| 教程 | 「我第一次怎么做？」 | 前提 → 步骤 → 可观察的结果 → 下一步 | [本地快速上手](../get-started/local.md) |
| 操作指南 | 「我怎么完成这项任务？」 | 目标 → 步骤 → 验证 → 注意事项 | [检索文献](../guide/search.md) |
| 参考 | 「确切的参数是什么？」 | 完整的字段、类型、默认值、约束 | [配置参考](../reference/configuration.md) |
| 解释 | 「为什么这样设计？」 | 原理 → 设计选择 → 边界与取舍 | [工作区与历史](../guide/workspace.md) |

任务指南开始解释架构，或参考页面开始教学时，应拆分页面并互相链接。

### 写作风格

- 标题以任务为先：写「配置数据源」，不写「数据源配置」。
- 短句。优先给出具体的命令、路径或按钮名，而不是抽象描述。
- 指南中不用宣传性语言。写明软件能做什么，也写明它不能做什么。
- 界面标签与产品一致：社区版是 **设置 › 模型 API**，在线版是 **设置 › 模型服务**。

### 方法边界与证据

PaperSeek 是研究工具，说明限制是正确用法的一部分：

- 文献计量页面必须说明：分析覆盖的是召回的候选集，不一定是该领域的完整文献。
- 综述页面必须区分：摘要中引用的证据、模型生成的文字、人工核实的结论。
- 每个功能页面都要写明不保证的内容：穷尽覆盖、研究质量评估或全文获取。

### 重定向与链接

不要删除已发布的 URL。移动页面后在原位置留下 noindex 占位页，模式见 `docs/user-manual.md`。存在失效内部链接时构建会失败。

公共指南介绍稳定产品行为。生产密钥、特定服务器清单、测试长日志和一次性发布记录不属于公共文档。产物不提交到源码；本地构建可通过 `DOCS_OUT_DIR` 和 `DOCS_CACHE_DIR` 指定外部目录。

凡是用户能感知的变化，都要同步更新对应的中英文操作指南或参考页：怎么使用、默认值和限制，以及失败或检查未完成意味着什么。安装包发布前，更新日志记在**尚未发布**下。测试组织方式属于本贡献指南，不放进用户操作流程。

Pull Request 会自动构建文档站检查错误，合并后网站自动更新。请只修改 `docs/` 下的源文件。

## 提交改动

说明具体问题、改动后的行为、受影响的界面与验证结果。界面修改请附前后截图，需要时补充行为测试，避免只检查临时文件布局。

请遵守[行为准则](https://github.com/paperseek/paperseek/blob/main/CODE_OF_CONDUCT.md)，保留署名，使贡献兼容 [Apache-2.0](https://github.com/paperseek/paperseek/blob/main/LICENSE) 许可。
