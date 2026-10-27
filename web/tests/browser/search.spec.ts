import { expect, test } from "@playwright/test";
import { createState, ids, requestsTo } from "./mock-backend";
import { open } from "./workspace-helpers";

test("new search defaults come from settings and submit names saved connections only", async ({ page }) => {
  const state = await open(page, "/search");
  await expect(page.getByRole("combobox", { name: "数据源" })).toContainText("OpenAlex");
  await expect(page.getByRole("combobox", { name: "检索预算" })).toContainText("自动");
  await page.getByLabel("研究问题").fill("LLM screening");
  await page.getByRole("button", { name: "开始检索" }).click();
  await expect(page).toHaveURL(/\/runs\/abababab/);
  const body = state.submissions[0];
  expect(body).toMatchObject({ question: "LLM screening", source: "openalex", search_budget: "fast", retrieval: { mode: "local_fusion" }, connections: { llm: ids.llm } });
  expect(body.credentials).toBeUndefined();
  expect(JSON.stringify(body)).not.toMatch(/api_key/);
});

test("only usable sources are offered and required-key sources stay hidden", async ({ page }) => {
  await open(page, "/search");
  await page.getByRole("combobox", { name: "数据源" }).click();
  await expect(page.getByRole("option")).toHaveText(["OpenAlex", "arXiv", "PubMed"]);
});

test("more options count changes, field dialog filters, and reset restores defaults", async ({ page }) => {
  await open(page, "/search");
  await page.getByText("更多选项").click();
  await page.getByTestId("field-selector-toggle").click();
  const dialog = page.getByRole("dialog", { name: "选择研究领域" });
  await dialog.getByRole("searchbox").fill("医学");
  await dialog.getByRole("checkbox", { name: "医学" }).check({ force: true });
  await dialog.getByRole("button", { name: "完成" }).click();
  await expect(page.getByTestId("field-selector-summary")).toHaveText("医学");
  await page.getByRole("switch", { name: "引用扩展" }).uncheck({ force: true });
  await expect(page.getByText("2 项已修改")).toBeVisible();
  await page.getByRole("button", { name: "恢复默认" }).click();
  await expect(page.getByTestId("field-selector-summary")).toHaveText("不限");
  await expect(page.getByRole("switch", { name: "引用扩展" })).toBeChecked();
});

test("unconfigured pre-ranking modes are disabled with a configure entry", async ({ page }) => {
  await open(page, "/search");
  await page.getByText("更多选项").click();
  await page.getByRole("combobox", { name: "候选初排序" }).click();
  await expect(page.getByRole("option", { name: /Embedding \+ Rerank/ })).toHaveAttribute("aria-disabled", "true");
  await expect(page.getByRole("link", { name: "去配置" })).toBeVisible();
});

test("first run without an LLM connection shows setup steps and blocks submission", async ({ page }) => {
  const state = createState();
  state.connections = [];
  state.defaults.search_llm = "";
  await open(page, "/search", state);
  const setup = page.getByRole("region", { name: "开始前，先连接一个模型" });
  await expect(setup).toBeVisible();
  // Source keys are an optional step to configure, not something already done.
  await expect(setup.locator(".step-dot.done")).toHaveCount(0);
  await expect(setup).toContainText("数据源密钥 · 可选");
  await expect(setup.getByRole("link", { name: "配置" }).first()).toHaveAttribute("href", "/settings/sources");
  await page.getByLabel("研究问题").fill("question");
  await expect(page.getByRole("button", { name: "开始检索" })).toBeDisabled();
  await page.getByRole("link", { name: "添加连接" }).click();
  await expect(page.getByRole("dialog", { name: "添加连接" })).toBeVisible();
});

test("an active run is announced with view and stop; Ctrl+Enter submits", async ({ page }) => {
  const state = createState();
  state.runs[ids.running] = { ...state.runs[ids.done], id: ids.running, question: "Running search", status: "running", result_count: 0 };
  await open(page, "/search", state);
  await expect(page.getByText("已有检索正在运行")).toBeVisible();
  await page.getByLabel("研究问题").fill("next question");
  await expect(page.getByRole("button", { name: "开始检索" })).toBeDisabled();
  await page.getByRole("button", { name: "停止" }).click();
  await expect(page.getByText("已有检索正在运行")).toHaveCount(0);
  expect(requestsTo(state, "POST", `/runs/${ids.running}/cancel`)).toHaveLength(1);
  await page.getByLabel("研究问题").press("Control+Enter");
  await expect(page).toHaveURL(/\/runs\/abababab/);
});
