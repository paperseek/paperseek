import { expect, test } from "@playwright/test";
import { createState, ids, paper, requestsTo, withDocument } from "./mock-backend";
import { open } from "./workspace-helpers";

test("auto-selection finishes away from the review and restores its draft without another request", async ({ page }) => {
  const state = await open(page, `/reviews/new?source=${ids.done}`);
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/reviews/paper-suggestions", async route => { await pending; await route.fallback(); });
  await page.getByLabel("综述主题").fill("Persistent review topic");
  await page.getByRole("radiogroup", { name: "篇幅" }).getByRole("radio", { name: /简短/ }).check({ force: true });
  await page.getByRole("button", { name: "下一步" }).click();
  await page.getByRole("button", { name: "按主题自动挑选" }).click();
  await expect(page.locator(".sb-scroll")).toContainText("挑选中");
  await page.getByRole("navigation", { name: "模块" }).getByRole("link", { name: "文献检索" }).click();
  release();
  await expect.poll(() => requestsTo(state, "POST", "/reviews/paper-suggestions").length).toBe(1);
  await page.getByRole("navigation", { name: "模块" }).getByRole("link", { name: /文献综述/ }).click();
  await expect(page.locator(".sb-scroll")).toContainText("待确认");
  await page.locator(".sb-scroll").getByRole("link", { name: /Persistent review topic/ }).click();
  await expect(page.getByText("Recall · Largest validation study.")).toBeVisible();
  await expect(page.locator(".paper-pick input:checked")).toHaveCount(2);
  await page.getByRole("button", { name: "调整", exact: true }).click();
  await expect(page.getByLabel("综述主题")).toHaveValue("Persistent review topic");
  await expect(page.getByRole("radiogroup", { name: "篇幅" }).getByRole("radio", { name: /简短/ })).toBeChecked();
  await page.getByRole("button", { name: "下一步" }).click();
  await page.getByRole("button", { name: "生成大纲" }).click();
  await expect(page).toHaveURL(/tab=outline/);
  expect(requestsTo(state, "POST", "/reviews/paper-suggestions")).toHaveLength(1);
  expect(requestsTo(state, "POST", "/reviews")[0].body.paper_uids).toEqual(["uid-1", "uid-6"]);
});

test("pending selection can be reopened, stopped and retried; discarding ignores a late result", async ({ page }) => {
  await open(page, `/reviews/new?source=${ids.done}&paper=uid-2`);
  let release!: () => void;
  let requests = 0;
  let delivered = 0;
  const pending = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/reviews/paper-suggestions", async route => {
    requests++;
    await pending;
    await route.fulfill({ json: { items: [{ uid: "uid-1", reason: "Late reason", theme: "" }] } });
    delivered++;
  });
  await page.getByRole("button", { name: "下一步" }).click();
  await page.getByRole("button", { name: "按主题自动挑选" }).click();
  await expect.poll(() => requests).toBe(1);
  const nav = page.getByRole("navigation", { name: "模块" });
  await nav.getByRole("link", { name: "文献检索" }).click();
  await nav.getByRole("link", { name: /文献综述/ }).click();
  await expect(page.getByRole("button", { name: "正在挑选…" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "生成大纲" })).toBeDisabled();
  await page.getByRole("button", { name: "停止等待" }).click();
  await expect(page.locator(".paper-pick input:checked")).toHaveCount(1);
  await page.getByRole("button", { name: "按主题自动挑选" }).click();
  await expect.poll(() => requests).toBe(2);
  await page.getByRole("button", { name: "丢弃草稿" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "丢弃草稿" }).click();
  release();
  await expect.poll(() => delivered).toBe(2);
  await expect(page.locator("[aria-current=step]")).toContainText("选择检索");
  await expect(page.locator(".sb-scroll")).not.toContainText("挑选中");
  await expect(page.getByText("Late reason")).toHaveCount(0);
});

test("failed selection remains retryable and manual picks survive module navigation", async ({ page }) => {
  await open(page, `/reviews/new?source=${ids.done}&paper=uid-2`);
  let attempts = 0;
  await page.route("**/reviews/paper-suggestions", route => ++attempts === 1
    ? route.fulfill({ status: 503, json: { error: { code: "reviewError" } } }) : route.fallback());
  await page.getByRole("button", { name: "下一步" }).click();
  await page.getByRole("button", { name: "按主题自动挑选" }).click();
  await expect(page.locator(".sb-scroll")).toContainText("挑选失败");
  await expect(page.locator(".paper-pick input:checked")).toHaveCount(1);
  await page.getByRole("button", { name: "按主题自动挑选" }).click();
  await expect(page.getByText("Recall · Largest validation study.")).toBeVisible();
  await page.locator(".paper-pick").filter({ hasText: "Paper 1 on screening" }).locator("label.cb").click();
  const nav = page.getByRole("navigation", { name: "模块" });
  await nav.getByRole("link", { name: "文献计量" }).click();
  await nav.getByRole("link", { name: /文献综述/ }).click();
  await expect(page.locator(".paper-pick input:checked")).toHaveCount(1);
  await expect(page.getByText("Workload · Measures time saved.")).toBeVisible();
  expect(attempts).toBe(2);
});

test("a saved review with a failed outline stays in history without blocking a new draft", async ({ page }) => {
  const state = await open(page, `/reviews/new?source=${ids.done}&paper=uid-1&paper=uid-2`);
  await page.route(`**/reviews/${ids.review}/jobs`, route => route.fulfill({ status: 503, json: { error: { code: "reviewError" } } }));
  await page.getByRole("button", { name: "下一步" }).click();
  await page.getByRole("button", { name: "生成大纲" }).click();
  await expect(page.getByRole("link", { name: "打开综述" })).toBeVisible();
  const nav = page.getByRole("navigation", { name: "模块" });
  await nav.getByRole("link", { name: "文献检索" }).click();
  await nav.getByRole("link", { name: /文献综述/ }).click();
  await expect(page.locator("[aria-current=step]")).toContainText("选择检索");
  await expect(page.locator(`.sb-scroll a[href='/reviews/${ids.review}']`)).toBeVisible();
  expect(requestsTo(state, "POST", "/reviews")).toHaveLength(1);
});

test("new review goes search → setup → papers and starts the outline with a saved connection", async ({ page }) => {
  const state = await open(page, "/reviews/new");
  await expect(page.getByRole("radio", { name: /Tiny search/ })).toHaveAttribute("aria-disabled", "true");
  await expect(page.getByRole("radio", { name: /Tiny search/ })).toContainText("结果不足 2 篇");
  await page.getByRole("radio", { name: /LLM screening/ }).click();
  await page.getByRole("button", { name: "下一步" }).click();
  await expect(page.locator("[aria-current=step]")).toContainText("写作设置");
  await expect(page.getByLabel("综述主题")).toHaveValue("LLM screening for systematic reviews");
  await expect(page.getByRole("radiogroup", { name: "篇幅" })).toContainText("20–45 篇");
  await page.getByRole("radiogroup", { name: "篇幅" }).getByRole("radio", { name: /自定义/ }).check({ force: true });
  await page.getByRole("spinbutton", { name: "章节数" }).fill("5");
  await expect(page.getByRole("combobox", { name: "每章段落数" })).toBeHidden();
  await page.getByText("更多选项").click();
  await expect(page.getByRole("combobox", { name: "每章段落数" })).toContainText("自动");
  await page.getByRole("combobox", { name: "每章段落数" }).click();
  await expect(page.getByRole("option", { name: /自动/ })).toContainText("随篇幅 5–7 段");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "下一步" }).click();
  await expect(page.locator("[aria-current=step]")).toContainText("选择文献");
  await expect(page.getByText("建议选 60–120 篇")).toBeVisible();
  const noAbstract = page.locator(".paper-pick.off");
  await expect(noAbstract).toContainText("无摘要");
  await expect(noAbstract.getByRole("checkbox")).toBeDisabled();
  await expect(page.getByRole("button", { name: "生成大纲" })).toBeDisabled();
  await page.getByRole("button", { name: "选择全部有摘要的" }).click();
  await expect(page.locator(".paper-pick input:checked")).toHaveCount(3);
  await page.getByRole("button", { name: "生成大纲" }).click();
  await expect(page).toHaveURL(new RegExp(`/reviews/${ids.review}\\?tab=outline`));
  const body = requestsTo(state, "POST", "/reviews")[0].body;
  expect(body).toMatchObject({ section_count: 5, length: "detailed", paper_uids: ["uid-1", "uid-2", "uid-3"] });
  expect(body).not.toHaveProperty("paragraph_count");
  expect(body).not.toHaveProperty("paragraph_length");
  expect(requestsTo(state, "POST", `/reviews/${ids.review}/jobs`)[0].body).toMatchObject({ action: "prepare_outline", connection_id: ids.llm });
});

test("selection from results lands on the setup step and keeps the papers", async ({ page }) => {
  await open(page, `/reviews/new?source=${ids.done}&paper=uid-1&paper=uid-2`);
  await expect(page.locator("[aria-current=step]")).toContainText("写作设置");
  await page.getByRole("button", { name: "下一步" }).click();
  await expect(page.locator(".paper-pick input:checked")).toHaveCount(2);
});

test("auto-select explains its candidate limit and shows why papers were picked", async ({ page }) => {
  const state = await open(page, `/reviews/new?source=${ids.done}`);
  await page.getByRole("radiogroup", { name: "篇幅" }).getByRole("radio", { name: /简短/ }).check({ force: true });
  await page.getByRole("button", { name: "下一步" }).click();
  await page.getByRole("button", { name: "按主题自动挑选" }).click();
  await expect(page.getByText(/前 256 篇有有效摘要的文献/)).toBeVisible();
  await expect(page.getByText("Recall · Largest validation study.")).toBeVisible();
  expect(requestsTo(state, "POST", "/reviews/paper-suggestions")[0].body).toMatchObject({
    source_run_id: ids.done, topic: "LLM screening for systematic reviews", count: 14, connection_id: ids.llm });
  await page.getByRole("button", { name: "生成大纲" }).click();
  await expect(page).toHaveURL(/tab=outline/);
  expect(requestsTo(state, "POST", "/reviews")[0].body.paper_uids).toEqual(["uid-1", "uid-6"]);
});

test("manual selection accepts 256 papers including later ranks and stops at the limit", async ({ page }) => {
  const state = createState();
  state.results[ids.done] = Array.from({ length: 257 }, (_, i) => paper(i + 1));
  await page.setViewportSize({ width: 390, height: 844 });
  await open(page, `/reviews/new?source=${ids.done}`, state);
  await page.getByRole("button", { name: "下一步" }).click();
  await page.getByRole("button", { name: "加载更多" }).click();
  await page.locator(".paper-pick").last().locator("label.cb").click();
  await page.getByRole("button", { name: "选择全部有摘要的" }).click();
  await expect(page.getByText("已选 256 / 256 篇")).toBeVisible();
  await expect(page.locator(".paper-pick").nth(255).getByRole("checkbox")).toBeDisabled();
  await expect(page.locator(".paper-pick").last().getByRole("checkbox")).toBeChecked();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByRole("button", { name: "生成大纲" }).click();
  await expect(page).toHaveURL(/tab=outline/);
  const picked = requestsTo(state, "POST", "/reviews")[0].body.paper_uids;
  expect(picked).toHaveLength(256);
  expect(picked).toContain("uid-257");
});

test("paragraph count and length offer presets and custom values in the output language's unit", async ({ page }) => {
  const state = createState();
  await open(page, `/reviews/new?source=${ids.done}&paper=uid-1&paper=uid-2`, state);
  await page.getByText("更多选项").click();
  await page.getByRole("combobox", { name: "每章段落数" }).click();
  await page.getByRole("option", { name: /充实/ }).click();
  await page.getByRole("combobox", { name: "每段长度" }).click();
  await expect(page.getByRole("option", { name: /充实/ })).toContainText("约 450 字");
  await page.getByRole("option", { name: /自定义/ }).click();
  await page.getByRole("spinbutton", { name: "每段长度" }).fill("320");
  await expect(page.locator("summary")).toContainText("2 项已修改");
  await page.getByRole("button", { name: "下一步" }).click();
  await page.getByRole("button", { name: "生成大纲" }).click();
  await expect(page).toHaveURL(/tab=outline/);
  expect(requestsTo(state, "POST", "/reviews")[0].body).toMatchObject({ paragraph_count: 5, paragraph_length: 320 });
});

test("paragraph length switches to words for English output", async ({ page }) => {
  await open(page, `/reviews/new?source=${ids.done}&paper=uid-1&paper=uid-2`);
  await page.getByRole("combobox", { name: "输出语言" }).click();
  await page.getByRole("option", { name: "English" }).click();
  await page.getByText("更多选项").click();
  await page.getByRole("combobox", { name: "每段长度" }).click();
  await expect(page.getByRole("option", { name: /充实/ })).toContainText("约 250 词");
  await page.getByRole("option", { name: /自定义/ }).click();
  await expect(page.getByRole("spinbutton", { name: "每段长度" })).toHaveValue("150");
});

test("outline edits show the unsaved bar and save through the outline endpoint", async ({ page }) => {
  const state = createState();
  await open(page, `/reviews/new?source=${ids.done}&paper=uid-1&paper=uid-2`, state);
  await page.getByRole("button", { name: "下一步" }).click();
  await page.getByRole("button", { name: "生成大纲" }).click();
  await expect(page.getByLabel("综述主线")).toHaveValue("Compare recall and workload.");
  await page.getByRole("button", { name: /Reproducibility/ }).click();
  await page.getByLabel("章节标题").fill("Bias and reporting");
  const bar = page.getByRole("region", { name: "大纲有未保存的修改" });
  await expect(bar).toBeVisible();
  await bar.getByRole("button", { name: "保存" }).click();
  await expect(bar).toBeHidden();
  expect(requestsTo(state, "PUT", `/reviews/${ids.review}/outline`)[0].body.outline.sections[1].title).toBe("Bias and reporting");
  // A paper removed from every section is saved as left out, with a reason.
  for (const name of ["Screening performance", "Bias and reporting"]) {
    await page.getByRole("button", { name: new RegExp(name) }).click();
    await page.getByRole("button", { name: /删除 \[2\]/ }).first().click();
  }
  await expect(page.getByText("未纳入大纲的文献 · 1 篇")).toBeVisible();
  await bar.getByRole("button", { name: "保存" }).click();
  await expect(bar).toBeHidden();
  expect(requestsTo(state, "PUT", `/reviews/${ids.review}/outline`)[1].body.outline.excluded).toEqual([{ paper_id: "p2", reason: "已在大纲中手动移除。" }]);
});

test("draft shows the table of contents and an evidence popover; evidence tab switches views", async ({ page }) => {
  const state = createState();
  await open(page, `/reviews/new?source=${ids.done}&paper=uid-1&paper=uid-2`, state);
  await page.getByRole("button", { name: "下一步" }).click();
  await page.getByRole("button", { name: "生成大纲" }).click();
  await expect(page).toHaveURL(/tab=outline/);
  withDocument(state.reviews[0]);
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  await page.route(url => url.pathname === `/api/v1/reviews/${ids.review}`, async route => {
    await pending;
    await route.fallback();
  });
  await page.goto(`/reviews/${ids.review}?tab=document`);
  const loading = page.getByTestId("review-loading");
  await expect(loading).toBeVisible();
  await expect(page.getByRole("button", { name: "生成大纲", exact: true })).toHaveCount(0);
  await page.setViewportSize({ width: 375, height: 812 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  release();
  await expect(loading).toHaveCount(0);
  await page.setViewportSize({ width: 1280, height: 900 });
  await expect(page.getByRole("navigation", { name: "大纲" })).toContainText("Screening performance");
  await page.getByRole("button", { name: "证据 [1]" }).first().click();
  const popover = page.getByRole("dialog", { name: "证据 [1]" });
  await expect(popover).toContainText("Quote from Paper 1 on screening");
  await page.keyboard.press("Escape");
  await expect(popover).toBeHidden();
  const first = page.locator("#review-s1");
  await expect(first.getByText("未对应到具体论断的引用：")).toContainText("2");
  await first.getByText("未引用的计划文献 · 1 篇").click();
  await expect(first.getByText("理由可能不成立：Its abstract reports recall.")).toBeVisible();
  state.reviews[0].document.sections[0].omitted[0] = {
    ...state.reviews[0].document.sections[0].omitted[0], questioned: false, check_incomplete: true, check_reason: "",
  };
  await page.reload();
  await first.getByText("未引用的计划文献 · 1 篇").click();
  await expect(first.getByText("排除理由检查未完成，请人工核对。")).toBeVisible();
  await expect(first.getByText(/理由可能不成立/)).toBeHidden();
  await page.getByRole("tab", { name: "引用证据" }).click();
  await page.getByRole("radio", { name: "论文引用关系" }).check({ force: true });
  await expect(page.getByTestId("citation-graph")).toBeVisible();
});

test("citation map selects nodes from the graph and the text list", async ({ page }) => {
  await open(page, `/runs/${ids.done}/citation-map`);
  await expect(page.getByRole("list", { name: "引用图例" })).toContainText("种子");
  await expect(page.getByTestId("citation-detail")).toContainText("Seed paper");
  await page.getByTestId("citation-text-list").locator("summary").click();
  await page.getByTestId("citation-text-list").getByRole("button", { name: /Result paper/ }).click();
  await expect(page.getByTestId("citation-detail")).toContainText("Result paper");
  await expect(page.getByTestId("citation-detail-score")).toHaveText("7/10");
});

test("a failed writing task offers a retry that resubmits the same action", async ({ page }) => {
  const state = createState();
  await open(page, `/reviews/new?source=${ids.done}&paper=uid-1&paper=uid-2`, state);
  await page.getByRole("button", { name: "下一步" }).click();
  await page.getByRole("button", { name: "生成大纲" }).click();
  await expect(page).toHaveURL(/tab=outline/);
  const review = state.reviews[0];
  review.latest_job = { ...review.latest_job, status: "failed", error_code: "review_model_output_invalid" };
  review.latest_request = { action: "write", section_id: "", instruction: "" };
  review.generation_failure = { task: "section_sources", reason: "Paragraph 5: synthesize within 900 characters." };
  await page.reload();
  const alert = page.getByRole("alert").filter({ hasText: "返回内容经自动修复后仍未通过格式校验" });
  await expect(alert).toContainText("段落引用");
  await alert.getByRole("button", { name: "重试" }).click();
  await expect.poll(() => requestsTo(state, "POST", `/reviews/${ids.review}/jobs`).at(-1)?.body).toMatchObject({ action: "write", connection_id: ids.llm });
});
