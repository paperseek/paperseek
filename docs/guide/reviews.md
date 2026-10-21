# Literature reviews

**Experimental.** Turn selected abstract evidence into a thematic outline and an editable draft with citations.

The feature uses available papers and abstracts, not automatically read full-text PDFs. Check the draft against original research.

## Module and evidence

Review contains Papers, Outline, Draft, and Citation evidence. Expand the model controls within the review page to configure its model; it cannot change during generation. Its sidebar lists reviews only. Citation evidence brings together paragraph citations and paper relations. New reviews preserve available source-map snapshots; older reviews may still depend on the source search.

## 1. Choose a search

Start from selected results, or choose **Reviews → New review** and an existing search.

Online reviews require login and a saved source search; Community stores them locally.

## 2. Set the scope

Provide a topic and select English or Chinese, a writing model, and Short, Standard, or Detailed.

Default chapter counts are **1, 3, and 6**, with a custom range of **1–14**. This is not a fixed word-count guarantee; the evidence and outline determine what can be written.

Open **More options** to choose the paragraph count per chapter and the target paragraph length. Automatic paragraph counts follow the selected scope; custom counts allow **1–10 paragraphs per chapter**. For Chinese output, custom length is **100–1,500 characters** per paragraph; for English, **50–800 words**. These are writing targets, so check the generated outline and draft.

Configure a writing model in Community or with your own key online, or choose an allowed model in PaperSeek Service.

## 3. Select papers

The length sets a suggested number of papers: about **8–20** for Short, **20–45** for Standard, and **40–80** for Detailed. Custom chapters or paragraphs are estimated from the paragraph count. Manual selection can use the whole search, up to **256 papers**; this is a limit, not a target.

Tick papers yourself, or choose **Auto-select by topic**: the writing model considers the first **256 papers with usable abstracts in search-rank order**, picks papers that cover distinct themes, methods, and study populations, and gives a reason for each. You can still add or remove papers, including manually adding papers ranked beyond that range. Auto-select does not count as a writing task and uses no credits with PaperSeek Service.

Only papers with abstracts can be selected. More papers do not automatically mean better evidence. Records without meaningful research content may not contribute to the draft.

You can switch modules while auto-selection runs. Return to Reviews and open the **Selecting / Ready to confirm** draft in the sidebar to recover the settings, selected papers, and reasons. This tab keeps one pending draft; refreshing, closing the page, or signing out clears it. **Stop waiting** ignores any later selection result but may not stop computation already running at the model provider. **Discard draft** clears the draft.

## 4. Check the outline

Review the themes, chapter structure, paragraph plans, and assigned papers. Look for omitted perspectives and a scope broader than the evidence supports.

Each generated paragraph plan has at most **10 papers**. Papers the outline does not use are listed under **Papers left out of the outline**, each with a reason. To bring one back, add it to a chapter's supporting papers; it then leaves the excluded list. A paper you remove from every chapter is recorded as removed by you.

Use **Save outline** to save edits without starting a writing task. Use **Confirm outline and write** or **Save outline and write** when ready to generate the draft. Leaving with unsaved edits prompts you to keep editing or discard them.

## 5. Read and revise

Edit text, rewrite an individual section, and inspect available versions. A changed outline does not automatically make the old draft match it; check the current version and task state.

Choose numbered or Harvard author–year citation display. Exports follow that choice.

While writing, the model may leave out a planned paper if it gives a reason, for example that the paper does not address the paragraph's comparison or repeats another study. **Planned papers not cited** at the end of each chapter lists these papers and reasons. When the evidence check finds a reason doubtful, it is marked with an explanation.

If an omission reason could not be checked, the draft is kept and that reason is marked as unchecked for manual review. An unfinished check does not mean the reason was accepted.

Papers a paragraph cites without placing them beside a specific claim are listed under the paragraph as **Also cited, not placed beside a claim**. Exports keep this line.

Evidence checks can flag missing or inconsistent support. They do not certify scientific truth. Read cited papers before relying on the claims.

Evidence quotations retain the source abstract's wording and language, even when the draft uses another language. A quotation may be longer when a shorter supporting passage cannot be reliably identified. Check that it supports the stated observation; text that cannot be verified against the abstract is not usable evidence.

## 6. Export

- **Markdown** for further editing.
- **Word (.docx)** for document workflows.
- **BibTeX** for the references.

## If generation stops

Inspect the current or latest task separately from the saved draft. A stopped task does not erase the outline or completed chapters. Model access, credit, timeouts, and structured-response errors can interrupt an outline or section. Fix the cause and retry the affected operation instead of creating duplicate reviews.

Refreshing reconnects to saved state; it does not restart generation. Online writing shares the [execution queue](../online/index.md#when-your-task-is-queued) with searches.
