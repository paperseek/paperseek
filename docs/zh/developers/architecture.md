# 研究工作区维护指南

社区版是共享 Python 与 Vue 实现的来源。在线版基于相同源码构建，部署适配层不应复制算法或页面。

共享 UI 变量、布局规则和样式归属见[界面规范](./design-system.md)。下表中 Python 路径相对于 `backend/src/`，Web 路径相对于仓库根目录。

| 职责 | 位置 |
| --- | --- |
| 精简原生元数据与文献身份 | `paperseek_core/bibliometrics/corpus.py` |
| 分布与覆盖率 | `paperseek_core/bibliometrics/statistics.py` |
| 有界稀疏网络 | `paperseek_core/bibliometrics/networks.py` |
| 分析的创建、读取、导出与删除 | `paperseek_app/bibliometrics/services.py` |
| 存储契约与轻量记录模型 | `paperseek_app/bibliometrics/ports.py`、`models.py` |
| 社区版持久化与 HTTP 组合 | `paperseek_community/persistence/sqlite_bibliometrics.py`、`api_routes/bibliometrics.py` |
| 导航、菜单与模块历史 | `web/src/features/workspace/` |
| 分析视图、控件与浏览器布局 worker | `web/src/features/bibliometrics/` |
| 检索草稿与模型连接输入 | `web/src/features/search/search-draft.ts`、`web/src/features/search/use-search-configuration.ts`、`web/src/features/settings/ConnectionSheet.vue` |
| 综述证据与来源图谱快照 | `web/src/features/review/ReviewEvidence.vue` |
| 共享设置契约与校验 | `paperseek_app/settings.py` |
| 部署无关的连接检测 | `paperseek_app/settings_probes.py`（客户端与来源由部署层提供） |
| 综述结构化输出与修复 | `paperseek_core/review/structured.py` |
| 综述证据提取与大纲 | `paperseek_core/review/planning.py` |
| 综述正文生成与核对 | `paperseek_core/review/generation.py`；共享提示词在 `prompts.py` |

检索在通道选择与融合前保留已返回页面的元数据，排序输入仍受原有上限约束。完成的任务独立写入 `bibliometric_corpus` 产物；结果 API 与事件流不传输候选集。社区版恢复检查点保留候选集；在线版可按策略跳过检查点，但仍保留最终产物。

创建分析时，将精简候选集和概览复制到独立压缩文件。SQL 保存摘要、描述信息和有界网络缓存引用。详情请求只读概览，网络计算与候选集导出按需加载候选集。删除流程先标记记录，再删除文件，最后保留最小幂等墓碑，避免迟到的重试重新创建记录。适配层在向用例暴露文件描述信息前检查所有权。

核心层明确限制网络规模，API 限制并发昂贵操作，浏览器 worker 只负责布局。精简候选集不重复保存大型源载荷、摘要、完整结果或模型凭据。不要将重型分析移入历史列表或页面挂载过程。

检索与综述的显示标题独立于问题、主题及执行版本。各模块历史共用 UI 行为，API 各自负责持久化。未保存的密钥输入位于表单内存；已保存连接与来源密钥由 `paperseek_community/workspace_settings.py` 加密，`web/src/features/settings/store.ts` 只缓存公开元数据。提交任务时另存加密凭据副本。账户切换时需重置草稿及按用户隔离的设置与历史。恢复历史任务不得静默改用当前服务默认值。

相关变更后，运行 `test_bibliometrics.py`、检索边界与恢复契约、导入边界检查、前端类型检查，以及 `web/tests/browser/` 下的 `shell.spec.ts`、`settings.spec.ts`、`records.spec.ts`、`reviews.spec.ts`。可选脚本 `backend/scripts/benchmark_bibliometrics.py` 记录 3,000／10,000 篇合成文献的开销，不代表远程服务或浏览器延迟。

## 工作区界面

`web/src/features/workspace/WorkspaceShell.vue` 负责顶部栏、完全收起的侧栏、移动端抽屉和主滚动区。`Sidebar.vue` 负责模块导航与历史，`WorkspaceSettingsMenu.vue` 负责账户菜单。`WorkspacePreferences.vue` 提供模型 API、数据源、通用和用户四个设置页。

`web/src/features/settings/` 下的 `SettingsModels.vue`、`ConnectionSheet.vue`、`SettingsSources.vue`、`SettingsUser.vue` 实现连接、数据源和数据管理。模型连接草稿可在不保存的情况下测试；数据源弹窗测试则先调用 `saveSource(false)` 保存密钥和选项，测试失败或取消弹窗不会回滚。两类弹窗也都提供显式保存操作；默认项选择即时保存。检索草稿选择已保存连接的 ID，全局默认值不替换历史综述自身的模型设置。

`web/src/styles/ui.css` 负责变量和基础控件，`web/src/styles/app.css` 负责外壳与浮层。在线版通过共享外壳的账户和历史插槽扩展，不复制外壳。
