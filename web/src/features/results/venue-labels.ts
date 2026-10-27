import type { JournalPartitions } from "../../api/types";
import type { useUi } from "../../locales/ui";

type Ui = ReturnType<typeof useUi>;

export function formatNumber(value: number | null | undefined, digits = 1): string {
  return value === null || value === undefined ? "" : Number(value).toFixed(digits).replace(/\.0+$/, "");
}

/** 北大核心 and CSSCI lists a journal is in, each with its edition. */
export function cnCoreListings(u: Ui, metrics: unknown): { label: string; edition: string }[] {
  const core = ((metrics || {}) as JournalPartitions).cn_core || {};
  return ([["pku", "pkuBadge"], ["cssci", "cssciBadge"], ["cssci_collection", "cssciCollectionBadge"], ["cssci_extended", "cssciExtendedBadge"]] as const)
    .filter(([list]) => core[list])
    .map(([list, key]) => ({ label: u(key), edition: core[`${list}_edition`] || "" }));
}

/** Short CCF, CAS, JCR, 北大核心 and CSSCI labels, shared by result cards and the review paper picker. */
export function venueBadges(u: Ui, metrics: unknown): string[] {
  const { cas, jcr, ccf } = (metrics || {}) as JournalPartitions;
  const badges: string[] = [];
  if (ccf?.rank) badges.push(u("ccfBadge", { rank: ccf.rank }));
  if (cas?.tier) badges.push(u("casBadge", { n: cas.tier }) + (cas.top ? ` · ${u("casTop")}` : ""));
  if (jcr?.quartile || jcr?.impact_factor != null) {
    badges.push([jcr.quartile ? u("jcrBadge", { q: jcr.quartile }) : "", jcr.impact_factor != null ? u("impactFactor", { n: formatNumber(jcr.impact_factor) }) : ""].filter(Boolean).join(" · "));
  }
  badges.push(...cnCoreListings(u, metrics).map(({ label }) => label));
  return badges;
}
