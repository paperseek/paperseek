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
| PaperHub 计算机顶会 | 无需 Key | 无需 Key | PaperHub 提供的顶会集合，并补充 OpenReview 上已录用的论文 |
| [Web of Science Starter](https://developer.clarivate.com/apis/wos-starter) | 需要 Clarivate Key | 必须配置 Key | 受支持的 WoS 集合 |
| [万方数据](https://www.wanfangdata.com.cn/) | 需要万方 AppKey | 必须自备 Key | 中文期刊论文、学位论文和会议论文 |
| [国家哲学社会科学学术期刊数据库](https://www.ncpssd.cn/) | 无需 Key | 暂不提供 | 中文哲学社会科学期刊 |
| [中华医学期刊库](https://www.yiigle.com/) | 无需 Key | 暂不提供 | 中华医学会系列期刊的题录和摘要 |

PaperSeek 服务的数据源访问由 PaperSeek 管理，Web of Science 和万方数据除外：站点不提供这两个库的密钥，即使使用 PaperSeek 服务，也需要在**设置 → 模型服务**中保存自己的密钥。社区版按上表允许匿名访问或使用可选密钥；在线版 BYOK 是单独的访问策略，在设置中填写并保存数据源密钥；除 arXiv、Crossref、PaperHub 等无需密钥的数据源外，其余数据源（包括 OpenAlex、PubMed、Semantic Scholar）都必须提供密钥。模型 Key 与数据源 Key 并不通用。

## 中文数据库

在万方数据、国家哲学社会科学学术期刊数据库和中华医学期刊库检索时，检索计划、概念组和每一条检索式都用中文构建，使用这些数据库实际标引的中文学术用语；无论你用中文还是英文提问都是如此。SGLT2、COVID-19 这类通用缩写会原样保留。

三个数据库的检索式写法各不相同：

| 数据库 | PaperSeek 发送的检索式 |
| --- | --- |
| 万方数据 | 万方 PQ 表达式，例如 `(数字治理 OR 数字化治理) AND 乡村振兴`；只有核心概念需要时才加 标题:、关键词:、摘要: 字段 |
| 国家哲学社会科学学术期刊数据库 | 用 AND、OR、NOT 连接的中文检索词；PaperSeek 会在题名、主题词和摘要中检索每个词 |
| 中华医学期刊库 | 2–4 个用空格分隔的中文词。该库要求所有词同时出现，不支持 OR，所以每个概念只保留一个词 |

这些数据库返回中文题名、作者、刊名和摘要。国家哲学社会科学学术期刊数据库和中华医学期刊库只提供题录和摘要，PaperSeek 不会下载其全文。万方需要 AppKey（[申请地址](https://ai.wfdata.com/skills/api)），可在**设置 → 数据源**中填写，或设置环境变量 `PAPERSEEK_WANFANG_API_KEY`。

## 配置与测试数据源

打开**设置 → 数据源**，点击来源所在行的**配置**或**配置密钥**，填写密钥和选项后点击**保存**。

此弹窗中的**测试连接**也会先保存当前密钥和选项，再测试。若保存失败，则不执行测试；保存成功后，即使测试失败或随后取消弹窗，也不会撤销保存。如需修改或清除已保存的密钥，请重新打开来源配置。列表中的**测试**和**全部测试**使用已配置的凭据，不会保存表单草稿。模型连接草稿的测试不会保存，详见[模型与排序](models.md)。

## 申请凭据

- [OpenAlex API 设置](https://openalex.org/settings/api)。
- [Semantic Scholar Key 申请](https://www.semanticscholar.org/product/api#api-key-form)。
- [NCBI API 指南](https://www.ncbi.nlm.nih.gov/books/NBK25497/)。
- [Serper 账户](https://serper.dev/)。
- [WoS Starter 应用](https://developer.clarivate.com/apis/wos-starter)。
- [万方 AppKey 申请](https://ai.wfdata.com/skills/api)。

上游订阅和速率限制，与 PaperSeek 账户及积分分别管理。

## 零结果与临时故障

检索返回零篇论文时，PaperSeek 最多再尝试两次。每次都会参考之前的检索式及其结果数，以及数据源反馈（例如 PubMed 未识别的短语），调整术语或可选限制。检索式执行前，另一次模型检查必须确认核心概念、必要关系和明确限制得到保留。这个检查不保证论文相关性，仍需查看检索过程和结果。没有合适检索式通过检查、数据源依然没有命中，或补充检索时无法连接数据源，检索都会以零结果结束并给出原因，不会默默删除研究必需条件；领域筛选始终继续应用。

调整任何检索时，如果模型给出的检索式之前已经执行过，会把之前的结果告诉模型并再请求一次，仍然重复才停止调整。

限流和异常接口响应会作为数据源问题报告，不会当作零命中。请求采用有上限的重试，并遵守 `Retry-After`；一般故障约 1 秒后重试，限流则等待更久。同一 PaperSeek 进程内运行的所有任务，对相同数据源和凭据共享节流与冷却时间，重试也受同样控制。例如无论同时运行多少个检索，arXiv 都是每 3 秒一次请求；Semantic Scholar 批量检索接口的冷却不会阻断普通相关性检索。后续检索式调整或翻页失败时，保留已有候选；辅助通道不可用则带警告继续。不同 worker 进程或其他应用仍可能共享上游账户配额，因此重试不能保证数据源恢复。

## 网络代理

如果你的网络无法直接访问某个数据源或模型服务，或者数据源对你的网络地址限速，可以在 **设置 › 通用 › 网络代理** 中设置代理，并选择哪些请求走代理：大模型请求、按网络地址限速的数据源（arXiv、Crossref、PaperHub、OpenReview、国家哲学社会科学学术期刊数据库、中华医学期刊库、ShowJCR 分区表下载，以及未填写密钥的 PubMed 和 OpenAlex）、其他数据源。代理不会增加密钥的额度。详情及对应的环境变量见[网络代理](../reference/configuration.md#网络代理)。

## 领域与分类

OpenAlex 使用受支持的学科选项，语义通道按支持的方式执行筛选；arXiv 使用 Category；WoS 学科与 WoS 数据库选择独立。其他来源使用文本领域提示。

限制领域可能排除跨学科文献，除非研究设计要求，否则可先不限制。

## 元数据缺失

记录可能没有摘要、年份、引用数或开放 PDF。信息缺失不是质量判断。

1.0.0-beta4 不再提供独立的跨源摘要补全或引用数补全开关。写综述时请选择摘要信息充分的文献。

## 多 Key 轮询

OpenAlex 与 Serper 支持用空白、逗号或分号分隔的 Key 池。请求会轮换起始 Key，鉴权失败与临时错误分别处理。

轮询不会改变提供方的条款，也不保证仍有余额，详见[配置参考](../reference/configuration.md)。
