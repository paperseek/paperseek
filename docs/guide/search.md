# Search the literature

Describe what you want to understand. PaperSeek prepares source-specific queries and shows how the search develops.

## Workspace navigation

Search settings, Run progress, and Results are separate views within Search. Model defaults live in the standalone settings page, opened from the sidebar footer; source, scope, and budget remain in the draft. Stop is available in the run header across views. Switching views does not restart the run.

## Write a focused question

Name the subject and relationship you care about. Add a method, population, period, or context when essential.

**Broad:** “Open-source software.”  
**Focused:** “How do funding mechanisms affect the long-term maintenance of open-source software?”

Use English or Chinese natural language; no hand-written Boolean query is required. The question field accepts up to 10,000 characters.

## Choose the source and scope

A run starts from **one selected source**. Multiple retrieval paths do not mean that all eight sources are queried in the same run.

OpenAlex, arXiv, and WoS offer source-specific field filters. Other sources use a research-field hint while planning queries. A hint is not a database-level hard filter.

OpenAlex citation expansion adds references and citing works within a budget. It introduces additional requests and can take longer.

## Set a budget

| Interface | API / CLI | Use |
| --- | --- | --- |
| Auto | — | Let PaperSeek choose the search budget |
| Fast | `fast` | Smaller retrieval and scoring budget |
| Balanced | `medium` | Broader coverage and ranking |
| Deep | `high` | More candidates and citation exploration |
| Maximum | `max` | Largest bounded search budget |

These are work limits, not promises of a paper count or completion time. Larger budgets can increase latency and cost. A focused question may help more than choosing Maximum.

## Follow the four stages

1. **Intent and queries:** understand the scope and prepare the strategy.
2. **Query iteration:** test and refine queries using source feedback.
3. **Retrieval and ranking:** collect candidates, optionally expand citations, apply preliminary ranking, and evaluate relevance. The model scores the candidates in batches of 64 and then writes a “Why it ranks” reason for every well-scoring paper in groups of 32; up to eight batches or groups run at once, and each has its own progress row.
4. **Results:** prepare the reading list and exports.

The UI shows stage status and details, rather than a raw terminal log.

### Understand the counts

A source may report thousands of hits while only one page has been downloaded. **Source hits**, **retrieved candidates**, and **final results** describe different sets.

PaperSeek can narrow an overly broad query before fetching a candidate pool. Repeated or unproductive rewrites stop, and selection can return to a better recorded query rather than automatically using the last rewrite. Candidate pools remain bounded; the output is not an exhaustive corpus.

## Stop or reconnect

Use **Stop** to request cancellation. **Cancelling** can remain briefly while in-flight work finishes.

Refreshing a persisted run reconnects to saved state. A network reconnect is not a new search. Retrying a finished or cancelled run starts new work; it does not reopen the old execution.

Online queueing is covered in the [online overview](../online/index.md#when-your-task-is-queued); anonymous sessions in [Data & privacy](../online/privacy.md#search-without-signing-in).
