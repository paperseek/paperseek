import { expect, test } from "@playwright/test";
import { ids, requestsTo } from "./mock-backend";
import { open } from "./workspace-helpers";

const url = `/runs/${ids.done}/results`;

test("result cards show score badges, DOI and PDF, and details open in a dialog", async ({ page }) => {
  await open(page, url);
  const cards = page.getByTestId("result-card");
  await expect(cards).toHaveCount(4);
  await expect(cards.first().getByTestId("result-score")).toHaveText("9/10");
  await expect(cards.nth(3).getByRole("link", { name: "PDF" })).toBeVisible();
  await expect(cards.nth(3)).toContainText("未提供摘要。");
  await expect(page.getByTestId("results-summary")).toContainText("共 6 篇");
  await cards.first().getByRole("button", { name: /更多操作/ }).click();
  await page.getByRole("menuitem", { name: "详细书目信息" }).click();
  const dialog = page.getByRole("dialog", { name: "Paper 1 on screening" });
  await expect(dialog).toContainText("10.1000/1");
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
});

test("filters and sort reload page one from the server and live in the URL", async ({ page }) => {
  const state = await open(page, url);
  await page.getByRole("combobox", { name: "分数" }).click();
  await page.getByRole("option", { name: "≥ 9 分" }).click();
  await expect(page).toHaveURL(/score=9/);
  await page.getByRole("combobox", { name: "结果排序" }).click();
  await page.getByRole("option", { name: "按年份" }).click();
  await expect(page).toHaveURL(/sort=year/);
  const last = requestsTo(state, "GET", `/runs/${ids.done}/results`).at(-1)!;
  expect(last.query.get("min_score")).toBe("9");
  expect(last.query.get("sort")).toBe("year");
  await page.reload();
  await expect(page.getByRole("combobox", { name: "分数" })).toContainText("≥ 9 分");
  await page.getByRole("searchbox", { name: "在结果中搜索" }).fill("nothing matches");
  await expect(page.getByTestId("no-filtered-results")).toBeVisible();
  await page.getByRole("button", { name: "清除筛选" }).click();
  await expect(page.getByTestId("result-card").first()).toBeVisible();
});

test("selection uses a tri-state checkbox and the floating bar exports and creates a review", async ({ page }) => {
  const state = await open(page, url);
  const all = page.getByRole("checkbox", { name: "全选已加载" });
  await all.check({ force: true });
  await page.getByRole("button", { name: "清空选择" }).click();
  await expect(page.getByRole("region", { name: /已选择/ })).toHaveCount(0);
  await page.getByTestId("result-card").first().getByRole("checkbox").check({ force: true });
  await expect(all).toHaveAttribute("aria-checked", "mixed");
  const bar = page.getByRole("region", { name: "已选择 1 篇" });
  await expect(bar.getByRole("button", { name: "用所选创建综述" })).toBeDisabled();
  await all.check({ force: true });
  await expect(page.getByTestId("selection-count")).toHaveText("已选择 4 篇");
  await page.getByRole("button", { name: "导出所选" }).click();
  await page.getByTestId("export-selected-csv").click();
  await expect.poll(() => requestsTo(state, "POST", `/runs/${ids.done}/export-selected`).length).toBe(1);
  expect(requestsTo(state, "POST", `/runs/${ids.done}/export-selected`)[0].body).toEqual({ format: "csv", uids: ["uid-1", "uid-2", "uid-3", "uid-4"] });
  await page.getByRole("button", { name: "加载更多" }).click();
  await expect(page.getByTestId("result-card")).toHaveCount(6);
  await page.getByRole("button", { name: "用所选创建综述" }).click();
  await expect(page).toHaveURL(/\/reviews\/new\?source=.*paper=uid-1/);
});
