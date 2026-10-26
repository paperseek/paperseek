# 在本地完成第一次检索

在自己的设备上安装 PaperSeek，连接模型，把一个研究问题变成阅读清单。

不想安装？改为[在线完成第一次检索](online.md)。

## 1. 安装

需要 **Python 3.12 或更新版本**，并使用独立环境。

::: code-group
~~~bash [macOS / Linux]
python3 -m venv .venv
source .venv/bin/activate
python -m pip install "paperseek==1.0.0b4"
~~~
~~~powershell [Windows PowerShell]
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install "paperseek==1.0.0b4"
~~~
:::

从 1.0.0-beta4 源码检出安装时，改为 `python -m pip install ./backend`。修改前端源码后，请按[贡献指南](../developers/contributing.md)中的构建说明操作。

## 2. 启动

~~~bash
paperseek serve
~~~

打开 **[http://127.0.0.1:8765](http://127.0.0.1:8765)**，使用期间保持终端运行。

安装包已包含网页界面，运行它不需要 Node.js。

## 3. 连接模型

打开侧栏底部的**设置 → 模型 API**，点击**添加连接**。选择 **LLM**、服务商和模型，填写密钥；自定义服务还需要确认基础地址与协议。可先点击**测试连接**检查模型草稿，再点击**保存**。在**默认使用**的**检索与评分**中选择已保存的连接，然后通过侧栏的**文献检索**返回。

OpenAI Chat Completions、OpenAI Responses 和 Anthropic Messages 是不同协议，只看模型名称无法判断该用哪一种。

模型草稿的测试不会保存连接。数据源测试有不同的保存行为，见下文。如需提供服务端默认配置，见[配置参考](../reference/configuration.md)。

## 4. 输入问题

> 开源软件如何获得支持长期维护的资金？

如有 OpenAlex 密钥，打开**设置 → 数据源 → OpenAlex → 配置**，填写后点击**保存**。此数据源弹窗中的**测试连接**也会先保存当前密钥和选项，再执行测试；测试失败或随后取消弹窗，都不会撤销这次保存。

通过侧栏返回**文献检索**，选择 **OpenAlex**。只有必要时才在**更多选项**中限制研究领域。第一次运行可保留 **Auto**，也可以按问题的范围选择检索预算。

点击**开始检索**，或按 **Ctrl+Enter / ⌘+Enter**，在 PaperSeek 检索和排序时关注各阶段进展。

## 5. 阅读结果

打开**检索结果**，查看摘要与相关性理由，导出 CSV、JSON、BibTeX 或 RIS。社区版将历史保存在本地。

通过侧栏的**检索历史**重新打开某次运行，或点击标题旁的**全部**进入完整列表。有引用数据时查看**引用证据**，也可以勾选文献开始一篇[实验性综述](../guide/reviews.md)。从侧栏打开[文献计量](../guide/bibliometrics.md)，可分析完整召回的候选集。

## 遇到问题时

先查看[常见问题](../reference/troubleshooting.md)。本地环境检查可执行：

~~~bash
paperseek doctor --json
~~~

配置有效不代表上游在整个长时间检索中始终有余额或保持可用。

## 接下来

- [自部署](../self-hosting/index.md)：Docker 部署、数据目录、网络访问和备份。
- [检索文献](../guide/search.md)：预算、领域筛选、停止与重连。
