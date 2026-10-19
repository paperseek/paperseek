# 数据源

选择适合研究问题的文献索引，不同提供方的覆盖、元数据、访问要求和筛选能力并不相同。

## 数据源目录

| 数据源 | 社区版 | 在线版 BYOK | 侧重 |
| --- | --- | --- | --- |
| [OpenAlex](https://openalex.org/) | 建议配置 Key | 必须配置 Key | 广泛学术发现与引用扩展 |
| [arXiv](https://arxiv.org/) | 无需 Key | 无需 Key | 科学预印本 |
| [Crossref](https://www.crossref.org/) | 无需 Key | 无需 Key | DOI 与出版元数据 |
| [Semantic Scholar](https://www.semanticscholar.org/) | Key 可选 | 必须配置 Key | 论文与学术图谱元数据 |
| [PubMed](https://pubmed.ncbi.nlm.nih.gov/) | NCBI Key 可选 | 必须配置 Key | 生物医学与生命科学 |
| Google Scholar / [Serper](https://serper.dev/) | 必须配置 Serper Key | 必须配置 Key | 通过受支持 API 检索 Scholar |
| PaperHub 计算机顶会 | 无需 Key | 无需 Key | PaperHub 提供的顶会集合 |
| [Web of Science Starter](https://developer.clarivate.com/apis/wos-starter) | 需要 Clarivate Key | 必须配置 Key | 受支持的 WoS 集合 |

PaperSeek 服务的数据源访问由 PaperSeek 管理；Web of Science 暂不通过 PaperSeek 服务提供，可在在线版使用自己的密钥，或使用社区版。社区版按上表允许匿名访问或使用可选密钥；在线版 BYOK 是单独的访问策略，在设置中填写并保存数据源密钥；除 arXiv、Crossref、PaperHub 等无需密钥的数据源外，其余数据源（包括 OpenAlex、PubMed、Semantic Scholar）都必须提供密钥。模型 Key 与数据源 Key 并不通用。

## 配置与测试数据源

打开**设置 → 数据源**，点击来源所在行的**配置**或**配置密钥**，填写密钥和选项后点击**保存**。

此弹窗中的**测试连接**也会先保存当前密钥和选项，再测试。若保存失败，则不执行测试；保存成功后，即使测试失败或随后取消弹窗，也不会撤销保存。如需修改或清除已保存的密钥，请重新打开来源配置。列表中的**测试**和**全部测试**使用已配置的凭据，不会保存表单草稿。模型连接草稿的测试不会保存，详见[模型与排序](models.md)。

## 申请凭据

- [OpenAlex API 设置](https://openalex.org/settings/api)。
- [Semantic Scholar Key 申请](https://www.semanticscholar.org/product/api#api-key-form)。
- [NCBI API 指南](https://www.ncbi.nlm.nih.gov/books/NBK25497/)。
- [Serper 账户](https://serper.dev/)。
- [WoS Starter 应用](https://developer.clarivate.com/apis/wos-starter)。

上游订阅和速率限制，与 PaperSeek 账户及积分分别管理。

## 零结果与临时故障

主检索返回零篇论文时，PaperSeek 最多再尝试一次恢复检索，利用数据源反馈（例如 PubMed 未识别的短语）调整术语或可选限制。执行前，另一次模型检查必须确认核心概念、必要关系和明确限制得到保留。这个检查不保证论文相关性，仍需查看检索过程和结果。没有合适检索式通过检查，或数据源依然没有命中时，会明确提示，不会默默删除研究必需条件；领域筛选始终继续应用。

限流和异常接口响应会作为数据源问题报告，不会当作零命中。请求采用有上限的重试，并遵守 `Retry-After`。同一 worker 进程内，相同数据源和凭据的请求共享节流与冷却时间，重试也受同样控制；Semantic Scholar 批量检索接口的冷却不会阻断普通相关性检索。后续检索式调整或翻页失败时，保留已有候选；辅助通道不可用则带警告继续。不同 worker 进程或其他应用仍可能共享上游账户配额，因此重试不能保证数据源恢复。

## 领域与分类

OpenAlex 使用受支持的学科选项，语义通道按支持的方式执行筛选；arXiv 使用 Category；WoS 学科与 WoS 数据库选择独立。其他来源使用文本领域提示。

限制领域可能排除跨学科文献，除非研究设计要求，否则可先不限制。

## 元数据缺失

记录可能没有摘要、年份、引用数或开放 PDF。信息缺失不是质量判断。

1.0.0-beta2 不再提供独立的跨源摘要补全或引用数补全开关。写综述时请选择摘要信息充分的文献。

## 多 Key 轮询

OpenAlex 与 Serper 支持用空白、逗号或分号分隔的 Key 池。请求会轮换起始 Key，鉴权失败与临时错误分别处理。

轮询不会改变提供方的条款，也不保证仍有余额，详见[配置参考](../reference/configuration.md)。
