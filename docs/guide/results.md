# Results and history

Inspect your reading list, select what matters, and export it into your own research workflow.

## Three independent histories

Search runs, analyses, and reviews are saved separately — each module in the sidebar shows its own history. Where history lives, how records are renamed, exported, or deleted, and what survives a reload: [Workspace and history](workspace.md).

## Read a paper card

Results can include titles, authors, years, venues, abstracts, source links, identifiers, citation information, and relevance explanations. Availability depends on the source.

Relevance measures the match to **your question**. It is not a study-quality score or permission to cite a paper without reading it.

## Journal rankings and conference acceptance

Cards can carry two kinds of venue labels. Open **Details** for the full entry.

- **Journal and venue rankings**: CAS tier and Top (中科院分区), JCR quartile and Impact Factor, and the CCF class (A, B or C) of recommended conferences and journals, from [ShowJCR](https://github.com/hitfyd/ShowJCR). CAS and JCR are matched by ISSN, otherwise by the exact journal title; Details says when the title was used. CCF is matched by the venue's full name or its abbreviation, such as NeurIPS; workshops get no CCF class. Only the JCR figure is an Impact Factor; CAS tiers, JCR quartiles and CCF classes are rankings.
- **Open impact**: a journal's 2-year mean citedness and h-index from OpenAlex. It needs no import and is not an Impact Factor.
- **Conference acceptance**: venue, year and Oral, Spotlight or Poster, from the PaperHub collection and OpenReview. Only papers a main conference track, NeurIPS Datasets & Benchmarks or TMLR accepted count; workshop, rejected, withdrawn and under-review submissions do not.

Labels are added when a search finishes. The ShowJCR tables download from GitHub on the first search and stay on this computer; **Settings → Data sources → Journal rankings** shows their editions and **Update from ShowJCR** fetches them again. The network proxy applies to this download. Where GitHub is unreachable, download the same file from ShowJCR (`FQBJCR2025-UTF8.csv`, `JCR2025-UTF8.csv`, `CCF2026-UTF8.csv`) and use **Import local file**. Run a search again to label it with a new edition.

## Filter, sort, and select

Result controls search the returned set, apply available filters, and change the order. They do not issue a fresh external search.

**Journal** keeps papers in a CAS tier, CAS Top, a JCR quartile range, or a CCF class range (A, A–B, A–C). **Accepted** keeps papers with conference acceptance, Oral only, or Oral and Spotlight. **By impact factor** sorts by JCR Impact Factor, then by OpenAlex open impact. Papers without a label are left out by these filters, not guessed.

Select individual papers for a smaller export or a review, and check the selected count before continuing. **Select loaded** applies only to the currently loaded papers. Review creation requires 2–256 selected papers. Switching modules preserves the selection during this session.

## Export

| Format | Use |
| --- | --- |
| CSV | Spreadsheets and manual screening |
| JSON | Structured downstream processing, including journal and acceptance labels |
| BibTeX | Zotero, JabRef, LaTeX |
| RIS | Zotero, EndNote, Mendeley |
| Selected (any format) | Only the selected papers |

The **Export** menu groups complete and selected downloads. BibTeX and RIS mark journal articles, conference papers and preprints with their own entry types, carry volume, issue, pages and ISSN when the source provides them, and put conference acceptance (for example "ICLR 2025 Oral") in the note field. Journal rankings stay out of these files. A complete export covers the stored results, not just the visible page. Original-record links take you to the source; PaperSeek does not bulk-download paywalled text.

[Review exports](reviews.md) use Markdown, Word, and BibTeX.

## Reopen history

Community history belongs to the local database. Online history belongs to your account. [Signing in after an anonymous search](../online/privacy.md#search-without-signing-in) does not backfill it.

A saved run retains its actual settings. Reopening it does not replace its model or budget with current defaults; older unrecorded fields remain unknown.

You can delete finished history items after exporting anything you need. Active tasks must end first.

## Start a review

Select at least two papers with useful abstracts, or create a review through **Reviews**. Follow the [outline and writing guide](reviews.md).
