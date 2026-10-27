<script setup lang="ts">
import { computed, ref } from "vue";
import type { Acceptance, JournalPartitions, PaperResult } from "../../api/types";
import AppDialog from "../../components/AppDialog.vue";
import Icon from "../../components/Icon.vue";
import UiMenu from "../../components/UiMenu.vue";
import { useLocale } from "../../locales";
import { useUi } from "../../locales/ui";
import { cnCoreListings, formatNumber, venueBadges } from "./venue-labels";

const props = defineProps<{ paper: PaperResult; identity: string; selected: boolean; expanded: ReadonlySet<string> }>();
const emit = defineEmits<{ select: []; expand: [key: string] }>();
const { sourceName, t } = useLocale();
const u = useUi();
const details = ref(false);
const score = computed(() => {
  const value = props.paper.score ?? props.paper.relevance_score;
  return value === null || value === undefined ? null : Number(value);
});
const scoreClass = computed(() => (score.value === null ? "badge-outline muted" : score.value >= 8 ? "" : score.value >= 5 ? "badge-secondary" : "badge-outline"));
const authors = computed(() => props.paper.authors_text || props.paper.authors?.filter(Boolean).join(", ") || t("unknownAuthors"));
const year = computed(() => props.paper.year ?? props.paper.publish_year);
const venue = computed(() => props.paper.venue || props.paper.source || "");
const citations = computed(() => props.paper.citation_count ?? props.paper.citations);
const reasoning = computed(() => props.paper.reasoning || props.paper.relevance_reason || "");
const open = computed(() => props.expanded.has(`${props.identity}:abstract`));
const keywords = computed(() => (Array.isArray(props.paper.keywords) ? props.paper.keywords.join("; ") : props.paper.keywords || ""));
const type = computed(() => (props.paper.document_types?.length ? props.paper.document_types.join("; ") : props.paper.publication_type || ""));
const journal = computed(() => (props.paper.journal_metrics || {}) as JournalPartitions);
const acceptance = computed(() => (props.paper.acceptance || {}) as Acceptance);
const number = formatNumber;
// Short labels beside the score; the details dialog spells out each platform and edition.
const badges = computed(() => venueBadges(u, journal.value));
const partitionRows = computed(() => {
  const { cas, jcr, ccf } = journal.value;
  const edition = (year?: number | null) => (year ? ` · ${u("partitionYear", { year })}` : "");
  return [
    cas?.tier ? `${u("casBadge", { n: cas.tier })}${cas.top ? ` · ${u("casTop")}` : ""}${cas.category ? ` · ${cas.category}` : ""}${cas.rank ? ` · ${cas.rank}` : ""}${edition(cas.year)}` : "",
    jcr?.quartile || jcr?.impact_factor != null ? `${[jcr.quartile ? u("jcrBadge", { q: jcr.quartile }) : "", jcr.impact_factor != null ? u("impactFactor", { n: number(jcr.impact_factor) }) : "", jcr.rank || ""].filter(Boolean).join(" · ")}${edition(jcr.year)}` : "",
    ccf?.rank ? `${u("ccfBadge", { rank: ccf.rank })} · ${u(ccf.type === "journal" ? "ccfJournal" : "ccfConference")}${ccf.abbreviation ? ` · ${ccf.abbreviation}` : ""}${ccf.field ? ` · ${ccf.field}` : ""}${edition(ccf.year)}` : "",
    ...cnCoreListings(u, journal.value).map(({ label, edition: listed }) => (listed ? `${label} · ${u("partitionYear", { year: listed })}` : label)),
  ].filter(Boolean);
});
const openImpact = computed(() => journal.value.openalex);
</script>

<template>
  <article class="card paper" data-testid="result-card" :class="{ sel: selected }">
    <label class="cb" style="margin-top: 3px" :aria-label="`${u('select')}: ${paper.title}`"><input type="checkbox" :checked="selected" @change="emit('select')" /><span class="box"><Icon name="check" /></span></label>
    <div class="stack" style="gap: 4px; min-width: 0">
      <a v-if="paper.url" class="paper-title" :href="paper.url" target="_blank" rel="noopener">{{ paper.title }}</a>
      <span v-else class="paper-title">{{ paper.title }}</span>
      <p class="sm muted truncate" :title="authors">{{ authors }}<template v-if="venue || year"> · {{ [venue, year].filter(Boolean).join(" ") }}</template></p>
    </div>
    <UiMenu :label="u('paperMore')" align="end">
      <template #trigger><Icon name="ellipsis" /></template>
      <a v-if="paper.url" class="opt" role="menuitem" data-menu-item :href="paper.url" target="_blank" rel="noopener"><Icon name="arrow-up-right" />{{ u("openRecord") }}</a>
      <a v-if="paper.pdf_url" class="opt" role="menuitem" data-menu-item :href="paper.pdf_url" target="_blank" rel="noopener"><Icon name="download" />{{ u("openPdf") }}</a>
      <button class="opt" role="menuitem" data-menu-item type="button" @click="details = true"><Icon name="file-text" />{{ u("details") }}</button>
    </UiMenu>
    <div class="body">
      <p v-if="paper.abstract" class="abstract" :class="{ 'clamp-2': !open }" style="cursor: pointer" :title="open ? undefined : t('expandAll')" @click="emit('expand', `${identity}:abstract`)">{{ paper.abstract }}</p>
      <p v-else class="abstract">{{ u("noAbstract") }}</p>
      <p v-if="reasoning" class="sm" style="color: #404040"><span class="medium" style="color: var(--foreground)">{{ u("reasoning") }} · </span>{{ reasoning }}</p>
      <div class="row" style="gap: 6px">
        <span class="badge" :class="scoreClass" data-testid="result-score">{{ score === null ? u("unscored") : `${score}/10` }}</span>
        <a v-if="paper.doi" class="badge badge-outline" :href="`https://doi.org/${paper.doi}`" target="_blank" rel="noopener">DOI</a>
        <a v-if="paper.pdf_url" class="badge badge-secondary" :href="paper.pdf_url" target="_blank" rel="noopener"><Icon name="download" />PDF</a>
        <a v-if="acceptance.label && acceptance.url" class="badge badge-outline" data-testid="result-acceptance" :href="acceptance.url" target="_blank" rel="noopener">{{ acceptance.label }}</a>
        <span v-else-if="acceptance.label" class="badge badge-outline" data-testid="result-acceptance">{{ acceptance.label }}</span>
        <span v-for="badge in badges" :key="badge" class="badge badge-outline" data-testid="result-journal-rank">{{ badge }}</span>
        <span v-if="citations != null" class="xs muted num end">{{ u("citations", { n: Number(citations).toLocaleString() }) }}</span>
      </div>
    </div>
    <AppDialog v-if="details" :title="paper.title || ''" size="lg" @close="details = false">
      <dl class="kv" style="grid-template-columns: auto minmax(0, 1fr)">
        <dt>{{ u("authors") }}</dt><dd style="text-align: left">{{ authors }}</dd>
        <template v-if="year"><dt>{{ u("year") }}</dt><dd style="text-align: left">{{ year }}</dd></template>
        <template v-if="venue"><dt>{{ u("venue") }}</dt><dd style="text-align: left">{{ venue }}</dd></template>
        <template v-if="type"><dt>{{ u("type") }}</dt><dd style="text-align: left">{{ type }}</dd></template>
        <template v-if="acceptance.label"><dt>{{ u("acceptanceLabel") }}</dt><dd style="text-align: left">{{ acceptance.label }}<template v-if="acceptance.sources?.length"> · <span class="muted">{{ acceptance.sources.map((name) => name === "paperhub" ? "Paper Hub" : "OpenReview").join(" + ") }}</span></template></dd></template>
        <template v-if="partitionRows.length"><dt>{{ u("journalPartitions") }}</dt><dd style="text-align: left"><span v-for="row in partitionRows" :key="row" style="display: block">{{ row }}</span><span class="xs muted" style="display: block">{{ u("partitionNote") }}<template v-if="journal.matched_by === 'title'"> · {{ u("matchedByTitle") }}</template></span></dd></template>
        <template v-if="openImpact"><dt>{{ u("openImpactLabel") }}</dt><dd style="text-align: left">{{ u("openImpactValue", { citedness: number(openImpact.two_year_mean_citedness, 2) || "—", h: openImpact.h_index ?? "—" }) }}</dd></template>
        <template v-if="paper.doi"><dt>DOI</dt><dd style="text-align: left"><a class="link" :href="`https://doi.org/${paper.doi}`" target="_blank" rel="noopener">{{ paper.doi }}</a></dd></template>
        <template v-if="keywords"><dt>{{ u("keywords") }}</dt><dd style="text-align: left">{{ keywords }}</dd></template>
        <template v-if="paper.provider"><dt>{{ u("provider") }}</dt><dd style="text-align: left">{{ sourceName(paper.provider, paper.provider) }}</dd></template>
        <template v-if="paper.rank"><dt>{{ t("rankLabel") }}</dt><dd style="text-align: left">{{ paper.rank }}</dd></template>
      </dl>
      <p v-if="paper.abstract" class="abstract">{{ paper.abstract }}</p>
    </AppDialog>
  </article>
</template>
