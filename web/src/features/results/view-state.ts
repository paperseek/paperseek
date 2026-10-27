import type { ResultPageQuery } from "../../api/client";

export interface ResultViewState {
  query: string;
  minScore: string;
  availability: string;
  journal?: string;
  acceptance?: string;
  sort: ResultPageQuery["sort"];
  selected: string[];
  uids: [string, string][];
  expanded: string[];
  loadedCount: number;
}
// Session-only, bounded to recent searches; cleared on Hosted account changes.
const recent = new Map<string, ResultViewState>();
export function rememberResults(id: string, value: ResultViewState) {
  recent.delete(id);
  recent.set(id, value);
  if (recent.size > 10) recent.delete(recent.keys().next().value!);
}
export function recallResults(id: string) {
  return recent.get(id);
}
export function clearResultViews() {
  recent.clear();
}
