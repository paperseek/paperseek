# Data sources

Choose an index that fits your research. Coverage, metadata, access, and filters vary across providers.

## Source directory

| Source | Community | Online BYOK | Focus |
| --- | --- | --- | --- |
| [OpenAlex](https://openalex.org/) | Key recommended | Key required | Broad discovery and citation expansion |
| [arXiv](https://arxiv.org/) | No key | No key | Scientific preprints |
| [Crossref](https://www.crossref.org/) | No key | No key | DOI and publication metadata |
| [Semantic Scholar](https://www.semanticscholar.org/) | Optional key | Key required | Papers and scholarly graph metadata |
| [PubMed](https://pubmed.ncbi.nlm.nih.gov/) | Optional NCBI key | Key required | Biomedical and life-science research |
| Google Scholar via [Serper](https://serper.dev/) | Serper key required | Key required | Scholar results through the supported API |
| PaperHub top CS conferences | No key | No key | PaperHub's conference collection, widened with papers OpenReview lists as accepted |
| [Web of Science Starter](https://developer.clarivate.com/apis/wos-starter) | Clarivate key required | Key required | Supported WoS collection when enabled |
| [Wanfang Data](https://www.wanfangdata.com.cn/) | Wanfang AppKey required | Your own key required | Chinese journal articles, theses, and conference papers |
| [National Social Sciences Database (NSSD)](https://www.ncpssd.cn/) | No key | Not available | Chinese social sciences and humanities journals |
| [Chinese Medical Journals (Yiigle)](https://www.yiigle.com/) | No key | Not available | Chinese Medical Association journals: titles and abstracts |

PaperSeek Service manages source access for you, except Web of Science and Wanfang Data: PaperSeek provides no key for these, so with PaperSeek Service too you save your own key under **Settings → Model service**. Community permits anonymous access or optional keys as shown above. Online BYOK has a separate access policy: add and save source keys in Settings for every source that takes a key, including OpenAlex, PubMed, and Semantic Scholar. arXiv, Crossref, and PaperHub need no key. A model key is not a data-source key.

## Chinese databases

Searches on Wanfang Data, NSSD, and Chinese Medical Journals are planned and built in Chinese: the search plan, its concept groups, and every query use the Chinese terminology these databases index, whatever language you ask in. Widely used abbreviations such as SGLT2 or COVID-19 stay as they are.

Each database takes its own query form:

| Database | Query PaperSeek sends |
| --- | --- |
| Wanfang Data | A Wanfang PQ expression, for example `(数字治理 OR 数字化治理) AND 乡村振兴`; a 标题:, 关键词:, or 摘要: field only when a core concept needs it |
| NSSD | Chinese terms with AND, OR, and NOT; PaperSeek searches each term in the title, subject terms, and abstract |
| Chinese Medical Journals | Two to four space-separated Chinese terms. The database matches all of them and has no OR, so each concept keeps one term |

Results from these databases carry Chinese titles, authors, venues, and abstracts. NSSD and Chinese Medical Journals return metadata and abstracts only; PaperSeek never downloads their full text. Wanfang needs an AppKey ([apply here](https://ai.wfdata.com/skills/api)): add it in **Settings → Data sources** or set `PAPERSEEK_WANFANG_API_KEY`.

## Configure and test a source

Open **Settings → Data sources** and choose **Configure** or **Configure key** on the source row. Enter the key and options, then click **Save**.

**Test connection** inside this dialog also saves the current key and options before testing. If saving fails, the test does not start. Once saving succeeds, a failed test or subsequently cancelling the dialog does not undo it; reopen the source configuration to change or clear the saved key. The table's **Test** and **Test all** use the configured credentials without saving a form draft. Model connection draft tests do not save; see [models and ranking](models.md).

## Obtain credentials

- [OpenAlex API settings](https://openalex.org/settings/api).
- [Semantic Scholar key request](https://www.semanticscholar.org/product/api#api-key-form).
- [NCBI API guidance](https://www.ncbi.nlm.nih.gov/books/NBK25497/).
- [Serper account](https://serper.dev/).
- [WoS Starter application](https://developer.clarivate.com/apis/wos-starter).
- [Wanfang AppKey application](https://ai.wfdata.com/skills/api).

Provider subscriptions and rate limits are separate from PaperSeek accounts and credits.

## Empty results and temporary failures

When a search returns no papers, PaperSeek tries up to two more queries. Each one uses the earlier queries and their counts, and source feedback such as PubMed's unrecognized phrases, to adjust terminology or optional restrictions. Before a query runs, a separate model check must confirm that it keeps the core concepts, required relationships, and explicit constraints. This check is not a guarantee of relevance; inspect the search process and returned papers. If no suitable query passes, the source still has no matches, or the source cannot be reached for a follow-up query, the search ends with no papers and a notice explaining why, instead of silently dropping required conditions. Field filters remain applied.

While refining any search, a proposed query that was already run is sent back to the model once, with its earlier result, before refinement stops.

Rate limits and malformed API responses are reported as source problems, not zero matches. Requests use bounded retries and respect `Retry-After`; ordinary failures are retried after about a second, rate limits after a longer pause. All tasks running in one PaperSeek process share pacing and cooldowns for the same source and credential, including retries. arXiv, for example, receives one request every three seconds however many searches are running. Semantic Scholar's bulk-search cooldown does not block ordinary relevance search. Existing candidates survive later query or pagination failures, and unavailable supplementary lanes produce warnings. Separate worker processes or other applications using the same provider account still share the upstream quota, so a retry cannot guarantee availability.

## Network proxy

If your network cannot reach a source or model provider directly, or a source has throttled your network address, set up a proxy in **Settings › General › Network proxy**. You choose which requests go through it: model requests, sources that limit by network address (arXiv, Crossref, PaperHub, NSSD, Chinese Medical Journals, and PubMed or OpenAlex without a key), and other sources. A proxy does not raise a key's quota. See [Network proxy](../reference/configuration.md#network-proxy) for the details and the matching environment variables.

## Fields and categories

OpenAlex uses supported discipline options; its semantic lane applies supported filtering behavior. arXiv uses categories. WoS categories and the WoS database selector are separate. Other sources use textual field guidance.

Restricting a field can exclude interdisciplinary work. Start without it unless your research design requires the constraint.

## Missing metadata

Records can lack abstracts, years, citation counts, or open PDF links. Missing metadata is not a quality judgment.

Version 1.0.0-beta4 has no separate cross-source abstract-filling or citation-count enrichment switch. Choose informative abstracts for review writing.

## Multiple keys

OpenAlex and Serper support key pools separated by whitespace, commas, or semicolons. Requests rotate the starting key; authentication failures are handled separately from transient errors.

Rotation does not change provider terms or guarantee remaining credit. See [configuration](../reference/configuration.md).
