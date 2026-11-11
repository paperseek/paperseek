// Regenerates the README screenshots from the real UI against the in-memory API:
//   node tests/browser/run.mjs --config playwright.docs.config.ts screenshots.spec.ts
// Name the file: scrolling.spec.ts in this folder needs the documentation site instead.
// The papers below are illustrative, not real publications.
import { test } from "@playwright/test";
import { createState, ids, paper } from "../browser/mock-backend";
import { open } from "../browser/workspace-helpers";

const out = "../docs/public/images";
const questions = {
  en: ["LLM screening for systematic reviews", "Long-term stability of perovskite solar cells", "Gut microbiome and depression"],
  zh: ["大语言模型辅助系统综述的文献筛选", "钙钛矿太阳能电池的长期稳定性", "肠道菌群与抑郁症的关联机制"],
};
const papers = [
  ["Large language models for title and abstract screening: a comparative evaluation", "Recall, workload saved and agreement with two human reviewers across twelve systematic reviews."],
  ["Prompt design and calibration for automated citation screening", "How instructions, examples and a held-out calibration set change sensitivity and specificity."],
  ["Workload reduction in systematic reviews with machine-assisted screening", "A prospective study of reviewer time and missed studies when a model ranks abstracts first."],
  ["Reproducibility of model-based eligibility decisions", "Repeated runs, model updates and their effect on which studies are included."],
  ["Active learning versus zero-shot screening for evidence synthesis", "Comparing two common automation strategies on medical and social science reviews."],
  ["Reporting standards for AI-assisted literature reviews", "A checklist for documenting models, prompts and human oversight in published reviews."],
].map(([title, abstract], index) => paper(index + 1, { title, abstract, authors: [["Lin", "Okafor", "Müller", "Silva", "Tanaka", "Haddad"][index] + " A", "Reyes M"],
  reasoning: "Directly evaluates screening accuracy and reviewer workload.", doi: "", url: "" }));

test.use({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });

for (const language of ["en", "zh"] as const) {
  test(`results screenshot (${language})`, async ({ page }) => {
    const state = createState();
    state.results[ids.done] = papers;
    [ids.done, ids.small, ids.failed].forEach((id, index) => { state.runs[id].question = questions[language][index]; });
    await open(page, `/runs/${ids.done}/results`, state, language);
    await page.waitForLoadState("networkidle");
    await page.screenshot({ path: `${out}/workspace-results-${language}.png` });
  });
}
