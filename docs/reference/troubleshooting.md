# Troubleshooting

Find your symptom in the index, check the likely cause, then follow the link to the fix. Start with the visible task state and error message: a slow upstream service, a queued task, and a disconnected browser need different responses.

## Symptom index

| Symptom | Likely cause | Go to |
| --- | --- | --- |
| A search takes a long time | Queued task, large budget, or a slow upstream stage | [Search problems](#a-search-is-taking-a-long-time) |
| HTTP 401 / 403 | Wrong key or missing provider permission | [HTTP errors](#http-401-403-404-or-429) |
| HTTP 404 | Wrong base URL, protocol, route, or model | [HTTP errors](#http-401-403-404-or-429) |
| HTTP 429 | Provider rate limit or insufficient credit | [HTTP errors](#http-401-403-404-or-429) |
| Connection error / timeout | Network reachability or provider availability | [HTTP errors](#http-401-403-404-or-429) |
| Your endpoint is rejected online | Non-public address (localhost, private network) | [My endpoint is rejected online](#my-endpoint-is-rejected-online) |
| No results, missing abstracts, or counts differ | Over-restrictive query or gaps in the source record | [No results](#no-results-missing-abstracts-or-different-counts) |
| The citation map is empty | Source without expansion, or no links within budget | [The citation map is empty](#the-citation-map-is-empty) |
| A review outline or section fails | Model availability or a provider error | [A review outline or section fails](#a-review-outline-or-section-fails) |
| Auto-select by topic returns nothing usable | The writing model returned an invalid list or could not be reached | [Auto-select by topic returns nothing usable](#auto-select-by-topic-returns-nothing-usable) |
| The local service cannot start | Python version, database path, or port conflict | [Local service cannot start](#local-service-cannot-start) |
| Sign-in or email-code problems | Spam folder, expired or replaced code | [Your account](../online/account.md#email-or-password-problems) |

## Search problems

### A search is taking a long time

Check whether the task is **queued** or **running**. Up to four tasks run at once by default; the rest wait in the queue (see [concurrent tasks](configuration.md#concurrent-tasks)). During execution, query refinement, source requests, citation expansion, and model scoring can each take time. Larger budgets make more work possible but do not guarantee more useful results.

If the page loses its connection, reopen the saved run and inspect its status before submitting again. Anonymous online results are temporary; see [Data & privacy](../online/privacy.md#search-without-signing-in).

### No results, missing abstracts, or different counts

Broaden the question, remove overly restrictive fields, or try a more suitable source. Source hits are not the number downloaded, and downloaded candidates are not the final ranked set.

Missing abstracts, citation counts, or PDFs can be a property of the source record. PaperSeek does not independently fill missing abstracts from another source.

### The citation map is empty

Check that the run used a supported citation-expansion source and that expansion was enabled. Even then, the source may provide no usable links within the available budget.

## HTTP and provider errors

### HTTP 401, 403, 404, or 429

| Error | Check |
| --- | --- |
| 401 / 403 | Correct key, provider account permissions, and selected model |
| 404 | API base URL, protocol, route, model availability, or a missing task/resource |
| 429 | Provider rate limit or structured insufficient-credit error |
| Connection / timeout | Network reachability and provider availability; if the service is blocked on your network, set up a [network proxy](configuration.md#network-proxy) |

A 404 displayed during an OpenAlex run does not by itself prove that OpenAlex returned it: model requests and application routes also occur during the task. Record the stage, safe error code, task ID, and time.

PaperSeek Service credits and an upstream provider's balance are different. Increasing one does not change the other.

### My endpoint is rejected online

Online, your own key cannot point to localhost, private networks, and other non-public destinations. Use a public provider URL, or use Community for a model running on your own machine. A valid URL must also implement the chosen API protocol.

## Review problems

### A review outline or section fails

Review writing is experimental and depends on selected evidence and model availability. Select papers with useful abstracts, inspect the current project/job state, and retry the failed work after resolving the reported provider issue. Avoid repeatedly creating duplicate projects.

Longer drafts need more output from the model. A completed outline does not guarantee that the next writing request will succeed. Keep manual edits and inspect versions before regenerating.

When one section fails, the other sections are still written and saved. Retrying writes only the sections that are missing or need revision.

### Auto-select by topic returns nothing usable

The writing model did not return a valid list, or could not be reached. Check the writing model in the setup step, try again, or tick papers yourself. Auto-select only proposes papers; nothing is created until you generate the outline.

## Local service

### Local service cannot start

Run `paperseek doctor --json`. Check Python 3.12+, the selected database path, write permissions, port conflicts, and model settings. Read the launching terminal or service manager's logs; the Web UI intentionally focuses on status rather than raw execution logs.

For separate processes, confirm both API and worker use the same database.

## Report a reproducible issue

Open a [GitHub issue](https://github.com/paperseek/paperseek/issues) with:

- Installed version or online mode, browser/OS, and steps to reproduce.
- Source, protocol, ranking method, and budget.
- Expected behavior, observed error, task ID if appropriate, and approximate time.
- A minimal non-sensitive example and a cropped screenshot if helpful.

Remove API keys, passwords, verification codes, cookies, private queries, and identifying data. Security reports belong in the repository's [security process](https://github.com/paperseek/paperseek/blob/main/SECURITY.md).
