# Third-party notices / 第三方声明

PaperSeek is distributed under the Apache License 2.0. This page identifies direct Python runtime dependencies, bundled interface and documentation assets, adapted designs, and the external data PaperSeek reads at run time. Transitive Python dependencies retain notices in their separately installed distributions.

PaperSeek 使用 Apache License 2.0 发布。本页说明直接 Python 运行依赖、随产品和文档站交付的界面资源、参考改写的设计，以及运行时读取的外部数据。间接 Python 依赖保留各自独立安装包中的声明。

## Runtime dependencies / 运行依赖

| Project | License | PaperSeek usage |
| --- | --- | --- |
| [Alembic](https://github.com/sqlalchemy/alembic) | MIT | Database migrations. |
| [cryptography](https://github.com/pyca/cryptography) | Apache-2.0 OR BSD-3-Clause | Authenticated encryption of saved and run credentials. |
| [FastAPI](https://github.com/fastapi/fastapi) | MIT | HTTP API and OpenAPI schema. |
| [httpx](https://github.com/encode/httpx) | BSD-3-Clause | Model and academic-source HTTP clients. |
| [SQLAlchemy](https://github.com/sqlalchemy/sqlalchemy) | MIT | Database migration support. |
| [Uvicorn](https://github.com/encode/uvicorn) | BSD-3-Clause | ASGI server. |
| [Pydantic](https://github.com/pydantic/pydantic) | [MIT](https://github.com/pydantic/pydantic/blob/main/LICENSE) | Typed configuration, search plans, checkpoints, and search-state contracts. |
| [python-docx](https://github.com/python-openxml/python-docx) | MIT | Basic DOCX review export. |
| [lxml](https://lxml.de/) | BSD-3-Clause (libxml2/libxslt retain their own notices) | XML serialization used by python-docx. |

These packages are installed as separate Python distributions and retain their own license and copyright notices.

上述项目作为独立 Python 包安装，并保留各自的许可证和版权声明。

## PaperHub integration / PaperHub 集成

[Yupu-Wang/paper-hub](https://github.com/Yupu-Wang/paper-hub) provides the manifest and content-addressed shards used by the computer-science top-conference source. PaperSeek integrates this source with the author's permission and reads the published index at runtime.

[Yupu-Wang/paper-hub](https://github.com/Yupu-Wang/paper-hub) 为计算机顶会数据源提供 manifest 和内容哈希 shard。PaperSeek 经作者同意集成该数据源，并在运行时读取公开索引。

## paper-search-pro / 设计来源

Portions of PaperSeek are adapted from [paper-search-pro](https://github.com/O0000-code/paper-search-pro), Copyright 2026 Bo, licensed under the [Apache License 2.0](https://github.com/O0000-code/paper-search-pro/blob/main/LICENSE.txt): the journal-ranking table parsing and ISSN matching (`paperseek_core/journal_metrics.py`), the OpenReview acceptance rules and venue labels (`paperseek_core/acceptance.py`, `paperseek_core/sources/openreview.py`), and the NSSD and Chinese Medical Journals query forms (`paperseek_core/sources/nssd.py`, `paperseek_core/sources/yiigle.py`). The code was rewritten for PaperSeek; the adapted files say so in their module notes.

PaperSeek 的部分功能参考并改写自 [paper-search-pro](https://github.com/O0000-code/paper-search-pro)（Copyright 2026 Bo，[Apache License 2.0](https://github.com/O0000-code/paper-search-pro/blob/main/LICENSE.txt)）：期刊分区表解析与 ISSN 匹配、OpenReview 录用判定与会议标签，以及国家哲学社会科学学术期刊数据库和中华医学期刊库的检索式写法。相关代码已为 PaperSeek 重写，并在模块说明中注明来源。

## Runtime data sources / 运行时数据

| Source | What PaperSeek reads | Terms |
| --- | --- | --- |
| [ShowJCR](https://github.com/hitfyd/ShowJCR) | CAS Journal Ranking, JCR quartiles and Impact Factor, and the CCF recommended list, downloaded into the local workspace on first use | ShowJCR is GPL-3.0. PaperSeek bundles none of its code or data. CAS rankings © 中科院文献情报中心, JCR © Clarivate, CCF list © China Computer Federation. |
| [OpenReview](https://openreview.net/) | Public search results for accepted papers | OpenReview terms of use |
| [Wanfang Data](https://www.wanfangdata.com.cn/) | Search API with the user's AppKey | Wanfang API terms |
| [National Social Sciences Database](https://www.ncpssd.cn/) | Public search results: metadata and abstracts | Site terms; full text is not downloaded |
| [Chinese Medical Journals](https://www.yiigle.com/) | Public search results: metadata and abstracts | Site terms; full text is not downloaded |

上述数据在运行时按用户的检索请求读取，PaperSeek 不随产品分发这些数据。ShowJCR 采用 GPL-3.0，PaperSeek 未包含其代码或数据；中科院分区、JCR、CCF 目录的权利分别归中科院文献情报中心、Clarivate 和中国计算机学会所有。

## Community interface assets

The bundled Vue interface uses [Lucide](https://lucide.dev/) icons (ISC) and self-hosted [Geist / Geist Mono](https://github.com/vercel/geist-font) fonts (SIL Open Font License 1.1). Their license and copyright notices are distributed alongside the static assets in `static/third-party-notices.txt`. The styling follows shadcn/ui conventions; no shadcn/ui or React code is bundled.

## Bundled product interface / 产品界面

| Project | License | Usage |
| --- | --- | --- |
| [Vue](https://github.com/vuejs/core) | MIT | Product interface runtime. |
| [Vue Router](https://github.com/vuejs/router) | MIT | Product navigation. |
| [D3 force](https://github.com/d3/d3-force) | ISC | Citation and bibliometric graph layout. |
| [Lucide](https://github.com/lucide-icons/lucide) | ISC | Interface icons. |
| [Geist / Geist Mono](https://github.com/vercel/geist-font) | OFL-1.1 | Self-hosted product fonts. |

## Documentation site / 文档站

The documentation is a separate Node project. Its assets are not required by the Community Python runtime.

文档站是独立的 Node 项目；这些资源不是 Community Python 运行依赖。

| Project | License | Usage |
| --- | --- | --- |
| [VitePress](https://github.com/vuejs/vitepress) and Vue | MIT | Documentation site and interactive theme. |
| [p5.js](https://github.com/processing/p5.js) | LGPL-2.1 | Homepage canvas illustration. |
| [p5.brush](https://github.com/acamposuribe/p5.brush) | MIT | Homepage brush rendering. |
| [GSAP](https://gsap.com/standard-license/) | GSAP Standard License | Homepage animation. |
| Geist / Geist Mono | OFL-1.1 | Self-hosted documentation fonts. |
| [Noto Sans SC](https://github.com/notofonts/noto-cjk) | OFL-1.1 | Self-hosted Chinese documentation font. |

## Independent MCP adapter / 独立 MCP 适配器

[The Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk) is used by the separately installed `paperseek-mcp` package under its MIT license.

When changing dependencies, update this inventory and preserve upstream license/copyright files in each delivered distribution. The package metadata and lockfiles record exact versions.
