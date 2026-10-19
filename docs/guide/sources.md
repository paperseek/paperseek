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
| PaperHub top CS conferences | No key | No key | PaperHub's conference collection |
| [Web of Science Starter](https://developer.clarivate.com/apis/wos-starter) | Clarivate key required | Key required | Supported WoS collection when enabled |

PaperSeek Service manages source access for you. Community permits anonymous access or optional keys as shown above. Online BYOK has a separate access policy: add and save source keys in Settings for every source that takes a key, including OpenAlex, PubMed, and Semantic Scholar. arXiv, Crossref, and PaperHub need no key. A model key is not a data-source key.

## Configure and test a source

Open **Settings → Data sources** and choose **Configure** or **Configure key** on the source row. Enter the key and options, then click **Save**.

**Test connection** inside this dialog also saves the current key and options before testing. If saving fails, the test does not start. Once saving succeeds, a failed test or subsequently cancelling the dialog does not undo it; reopen the source configuration to change or clear the saved key. The table's **Test** and **Test all** use the configured credentials without saving a form draft. Model connection draft tests do not save; see [models and ranking](models.md).

## Obtain credentials

- [OpenAlex API settings](https://openalex.org/settings/api).
- [Semantic Scholar key request](https://www.semanticscholar.org/product/api#api-key-form).
- [NCBI API guidance](https://www.ncbi.nlm.nih.gov/books/NBK25497/).
- [Serper account](https://serper.dev/).
- [WoS Starter application](https://developer.clarivate.com/apis/wos-starter).

Provider subscriptions and rate limits are separate from PaperSeek accounts and credits.

## Empty results and temporary failures

When the primary search returns no papers, PaperSeek makes at most one additional recovery query. It uses source feedback, including PubMed's unrecognized phrases, to adjust terminology or optional restrictions. A separate model check must approve preservation of core concepts, required relationships, and explicit constraints before that query runs. This check is not a guarantee of relevance; inspect the search process and returned papers. If no suitable query passes or the source still has no matches, PaperSeek reports that outcome instead of silently dropping required conditions. Field filters remain applied.

Rate limits and malformed API responses are reported as source problems, not zero matches. Requests use bounded retries and respect `Retry-After`. Within one worker process, requests sharing a source and credential also share pacing and cooldowns; retries participate in the same controls. Semantic Scholar's bulk-search cooldown does not block ordinary relevance search. Existing candidates survive later query or pagination failures, and unavailable supplementary lanes produce warnings. Separate worker processes or other applications using the same provider account still share the upstream quota, so a retry cannot guarantee availability.

## Fields and categories

OpenAlex uses supported discipline options; its semantic lane applies supported filtering behavior. arXiv uses categories. WoS categories and the WoS database selector are separate. Other sources use textual field guidance.

Restricting a field can exclude interdisciplinary work. Start without it unless your research design requires the constraint.

## Missing metadata

Records can lack abstracts, years, citation counts, or open PDF links. Missing metadata is not a quality judgment.

Version 1.0.0-beta2 has no separate cross-source abstract-filling or citation-count enrichment switch. Choose informative abstracts for review writing.

## Multiple keys

OpenAlex and Serper support key pools separated by whitespace, commas, or semicolons. Requests rotate the starting key; authentication failures are handled separately from transient errors.

Rotation does not change provider terms or guarantee remaining credit. See [configuration](../reference/configuration.md).
