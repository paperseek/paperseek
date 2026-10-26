# Bibliometrics

Explore the metadata of a saved search in **Bibliometrics**. An analysis has its own title, history, and exports.

## Create an analysis

Choose a completed search, or use the analysis action from its results. PaperSeek saves an independent snapshot of the compact candidate metadata. Deleting the source search does not delete the analysis.

The input is the **actually retrieved, deduplicated candidate corpus**, including available citation-expansion candidates. It is not the source's reported total, the whole research field, or just the final relevance-ranked list. Search budgets still limit retrieval; LLM scoring still covers only its original subset. Creating an analysis does not score every paper, call a language model, or fetch missing metadata.

Older searches without this corpus artifact can only be analyzed from their saved final results. The interface labels this reduced scope explicitly. Re-running a search is necessary to obtain the broader corpus.

## Views

| View | What it provides |
| --- | --- |
| Overview | Publication years, venues, document types, authors, institutions, countries, citation coverage, and the scored subset |
| Venue rankings | CAS tiers and Top, JCR quartiles, CCF classes, and a table of ranked journals and conferences with their Impact Factor |
| Keywords | Native keywords or source categories, frequencies, and an optional co-occurrence network |
| Paper relations | Bibliographic coupling: papers connected by shared returned references |
| Data quality | Field coverage, missing values, scope, and source limitations |

Venue rankings label the whole analysed corpus, not only scored papers, from the [ShowJCR tables](results.md#journal-rankings-and-conference-acceptance) on this computer when the analysis is created. An analysis created before the tables were downloaded has no ranking labels; create it again from the same search. Only the JCR IF is an Impact Factor.

Missing citation counts remain unknown; an explicitly returned zero remains zero. Author and institution labels are not identity-disambiguated. Country counts describe returned affiliations, not researcher nationality. Keyword provenance matters: an OpenAlex concept or a Semantic Scholar field is not necessarily an author-supplied keyword.

## Generate a network

Opening a network tab does not compute it. Choose thresholds and click **Generate network**. Parameters collapse after submission and can be reopened above the canvas. Search node labels, select a node to inspect its details, and use the group legend to interpret colors. The server caches completed results by parameters; layout runs in a browser worker. A failed layout offers a retry.

Co-occurrence connects terms appearing in the same paper. Bibliographic coupling connects papers sharing references. Network groups are connected components, not validated semantic topics. Neither relationship proves that a paper supports a written claim; use the review's **Citation evidence** for that purpose.

Networks display at most **300 nodes and 2,000 edges**. Sparse computation also has pair-work limits. A dense corpus can require a higher threshold or exclusion of very common references. Display truncation and omitted data are reported. Category summaries retain up to 100 categories; exports preserve the underlying compact corpus.

## History and export

Choose **Bibliometrics** in the left sidebar to see **Analyses**. Click a record to reopen it, use the magnifying glass beside the heading to search, or choose **All** for the full list. Each row in that list has a record menu for rename, export, and delete. Renaming changes the display title, not the source query.

CSV exports one row per corpus paper. JSON includes the corpus and scope metadata. These exports differ from search exports, which contain the final result set. Analysis deletion removes its snapshots and cached networks while leaving the source search and reviews intact.

## Interpret results carefully

This is preliminary bibliometric exploration of one bounded search. Source coverage, missing reference lists, name variants, and query selection affect distributions and networks. It is not a substitute for a reproducible multi-database systematic search or specialist bibliometric validation.
