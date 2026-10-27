import type { JournalPartitions } from "../../api/types";
import type { useUi } from "../../locales/ui";

type Ui = ReturnType<typeof useUi>;

export function formatNumber(value: number | null | undefined, digits = 1): string {
  return value === null || value === undefined ? "" : Number(value).toFixed(digits).replace(/\.0+$/, "");
}

/** Short CCF, CAS and JCR labels, shared by result cards and the review paper picker. */
export function venueBadges(u: Ui, metrics: unknown): string[] {
  const { cas, jcr, ccf } = (metrics || {}) as JournalPartitions;
  const badges: string[] = [];
  if (ccf?.rank) badges.push(u("ccfBadge", { rank: ccf.rank }));
  if (cas?.tier) badges.push(u("casBadge", { n: cas.tier }) + (cas.top ? ` · ${u("casTop")}` : ""));
  if (jcr?.quartile || jcr?.impact_factor != null) {
    badges.push([jcr.quartile ? u("jcrBadge", { q: jcr.quartile }) : "", jcr.impact_factor != null ? u("impactFactor", { n: formatNumber(jcr.impact_factor) }) : ""].filter(Boolean).join(" · "));
  }
  return badges;
}
