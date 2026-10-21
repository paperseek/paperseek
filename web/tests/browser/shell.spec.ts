import { expect, test } from "@playwright/test";
import { createState, ids } from "./mock-backend";
import { noHorizontalOverflow, open } from "./workspace-helpers";

test("module entries open create pages and 'All' opens each record list", async ({ page }) => {
  await open(page, "/");
  await expect(page).toHaveURL(/\/search$/);
  const nav = page.getByRole("navigation", { name: "模块" });
  await nav.getByRole("link", { name: /文献计量/ }).click();
  await expect(page).toHaveURL(/\/bibliometrics$/);
  await expect(page.getByRole("heading", { name: "新建分析", exact: true })).toBeVisible();
  await nav.getByRole("link", { name: /文献综述/ }).click();
  await expect(page).toHaveURL(/\/reviews\/new$/);
  await expect(page.locator(".sb-new")).toContainText("新建综述");
  await page.locator(".sb-label").getByRole("link", { name: "全部" }).click();
  await expect(page).toHaveURL(/\/reviews$/);
  await nav.getByRole("link", { name: /文献检索/ }).click();
  await page.locator(".sb-label").getByRole("link", { name: "全部" }).click();
  await expect(page).toHaveURL(/\/history$/);
  await expect(page.locator(".hdr h1")).toHaveText("文献检索");
});

test("breadcrumb root returns to the module list and the sidebar lists recent searches", async ({ page }) => {
  await open(page, `/runs/${ids.done}/results`);
  await expect(page.locator(".hdr h1")).toContainText("LLM screening for systematic reviews");
  const record = page.locator(".sb-scroll .sb-btn", { hasText: "LLM screening for systematic reviews" });
  await expect(record).toHaveAttribute("aria-current", "page");
  await expect(record.locator(".sb-badge")).toHaveText("6");
  await expect(page.locator(".sb-scroll .sb-btn", { hasText: "Failed search" }).locator(".sb-badge")).toHaveText("失败");
  await page.locator(".hdr .crumb").click();
  await expect(page).toHaveURL(/\/history$/);
});

test("sidebar record search filters on the server", async ({ page }) => {
  const state = await open(page, "/search");
  await page.getByRole("button", { name: "搜索记录" }).click();
  await page.getByRole("searchbox", { name: "搜索记录" }).fill("Tiny");
  await expect(page.locator(".sb-scroll .sb-btn")).toHaveCount(1);
  expect(state.requests.some((item) => item.path === "/history" && item.query.get("query") === "Tiny")).toBe(true);
});

test("collapsing the sidebar persists and the language menu switches the interface", async ({ page }) => {
  await open(page, "/search");
  await page.getByRole("button", { name: "收起侧栏" }).click();
  await expect(page.locator(".sb")).toBeHidden();
  await page.reload();
  await expect(page.locator(".sb")).toBeHidden();
  await page.getByRole("button", { name: "展开侧栏" }).click();
  await page.getByRole("button", { name: "界面语言" }).click();
  await page.getByRole("menuitemradio", { name: "English" }).click();
  await expect(page.getByRole("heading", { name: "New search", exact: true })).toBeVisible();
});

test("skip link is the first tab stop", async ({ page }) => {
  await open(page, "/search");
  await page.keyboard.press("Tab");
  await expect(page.locator(".skip-link")).toBeFocused();
});

for (const width of [390, 1440]) {
  test(`pages fit without horizontal scrolling at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await open(page, "/search");
    const github = page.getByRole("link", { name: "GitHub 社区仓库" });
    await expect(github).toHaveAttribute("href", "https://github.com/paperseek/paperseek");
    await expect(github).toHaveAttribute("target", "_blank");
    expect((await github.boundingBox())!.x).toBeLessThan((await page.getByRole("button", { name: "界面语言" }).boundingBox())!.x);
    expect(await noHorizontalOverflow(page)).toBe(true);
    await page.goto(`/runs/${ids.done}/results`);
    await expect(page.getByTestId("result-card").first()).toBeVisible();
    expect(await noHorizontalOverflow(page)).toBe(true);
    await page.goto("/settings/models");
    await expect(page.locator(".table-wrap")).toBeVisible();
    expect(await noHorizontalOverflow(page)).toBe(true);
    if (width < 900) {
      await expect(page.locator(".app > .sb")).toHaveCount(0);
      await page.getByRole("button", { name: "展开侧栏" }).click();
      await expect(page.locator(".sb-sheet .sb")).toBeVisible();
      await page.locator(".sb-sheet").getByRole("link", { name: /文献计量/ }).click();
      await expect(page.locator(".sb-sheet")).toHaveCount(0);
      await expect(page).toHaveURL(/\/bibliometrics$/);
    }
  });
}

test("a long sidebar history scrolls instead of squeezing its rows", async ({ page }) => {
  const state = createState();
  const template = state.runs[ids.done];
  for (let index = 0; index < 40; index++) {
    const id = `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`;
    state.runs[id] = { ...template, id, question: `Search ${index}`, display_title: `Search ${index}` };
  }
  await page.setViewportSize({ width: 1280, height: 720 });
  await open(page, "/search", state);
  const rows = page.locator(".sb-scroll .sb-btn");
  await expect(rows.nth(20)).toBeAttached();
  const heights = await rows.evaluateAll((items) => items.map((item) => item.getBoundingClientRect().height));
  expect(Math.min(...heights)).toBeGreaterThanOrEqual(28);
  expect(await page.locator(".sb-scroll").evaluate((list) => list.scrollHeight > list.clientHeight)).toBe(true);
});
