import { expect, test } from "@playwright/test";
import { createState, event, ids, requestsTo } from "./mock-backend";
import { open } from "./workspace-helpers";

test("a completed run shows four stages, metrics and the results entry", async ({ page }) => {
  await open(page, `/runs/${ids.done}`);
  await expect(page.getByRole("tab", { name: "运行过程" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("tab", { name: /检索结果/ })).toContainText("6");
  for (const key of ["query", "search", "ranking", "results"]) await expect(page.getByTestId(`stage-${key}`)).toHaveAttribute("data-state", "complete");
  await expect(page.getByTestId("stage-query")).toContainText("Compare LLM screening with humans");
  await page.getByTestId("stage-ranking-toggle").click();
  await expect(page.getByTestId("stage-ranking-audit")).toContainText("融合后候选");
  await page.getByTestId("stage-results").getByRole("link", { name: "查看结果" }).click();
  await expect(page).toHaveURL(new RegExp(`/runs/${ids.done}/results`));
});

test("a running run expands the active stage, disables results and confirms before stopping", async ({ page }) => {
  const state = createState();
  state.runs[ids.running] = { ...state.runs[ids.done], id: ids.running, status: "running", result_count: 0, last_event_sequence: 4 };
  state.events[ids.running] = [
    event(ids.running, 1, "query.started"),
    event(ids.running, 2, "query.planned", { intent: "Intent" }),
    event(ids.running, 3, "query.progress", { iteration: 1, decision: "accept", hit_count: 3, total: 9 }),
    event(ids.running, 4, "retrieval.progress", { candidate_count: 18, completed_lanes: 1, total_lanes: 2 }),
  ];
  await open(page, `/runs/${ids.running}`, state);
  await expect(page.getByTestId("stage-ranking")).toHaveAttribute("open", "");
  await expect(page.getByTestId("stage-ranking")).toContainText("进行中");
  await expect(page.getByRole("tab", { name: "检索结果" })).toHaveAttribute("aria-disabled", "true");
  await page.getByRole("button", { name: "停止" }).click();
  const dialog = page.getByRole("dialog", { name: "停止这次检索？" });
  await expect(dialog).toContainText("已完成的阶段会保留");
  await dialog.getByRole("button", { name: "停止" }).click();
  await expect.poll(() => requestsTo(state, "POST", `/runs/${ids.running}/cancel`).length).toBe(1);
});

test("a queued run shows queue position and cancels after confirmation", async ({ page }) => {
  const state = createState();
  state.runs[ids.queued] = { ...state.runs[ids.done], id: ids.queued, status: "queued", result_count: 0, queue: { ahead: 2, running: 1 } };
  await open(page, `/runs/${ids.queued}`, state);
  const card = page.getByTestId("queue-status");
  await expect(card).toContainText("正在等待执行");
  await expect(card).toContainText("2");
  await card.getByRole("button", { name: "取消排队" }).click();
  await page.getByRole("dialog", { name: "取消排队？" }).getByRole("button", { name: "取消排队" }).click();
  await expect.poll(() => requestsTo(state, "POST", `/runs/${ids.queued}/cancel`).length).toBe(1);
});

test("a failed run explains the error and run settings are read-only with rerun", async ({ page }) => {
  await open(page, `/runs/${ids.failed}`);
  await expect(page.getByTestId("run-primary-summary")).toContainText("检索失败");
  await page.getByRole("button", { name: "检索设置" }).click();
  const dialog = page.getByRole("dialog", { name: "检索设置" });
  await expect(dialog).toContainText("Failed search");
  await expect(dialog).toContainText("deepseek-chat");
  await dialog.getByTestId("rerun-edit").click();
  await expect(page).toHaveURL(/\/search$/);
  await expect(page.getByLabel("研究问题")).toHaveValue("Failed search");
});

test("a failed stage retries only that step and the run resumes", async ({ page }) => {
  const state = await open(page, `/runs/${ids.failed}`);
  const stage = page.getByTestId("stage-query");
  await expect(stage).toHaveAttribute("data-state", "failed");
  const retry = page.getByTestId("stage-query-retry");
  await expect(retry).toContainText("从「检索意图」重跑");
  await expect(retry).toHaveAttribute("title", /检索意图/);
  await retry.click();
  await expect.poll(() => requestsTo(state, "POST", `/runs/${ids.failed}/steps/query/retry`).length).toBe(1);
  await expect.poll(() => state.retries.map((item) => item.step)).toEqual(["query"]);
  await expect(stage).toHaveAttribute("data-state", "active");
});

test("a degraded sub-step retries its own pipeline boundary", async ({ page }) => {
  const state = createState();
  Object.assign(state.runs[ids.done], { status: "completed_with_warnings", warnings: ["ranking_degraded"], last_event_sequence: 7 });
  state.events[ids.done] = [
    event(ids.done, 1, "query.started", {}),
    event(ids.done, 2, "query.planned", { intent: "Intent" }),
    event(ids.done, 3, "retrieval.completed", { candidate_count: 24, total_candidates: 31 }),
    event(ids.done, 4, "selection.started", { candidate_count: 24 }),
    event(ids.done, 5, "selection.degraded", { code: "decision_degraded" }),
    event(ids.done, 6, "ranking.completed", { candidate_count: 24, scored_count: 24 }),
    event(ids.done, 7, "workflow.completed", { result_count: 6 }),
  ];
  await open(page, `/runs/${ids.done}`, state);
  await page.getByTestId("stage-ranking-toggle").click();
  const retry = page.getByTestId("stepCandidateSelection-retry");
  await expect(retry).toBeVisible();
  await expect(retry).toContainText("从「候选筛选」重跑");
  await retry.click();
  await expect.poll(() => state.retries.map((item) => item.step)).toEqual(["selection"]);
});

test("a retry shows which boundary is queued and keeps the stages on screen", async ({ page }) => {
  const state = createState();
  state.retryManual = true;
  await open(page, `/runs/${ids.failed}`, state);
  await page.getByTestId("stage-query-retry").click();
  await expect.poll(() => state.retries.map((item) => item.step)).toEqual(["query"]);
  const notice = page.getByTestId("retry-notice");
  await expect(notice).toBeVisible();
  await expect(notice).toContainText("检索意图");
  await expect(page.getByTestId("queue-status")).toHaveCount(0);
  await expect(page.getByTestId("stage-query")).toHaveAttribute("data-state", "idle");
});

test("a degraded retrieval step is degraded and retries its own boundary", async ({ page }) => {
  const state = createState();
  Object.assign(state.runs[ids.done], { status: "completed_with_warnings", warnings: ["retrieval_lane_failed"], last_event_sequence: 6 });
  state.events[ids.done] = [
    event(ids.done, 1, "query.started", {}),
    event(ids.done, 2, "query.planned", { intent: "Intent" }),
    event(ids.done, 3, "retrieval.degraded", { code: "retrieval_lane_failed", lane: "semantic" }),
    event(ids.done, 4, "retrieval.completed", { candidate_count: 24, total_candidates: 31 }),
    event(ids.done, 5, "ranking.completed", { candidate_count: 24, scored_count: 24 }),
    event(ids.done, 6, "workflow.completed", { result_count: 6 }),
  ];
  await open(page, `/runs/${ids.done}`, state);
  await page.getByTestId("stage-ranking-toggle").click();
  await expect(page.getByTestId("stage-ranking-audit")).toContainText("降级");
  const retry = page.getByTestId("stepMultiLane-retry");
  await expect(retry).toBeVisible();
  await retry.click();
  await expect.poll(() => state.retries.map((item) => item.step)).toEqual(["retrieval"]);
});

test("after a retry the re-run step is active and the kept steps say so", async ({ page }) => {
  const state = createState();
  Object.assign(state.runs[ids.done], { status: "running", result_count: 0, last_event_sequence: 10 });
  state.events[ids.done] = [
    event(ids.done, 1, "query.started", {}),
    event(ids.done, 2, "query.planned", { intent: "Intent" }),
    event(ids.done, 3, "retrieval.completed", { candidate_count: 24, total_candidates: 31 }),
    event(ids.done, 4, "ranking.started", { candidate_count: 24, batch_count: 2 }),
    event(ids.done, 5, "ranking.completed", { candidate_count: 24, scored_count: 24, warning_count: 0 }),
    event(ids.done, 6, "workflow.completed", { result_count: 6, warning_count: 0 }),
    event(ids.done, 7, "run.retry_requested", { code: "ranking", from_status: "completed" }, "lifecycle"),
    event(ids.done, 8, "run.claimed", { attempt: 2, lease_generation: 2 }, "lifecycle"),
    event(ids.done, 9, "query.resumed", { phase: "search", iteration: 1, portfolio_count: 1 }),
    event(ids.done, 10, "ranking.started", { candidate_count: 24, batch_count: 2 }),
  ];
  await open(page, `/runs/${ids.done}`, state);
  await expect(page.getByTestId("stage-ranking")).toHaveAttribute("data-state", "active");
  await expect(page.getByTestId("stepMultiLane-marker")).toContainText("沿用上次结果");
  await expect(page.getByTestId("stepLlmRanking-marker")).toHaveCount(0);
  await expect(page.getByTestId("stage-ranking-audit")).toContainText("0 / 2");
});

test("retrying the query does not duplicate the iteration list", async ({ page }) => {
  const state = createState();
  Object.assign(state.runs[ids.done], { status: "running", result_count: 0, last_event_sequence: 9 });
  state.events[ids.done] = [
    event(ids.done, 1, "query.started", {}),
    event(ids.done, 2, "query.planned", { intent: "Old intent" }),
    event(ids.done, 3, "query.progress", { iteration: 1, decision: "accept", hit_count: 12, total: 31, query: "old query" }),
    event(ids.done, 4, "run.failed", { code: "query_primary_source_failed" }, "lifecycle"),
    event(ids.done, 5, "run.retry_requested", { code: "query", from_status: "failed" }, "lifecycle"),
    event(ids.done, 6, "run.claimed", { attempt: 2, lease_generation: 2 }, "lifecycle"),
    event(ids.done, 7, "query.started", {}),
    event(ids.done, 8, "query.planned", { intent: "New intent" }),
    event(ids.done, 9, "query.progress", { iteration: 1, decision: "accept", hit_count: 20, total: 40, query: "new query" }),
  ];
  await open(page, `/runs/${ids.done}`, state);
  await expect(page.getByTestId("stage-query")).toContainText("New intent");
  await expect(page.getByTestId("iteration-row")).toHaveCount(1);
  await expect(page.getByTestId("stage-search-audit")).toContainText("20");
});
