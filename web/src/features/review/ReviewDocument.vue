<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import Icon from "../../components/Icon.vue";
import { useLocale } from "../../locales";
import { useUi } from "../../locales/ui";
import { paperUrl, type Citation, type Review } from "./api";

const props = defineProps<{ review: Review; disabled: boolean }>();
const emit = defineEmits<{ edit: [id: string]; rewrite: [id: string]; versions: [id: string] }>();
const { t } = useLocale();
const u = useUi();
const sections = computed(() => props.review.document?.sections || []);
const current = ref("");
const popover = ref<{ citation: Citation; key: string; left: number; top: number; section: string; abstract: boolean } | null>(null);
const article = ref<HTMLElement>();
let observer: IntersectionObserver | undefined;

function status(validation: string) {
  if (validation === "user_edited") return { icon: "ok" as const, label: u("userEdited") };
  if (validation === "supported") return { icon: "ok" as const, label: u("supported") };
  return { icon: "warn" as const, label: u("needsCheck") };
}
function findings(id: string) {
  return props.review.content.sections?.find((s) => s.section_id === id)?.findings?.filter((f) => f.status !== "supported") || [];
}
function title(id: string) {
  return props.review.content.papers.find((item) => item.id === id)?.title || id;
}
const paper = computed(() => props.review.content.papers.find((item) => item.id === popover.value?.citation.paper_id));
const quotes = computed(() => props.review.content.cards?.flatMap((card) => card.evidence).filter((item) => popover.value?.citation.evidence_ids.includes(item.id)) || []);

function openCitation(event: MouseEvent, citation: Citation, key: string, section: string) {
  if (popover.value?.key === key) {
    popover.value = null;
    return;
  }
  const button = event.currentTarget as HTMLElement;
  const host = button.closest("section") as HTMLElement;
  const box = button.getBoundingClientRect();
  const hostBox = host.getBoundingClientRect();
  popover.value = {
    citation, key, section, abstract: false,
    left: Math.max(0, Math.min(hostBox.width - 380, box.left - hostBox.left - 40)),
    top: box.bottom - hostBox.top + 6,
  };
}
function outside(event: Event) {
  if (popover.value && !(event.target as Element).closest(".evidence-pop, .cite")) popover.value = null;
}
function escape(event: KeyboardEvent) {
  if (event.key === "Escape") popover.value = null;
}
async function observe() {
  observer?.disconnect();
  await nextTick();
  if (!article.value) return;
  observer = new IntersectionObserver((entries) => {
    const visible = entries.filter((entry) => entry.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
    if (visible) current.value = visible.target.id;
  }, { rootMargin: "-10% 0px -70% 0px" });
  for (const element of article.value.querySelectorAll("section[id]")) observer.observe(element);
}
watch(() => sections.value.length, observe);
onMounted(() => {
  void observe();
  document.addEventListener("pointerdown", outside);
  document.addEventListener("keydown", escape);
});
onBeforeUnmount(() => {
  observer?.disconnect();
  document.removeEventListener("pointerdown", outside);
  document.removeEventListener("keydown", escape);
});
</script>

<template>
  <div v-if="!sections.length" class="empty">
    <span class="empty-media"><Icon :name="review.active_job ? 'loader' : 'file-text'" size="lg" /></span>
    <p class="empty-title">{{ review.active_job ? u("draftLoading") : t("reviewDocument") }}</p>
    <p class="empty-desc">{{ review.active_job ? t("reviewDraftLoadingHint") : u("emptyDraft") }}</p>
  </div>
  <div v-else style="display: flex; flex-wrap: wrap; gap: 32px; align-items: flex-start; padding-top: 8px">
    <nav class="toc stack" style="flex: 1 1 180px; max-width: 220px; position: sticky; top: 16px" :aria-label="t('reviewOutline')">
      <a v-for="section in sections" :key="section.id" :class="{ on: current === `review-${section.id}` }" :href="`#review-${section.id}`" :aria-current="current === `review-${section.id}` ? 'true' : undefined">{{ section.title }}</a>
      <a :class="{ on: current === 'review-references' }" href="#review-references">{{ u("references") }}</a>
    </nav>
    <article ref="article" class="stack" style="flex: 999 1 520px; max-width: 760px; gap: 24px; min-width: 0">
      <p v-if="review.document?.draft" class="alert" style="grid-template-columns: 16px 1fr"><Icon name="info" /><span class="alert-title">{{ t("reviewDraftNotice") }}</span></p>
      <details v-if="review.content.consistency_findings?.length" class="alert" style="display: block; padding: 10px 14px">
        <summary class="row" style="cursor: pointer; gap: 10px; flex-wrap: nowrap"><Icon name="warn" /><span class="sm medium grow">{{ u("crossCheck", { n: review.content.consistency_findings.length }) }}</span><Icon name="chevron-down" class="muted" /></summary>
        <ul class="stack" style="gap: 6px; margin-top: 8px; padding-left: 26px"><li v-for="(issue, index) in review.content.consistency_findings" :key="index" class="sm muted">{{ issue.reason }}</li></ul>
      </details>
      <p v-else-if="!review.content.consistency_signature" class="xs muted">{{ u("checkPending") }}</p>

      <section v-for="section in sections" :id="`review-${section.id}`" :key="section.id" class="stack" style="gap: 10px; position: relative; scroll-margin-top: 16px">
        <div class="sec-h">
          <component :is="section.level === 2 ? 'h3' : 'h2'">{{ section.title }}</component>
          <span class="badge badge-outline muted"><Icon :name="status(section.validation).icon" />{{ status(section.validation).label }}</span>
          <button class="btn btn-ghost btn-sm" type="button" style="height: 28px" :disabled="disabled" @click="emit('edit', section.id)">{{ u("edit") }}</button>
          <button class="btn btn-ghost btn-sm" type="button" style="height: 28px" :disabled="disabled" @click="emit('rewrite', section.id)">{{ u("rewrite") }}</button>
          <button class="btn btn-ghost btn-icon btn-xs" type="button" :aria-label="u('versions')" :title="u('versions')" @click="emit('versions', section.id)"><Icon name="history" /></button>
        </div>
        <p v-if="section.stale" class="xs muted row" style="gap: 6px"><Icon name="warn" size="sm" />{{ u("stale") }}</p>
        <template v-for="(paragraph, index) in section.paragraphs" :key="index">
          <p class="prose"><template v-for="(span, part) in paragraph.spans" :key="part"><button
            v-if="span.citation"
            class="cite"
            :class="{ on: popover?.key === `${section.id}-${index}-${part}` }"
            type="button"
            :aria-label="u('evidenceN', { n: span.citation.number })"
            :aria-expanded="popover?.key === `${section.id}-${index}-${part}`"
            @click="openCitation($event, span.citation, `${section.id}-${index}-${part}`, section.id)"
          >{{ review.document?.citation_style === "harvard" ? span.citation.label : span.citation.number }}</button><template v-else>{{ span.text }}</template></template></p>
          <p v-if="paragraph.unplaced?.length" class="xs muted">{{ u("alsoCited") }}<template v-for="(citation, part) in paragraph.unplaced" :key="part"><template v-if="part">、</template><button
            class="cite"
            :class="{ on: popover?.key === `${section.id}-${index}-u${part}` }"
            type="button"
            :aria-label="u('evidenceN', { n: citation.number })"
            :aria-expanded="popover?.key === `${section.id}-${index}-u${part}`"
            @click="openCitation($event, citation, `${section.id}-${index}-u${part}`, section.id)"
          >{{ review.document?.citation_style === "harvard" ? citation.label : citation.number }}</button></template></p>
          <p v-for="warning in paragraph.citation_warnings" :key="warning" class="xs muted row" style="gap: 6px"><Icon name="warn" size="sm" />{{ warning }}</p>
        </template>
        <details v-if="section.omitted?.length">
          <summary class="xs muted row" style="cursor: pointer; gap: 6px"><Icon :name="section.omitted.some((item) => item.questioned || item.check_incomplete) ? 'warn' : 'info'" size="sm" />{{ u("notCited", { n: section.omitted.length }) }}<Icon name="chevron-down" size="sm" /></summary>
          <ul class="stack" style="gap: 6px; margin-top: 6px; padding-left: 20px">
            <li v-for="item in section.omitted" :key="item.paper_id" class="xs muted">
              <span class="medium" style="color: var(--foreground)">{{ title(item.paper_id) }}</span>{{ u("colon") }}{{ item.reason }}
              <span v-if="item.check_incomplete" class="row" style="gap: 4px; margin-top: 2px"><Icon name="warn" size="sm" />{{ u("omissionCheckIncomplete") }}</span>
              <span v-else-if="item.questioned" class="row" style="gap: 4px; margin-top: 2px"><Icon name="warn" size="sm" />{{ u("reasonMayNotHold") }}{{ item.check_reason ? u("colon") + item.check_reason : "" }}</span>
            </li>
          </ul>
        </details>
        <details v-if="findings(section.id).length" class="alert" style="display: block; padding: 10px 14px">
          <summary class="row" style="cursor: pointer; gap: 10px; flex-wrap: nowrap"><Icon name="warn" /><span class="sm medium grow">{{ t("reviewNeedsRevision") }}</span><Icon name="chevron-down" class="muted" /></summary>
          <ul class="stack" style="gap: 6px; margin-top: 8px; padding-left: 26px"><li v-for="(finding, index) in findings(section.id)" :key="index" class="sm muted">{{ u("paragraphN", { n: finding.paragraph + 1 }) }}: {{ finding.reason }}</li></ul>
        </details>
        <div
          v-if="popover && popover.section === section.id"
          role="dialog"
          :aria-label="u('evidenceN', { n: popover.citation.number })"
          class="stack evidence-pop"
          :style="{ position: 'absolute', left: `${popover.left}px`, top: `${popover.top}px`, width: '380px', maxWidth: 'calc(100% - 24px)', zIndex: 4, background: 'var(--background)', border: '1px solid var(--border)', borderRadius: '10px', boxShadow: 'var(--shadow-md)', padding: '12px 14px', gap: '8px' }"
        >
          <span class="xs muted">{{ u("evidenceN", { n: popover.citation.number }) }} · {{ u("supportingText") }}</span>
          <p class="sm medium">{{ paper?.title }}</p>
          <blockquote v-for="item in quotes" :key="item.id" class="quote">{{ item.quote }}</blockquote>
          <p v-if="popover.abstract" class="xs muted" style="max-height: 180px; overflow-y: auto">{{ paper?.abstract }}</p>
          <div class="row" style="justify-content: space-between; margin: 0 -8px -4px">
            <button class="btn btn-ghost btn-sm" type="button" style="height: 28px" :aria-expanded="popover.abstract" @click="popover.abstract = !popover.abstract">{{ u("viewAbstract") }}</button>
            <a v-if="paperUrl(paper?.url || '')" class="btn btn-ghost btn-sm" :href="paperUrl(paper?.url || '')" target="_blank" rel="noopener noreferrer" style="height: 28px">{{ u("record") }}<Icon name="arrow-up-right" /></a>
          </div>
        </div>
      </section>

      <section id="review-references" class="stack" style="gap: 8px; border-top: 1px solid var(--border); padding-top: 20px; scroll-margin-top: 16px">
        <h2 class="card-title">{{ u("references") }}</h2>
        <ol class="stack sm" style="gap: 6px; color: #404040">
          <li v-for="reference in review.document?.references" :key="reference.number">
            <template v-if="review.document?.citation_style !== 'harvard'">[{{ reference.number }}] </template>{{ reference.text }}
            <a v-if="paperUrl(reference.url)" class="link muted" :href="paperUrl(reference.url)" target="_blank" rel="noopener noreferrer">{{ reference.doi ? `DOI: ${reference.doi}` : u("record") }}</a>
          </li>
        </ol>
      </section>
    </article>
  </div>
</template>
