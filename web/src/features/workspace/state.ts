import { reactive } from "vue";
import type { RouteLocationNormalizedLoaded } from "vue-router";
import { resetSearchDraft } from "../search/search-draft";
import { resetReviewDraft } from "../review/review-draft";
import { clearRun } from "../../state/run";
import { clearResultViews } from "../results/view-state";

export type WorkspaceModule = "search" | "bibliometrics" | "reviews";
export const modulePaths: Record<WorkspaceModule, string> = {
  search: "/search",
  bibliometrics: "/bibliometrics",
  reviews: "/reviews/new",
};
export const listPaths: Record<WorkspaceModule, string> = {
  search: "/history",
  bibliometrics: "/bibliometrics/history",
  reviews: "/reviews",
};
/** Title shown in the 48px header; views set it, with an optional breadcrumb root. */
export const pageHeader = reactive({ title: "", crumb: null as null | { label: string; to: string } });
export function setHeader(title: string, crumb: null | { label: string; to: string } = null) {
  pageHeader.title = title;
  pageHeader.crumb = crumb;
}
export const historyRevision = reactive({
  search: 0,
  bibliometrics: 0,
  reviews: 0,
});
export const settingsReturn = reactive({ path: "/search", scroll: 0 });
export function currentModule(
  route: RouteLocationNormalizedLoaded,
): WorkspaceModule {
  if (route.path.startsWith("/bibliometrics")) return "bibliometrics";
  if (route.path.startsWith("/reviews")) return "reviews";
  return "search";
}
export function invalidateHistory(module: WorkspaceModule) {
  historyRevision[module]++;
}
export function resetWorkspace() {
  clearResultViews();
  clearRun();
  resetSearchDraft();
  resetReviewDraft();
  for (const module of Object.keys(modulePaths) as WorkspaceModule[])
    invalidateHistory(module);
  Object.assign(settingsReturn, { path: "/search", scroll: 0 });
}
