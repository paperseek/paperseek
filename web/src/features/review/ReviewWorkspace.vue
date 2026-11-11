<script setup lang="ts">
import { computed, ref, watch, watchEffect } from "vue";
import { useRoute, useRouter } from "vue-router";
import AppDialog from "../../components/AppDialog.vue";
import Icon from "../../components/Icon.vue";
import QueueStatusCard from "../../components/QueueStatusCard.vue";
import UiMenu from "../../components/UiMenu.vue";
import UiSelect from "../../components/UiSelect.vue";
import { useLocale } from "../../locales";
import { useUi } from "../../locales/ui";
import { connectionOptions } from "../settings/store";
import { invalidateHistory, listPaths, setHeader } from "../workspace/state";
import { paperUrl, reviewApi, saveDownload, type CitationStyle, type Revision } from "./api";
import ReviewDocument from "./ReviewDocument.vue";
import ReviewEvidence from "./ReviewEvidence.vue";
import ReviewOutlineEditor from "./ReviewOutlineEditor.vue";
import ReviewSectionEditor from "./ReviewSectionEditor.vue";
import { useReview } from "./use-review";

const tabs = ["papers", "outline", "document", "evidence"] as const;
type Tab = (typeof tabs)[number];
const route = useRoute();
const router = useRouter();
const { t, runMessage, language } = useLocale();
const u = useUi();
const id = computed(() => String(route.params.reviewId || ""));
const citationStyle = computed<CitationStyle>({
  get: () => (route.query.citation_style === "harvard" ? "harvard" : "numeric"),
  set: (value) => void router.replace({ query: { ...route.query, citation_style: value === "numeric" ? undefined : value } }),
});
const { review, connection, error, busy, loading, active, operation, action, submit, cancel, refresh, report } = useReview(id, citationStyle);
const tab = ref<Tab>("papers");
const outlineDirty = ref(false);
const editingId = ref("");
const rewriteId = ref("");
const instruction = ref("");
const versions = ref<Revision[] | null>(null);
const versionTitle = ref("");
const confirmRegenerate = ref(false);
const evidenceView = ref<"claims" | "relations">("claims");
const disabled = computed(() => busy.value || Boolean(active.value));
const editing = computed(() => review.value?.content.sections?.find((s) => s.section_id === editingId.value));
const latest = computed(() => review.value?.latest_job);
const stage = computed(() => (review.value?.content.sections?.length ? 3 : review.value?.content.outline ? 2 : 1));
const sectionsTotal = computed(() => review.value?.content.outline?.sections.length || 0);
const sectionsDone = computed(() => review.value?.document?.sections?.length || 0);
const pending = computed(() => review.value?.document?.sections?.filter((s) => s.stale || s.validation === "needs_revision").length || 0);
const excluded = computed(() => Object.values(review.value?.content.evidence_errors || {}).filter((code) => code === "review_evidence_unusable").length);
const evidenceReady = computed(() => review.value?.content.cards?.length || 0);
const evidenceTotal = computed(() => review.value?.content.papers.filter((p) => p.has_abstract).length || 0);
const needsWriting = computed(() => !sectionsDone.value || pending.value > 0 || sectionsDone.value < sectionsTotal.value);
const lengthLabel = computed(() => {
  const content = review.value?.content;
  if (!content) return "";
  const count = content.section_count || content.outline?.sections.length;
  return [content.language === "zh" ? "中文" : "English", t({ short: "reviewShort", standard: "reviewStandard", detailed: "reviewDetailed" }[content.length]), count ? u("chapters", { n: count }) : "",
    content.paragraph_count ? u("paragraphsEach", { n: content.paragraph_count }) : "",
    content.paragraph_length ? u(content.language === "zh" ? "sizeChars" : "sizeWords", { n: content.paragraph_length }) : ""].filter(Boolean).join(" · ");
});
const operationTitle = computed(() => {
  if (!operation.value || operation.value.jobId !== latest.value?.id) return "";
  const label = t({ prepare_outline: "reviewPrepare", write: "reviewConfirm", rewrite_section: "reviewRewrite" }[operation.value.action]);
  return [label, operation.value.title].filter(Boolean).join(" · ");
});

watchEffect(() => setHeader(review.value?.display_title || review.value?.content.topic || u("reviews"), { label: u("reviews"), to: listPaths.reviews }));

let initial = "";
watch(review, (value) => {
  if (!value || initial === value.id) return;
  initial = value.id;
  const requested = String(route.query.tab || "");
  tab.value = (tabs as readonly string[]).includes(requested) ? (requested as Tab) : stage.value === 3 ? "document" : stage.value === 2 ? "outline" : "papers";
});
watch([() => review.value?.id, () => review.value?.version, () => latest.value?.status], () => invalidateHistory("reviews"));
watch(() => route.query.tab, (value) => {
  if ((tabs as readonly string[]).includes(String(value))) tab.value = value as Tab;
});
watch(tab, (value) => {
  if (id.value && initial === id.value && route.query.tab !== value) void router.replace({ query: { ...route.query, tab: value } });
});
watch(id, () => {
  outlineDirty.value = false;
  editingId.value = rewriteId.value = "";
  versions.value = null;
});

function message(code: string) {
  const value = t(code);
  return value !== code ? value : runMessage(code, "error");
}
function tabKey(event: KeyboardEvent, index: number) {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key === "ArrowLeft" ? -1 : 1) + tabs.length) % tabs.length;
  (event.currentTarget as HTMLElement).parentElement?.querySelectorAll<HTMLButtonElement>("button")[next]?.focus();
  tab.value = tabs[next];
}
async function prepare() {
  confirmRegenerate.value = false;
  await submit("prepare_outline");
  if (!error.value) tab.value = "outline";
}
async function write() {
  if (outlineDirty.value) {
    error.value = "review_dirty";
    tab.value = "outline";
    return;
  }
  await submit("write");
  if (!error.value) tab.value = "document";
}
async function rewrite() {
  const section = review.value?.content.sections?.find((s) => s.section_id === rewriteId.value);
  if (!section) return;
  await submit("rewrite_section", { section_id: section.section_id, section_revision: section.revision, instruction: instruction.value });
  if (!error.value) {
    rewriteId.value = "";
    instruction.value = "";
  }
}
// Resubmit the failed job as it was requested; saved evidence, outline and sections are reused.
async function retry() {
  const request = review.value?.latest_request;
  if (!request || disabled.value) return;
  if (request.action === "prepare_outline") return prepare();
  if (request.action === "write") return write();
  const section = review.value?.content.sections?.find((s) => s.section_id === request.section_id);
  if (section) await submit("rewrite_section", { section_id: section.section_id, section_revision: section.revision, instruction: request.instruction || "" });
}
async function showVersions(part: string) {
  versionTitle.value = part === "outline" ? t("reviewOutline") : review.value?.content.outline?.sections.find((section) => section.id === part)?.title || t("reviewDocument");
  try {
    versions.value = (await reviewApi.versions(id.value, part)).items;
  } catch (e) {
    report(e);
  }
}
async function exportFile(format: "md" | "docx" | "bibtex") {
  await action(async () => saveDownload(await reviewApi.export(id.value, review.value!.version, format, citationStyle.value)));
}
function tabLabel(step: Tab) {
  return u(({ papers: "tabPapers", outline: "tabOutline", document: "tabDocument", evidence: "tabEvidence" } as const)[step]);
}
</script>

<template>
  <main class="page">
    <template v-if="loading && !review">
      <div class="toolbar"><span class="skeleton" style="width: 320px; height: 36px; border-radius: 10px"></span></div>
      <div class="card c" data-testid="review-loading"><div class="card-content stack" style="gap: 10px"><span class="skeleton" style="height: 24px; width: 40%"></span><span class="skeleton" style="height: 14px; width: 60%"></span></div></div>
    </template>
    <div v-if="error && !review" class="alert" role="alert"><Icon name="fail" /><p class="alert-title">{{ message(error) }}</p><div class="alert-act"><button class="btn btn-outline btn-sm" type="button" @click="refresh">{{ u("retry") }}</button></div></div>

    <template v-if="review">
      <div class="toolbar">
        <div class="tabs" role="tablist" :aria-label="u('reviews')">
          <button
            v-for="(step, index) in tabs"
            :id="`review-tab-${step}`"
            :key="step"
            class="tab"
            :class="{ active: tab === step }"
            type="button"
            role="tab"
            :aria-selected="tab === step"
            :aria-controls="`review-panel-${step}`"
            :tabindex="tab === step ? 0 : -1"
            @keydown="tabKey($event, index)"
            @click="tab = step"
          >{{ tabLabel(step) }}<span v-if="step === 'papers'" class="badge badge-secondary">{{ review.content.papers.length }}</span></button>
        </div>
        <div v-if="tab === 'evidence'" class="seg" role="radiogroup" :aria-label="u('evidenceView')" style="margin-left: 8px">
          <label><input v-model="evidenceView" type="radio" name="evidence-view" value="claims" /><span>{{ u("claims") }}</span></label>
          <label><input v-model="evidenceView" type="radio" name="evidence-view" value="relations" /><span>{{ u("relations") }}</span></label>
        </div>
        <span v-else-if="tab === 'document' && sectionsDone" class="sm muted row" style="gap: 6px; margin-left: 8px">
          <Icon :name="pending ? 'warn' : 'ok'" size="sm" />{{ u("generatedOf", { done: sectionsDone, total: sectionsTotal }) }}<template v-if="pending"> · {{ u("pendingOf", { n: pending }) }}</template>
        </span>
        <span v-else class="sm muted" style="margin-left: 8px">{{ lengthLabel }}</span>
        <div class="row end" style="gap: 6px">
          <template v-if="tab === 'outline' || tab === 'papers'">
            <button v-if="tab === 'outline' && review.content.outline" class="btn btn-ghost btn-sm" type="button" @click="showVersions('outline')"><Icon name="history" />{{ u("versions") }}</button>
            <UiSelect v-model="connection" :options="connectionOptions('llm')" :label="u('writingModel')" :placeholder="u('writingModel')" size="sm" inline chevron="down" :disabled="disabled" />
          </template>
          <template v-else-if="tab === 'document'">
            <UiSelect v-model="citationStyle" :options="[{ value: 'numeric', label: u('citeNumeric') }, { value: 'harvard', label: u('citeHarvard') }]" :label="u('citationStyle')" :prefix="u('cite')" size="sm" inline chevron="down" />
            <UiMenu :label="u('export')" trigger-class="btn btn-outline btn-sm" align="end" :min-width="140" :disabled="disabled || !review.document?.sections?.length">
              <template #trigger><Icon name="download" />{{ u("export") }}<Icon name="chevron-down" /></template>
              <button v-for="format in ['md', 'docx', 'bibtex'] as const" :key="format" class="opt" role="menuitem" data-menu-item type="button" @click="exportFile(format)">{{ { md: "Markdown", docx: "Word (DOCX)", bibtex: "BibTeX" }[format] }}</button>
            </UiMenu>
          </template>
          <span v-else-if="tab === 'evidence'" class="sm muted row" style="gap: 4px">
            <RouterLink class="link" :to="`/runs/${review.source_run_id}/results`">{{ u("sourceSearch") }}</RouterLink>
            <button class="btn btn-ghost btn-icon btn-xs" type="button" :aria-label="u('explain')" :title="u('relationNote')" style="width: 24px; height: 24px"><Icon name="info" size="sm" /></button>
          </span>
        </div>
      </div>

      <QueueStatusCard v-if="active?.status === 'queued'" :run="active" :busy="busy" :poll="false" @cancel="cancel" />
      <section v-else-if="active || tab === 'papers' || tab === 'outline'" class="card c" :aria-label="u('progress')">
        <div class="card-content row" style="gap: 16px; justify-content: space-between">
          <div class="stack" style="gap: 12px">
            <ol class="hsteps" :aria-label="u('progress')">
              <template v-for="(label, index) in ['stepEvidence', 'stepOutline', 'stepWrite'] as const" :key="label">
                <li v-if="index" class="hline" aria-hidden="true"></li>
                <li class="hstep" :class="{ muted: stage < index + 1 }" :aria-current="stage === index + 1 ? 'step' : undefined">
                  <span v-if="stage > index + 1 || (index === 2 && sectionsDone && !needsWriting)" class="step-dot done"><Icon name="check" size="sm" /></span>
                  <span v-else class="step-dot" :class="{ active: stage === index + 1 }">{{ index + 1 }}</span>{{ u(label) }}
                </li>
              </template>
            </ol>
            <div class="stack" style="gap: 2px">
              <p v-if="active" class="sm medium row" style="gap: 6px" role="status"><Icon name="loader" />{{ operationTitle || t(active.status) }}</p>
              <p v-else-if="latest?.status === 'failed' || latest?.status === 'cancelled'" class="sm medium" role="status">{{ u(latest.status === "failed" ? "latestTaskFailed" : "latestTaskCancelled") }}</p>
              <p v-else class="sm medium" role="status">{{ t(stage === 1 ? "reviewReadyToPrepare" : stage === 2 ? "reviewReadyToWrite" : "reviewReadyToRead") }}</p>
              <p class="xs muted">
                {{ u("evidenceCount", { done: evidenceReady, total: evidenceTotal }) }}<template v-if="excluded"> · {{ u("evidenceExcluded", { n: excluded }) }}</template><template v-if="sectionsTotal"> · {{ u("generatedOf", { done: sectionsDone, total: sectionsTotal }) }}</template><template v-if="pending"> · {{ u("pendingOf", { n: pending }) }}</template>
              </p>
            </div>
          </div>
          <button v-if="active" class="btn btn-outline btn-sm danger" type="button" :disabled="busy || active.status === 'cancelling'" @click="cancel"><Icon name="square" />{{ u("stopTask") }}</button>
          <div v-else class="row" style="gap: 6px">
            <button v-if="review.content.outline" class="btn btn-ghost btn-sm" type="button" aria-haspopup="dialog" :disabled="busy || outlineDirty" @click="confirmRegenerate = true">{{ u("regenerateOutline") }}</button>
            <button v-else class="btn btn-sm" type="button" :disabled="busy || !connection" @click="prepare">{{ u("generateFirstOutline") }}</button>
            <button v-if="review.content.outline && needsWriting" class="btn btn-sm" type="button" :disabled="busy || outlineDirty || !connection" @click="write">{{ u(sectionsDone ? "continueWriting" : "confirmAndWrite") }}</button>
          </div>
        </div>
      </section>

      <div v-if="error" class="alert" role="alert" style="grid-template-columns: 16px 1fr"><Icon name="fail" /><p class="alert-title">{{ message(error) }}</p></div>
      <div v-if="latest?.error_code" class="alert" role="alert" :style="review.latest_request && !active ? undefined : 'grid-template-columns: 16px 1fr'">
        <Icon name="fail" />
        <p class="alert-title">{{ message(latest.error_code) }}</p>
        <div v-if="review.latest_request && !active" class="alert-act"><button class="btn btn-outline btn-sm" type="button" :disabled="disabled || !connection" @click="retry"><Icon v-if="busy" name="loader" />{{ u("retry") }}</button></div>
        <details v-if="review.generation_failure" class="alert-desc">
          <summary class="sm" style="cursor: pointer">{{ t("reviewFailureDetails") }} · {{ t(`reviewTask_${review.generation_failure.task}`) }}</summary>
          <p class="xs" style="white-space: pre-wrap; overflow-wrap: anywhere; margin-top: 4px">{{ review.generation_failure.reason }}</p>
        </details>
      </div>

      <section v-show="tab === 'papers'" id="review-panel-papers" role="tabpanel" aria-labelledby="review-tab-papers" class="card flush">
        <details v-for="paper in review.content.papers" :key="paper.id">
          <summary class="stage" style="gap: 10px">
            <span class="xs muted num" style="width: 20px">{{ paper.source_rank }}</span>
            <span class="stack grow" style="min-width: 0"><span class="sm medium truncate">{{ paper.title }}</span><span class="xs muted truncate">{{ [paper.authors?.slice(0, 3).join(", "), paper.year, paper.venue].filter(Boolean).join(" · ") }}</span></span>
            <span v-if="!paper.has_abstract" class="badge badge-outline muted">{{ u("noAbstractShort") }}</span>
            <span v-else-if="review.content.evidence_errors?.[paper.id]" class="badge badge-outline muted"><Icon name="warn" />{{ u(review.content.evidence_errors[paper.id] === "review_evidence_unusable" ? "noEvidence" : "evidenceFailed") }}</span>
            <Icon name="chevron-down" class="muted" />
          </summary>
          <div class="stack" style="gap: 8px; padding: 0 16px 16px 46px">
            <p v-if="review.content.evidence_errors?.[paper.id]" class="xs muted">{{ t(review.content.evidence_errors[paper.id] === "review_evidence_unusable" ? "review_evidence_unusable" : "reviewEvidenceFailed") }}</p>
            <p class="abstract">{{ paper.abstract || u("noAbstract") }}</p>
            <a v-if="paperUrl(paper.url || '')" class="link sm" :href="paperUrl(paper.url || '')" target="_blank" rel="noopener noreferrer">{{ paper.doi || u("record") }}</a>
          </div>
        </details>
      </section>

      <section v-show="tab === 'outline'" id="review-panel-outline" role="tabpanel" aria-labelledby="review-tab-outline">
        <ReviewOutlineEditor v-if="review.content.outline" :key="review.id" :review="review" :disabled="disabled" @dirty="outlineDirty = $event" @save="action" />
        <div v-else class="empty">
          <span class="empty-media"><Icon :name="active ? 'loader' : 'file-text'" size="lg" /></span>
          <p class="empty-title">{{ active ? u("outlineLoading") : t("reviewOutline") }}</p>
          <p class="empty-desc">{{ active ? t("reviewPreparingHint") : u("outlineEmpty") }}</p>
        </div>
      </section>

      <section v-show="tab === 'document'" id="review-panel-document" role="tabpanel" aria-labelledby="review-tab-document">
        <ReviewDocument :review="review" :disabled="disabled" @edit="editingId = $event" @rewrite="rewriteId = $event" @versions="showVersions" />
      </section>

      <section v-if="tab === 'evidence'" id="review-panel-evidence" role="tabpanel" aria-labelledby="review-tab-evidence">
        <ReviewEvidence :review="review" :view="evidenceView" />
      </section>

      <ReviewSectionEditor v-if="editing" :key="editing.section_id" :review="review" :section="editing" :busy="busy" :error="error" @close="editingId = ''" @save="action" />

      <AppDialog v-if="rewriteId" :title="`${u('rewrite')} · ${review.content.outline?.sections.find((section) => section.id === rewriteId)?.title || ''}`" :description="u('rewriteHint')" :busy="busy" @close="rewriteId = ''">
        <form id="rewrite-form" class="stack" style="gap: 16px" @submit.prevent="rewrite">
          <div class="field"><label class="label" for="rewrite-instruction">{{ u("instruction") }}</label><textarea id="rewrite-instruction" v-model="instruction" class="input textarea" rows="4" maxlength="2000" required :disabled="busy"></textarea></div>
          <div class="field"><span class="label">{{ u("writingModel") }}</span><UiSelect v-model="connection" :options="connectionOptions('llm')" :label="u('writingModel')" /></div>
        </form>
        <template #footer>
          <button class="btn btn-outline btn-sm" type="button" :disabled="busy" @click="rewriteId = ''">{{ u("cancel") }}</button>
          <button class="btn btn-sm" type="submit" form="rewrite-form" :disabled="disabled || !instruction.trim() || !connection">{{ u("rewrite") }}</button>
        </template>
      </AppDialog>

      <AppDialog v-if="confirmRegenerate" :title="u('regenerateTitle')" :description="u('regenerateDesc')" @close="confirmRegenerate = false">
        <template #footer>
          <button class="btn btn-outline btn-sm" type="button" @click="confirmRegenerate = false">{{ u("cancel") }}</button>
          <button class="btn btn-sm" type="button" :disabled="busy || !connection" @click="prepare">{{ u("generateOutline") }}</button>
        </template>
      </AppDialog>

      <AppDialog v-if="versions" :title="`${u('versions')} · ${versionTitle}`" size="lg" @close="versions = null">
        <p v-if="!versions.length" class="sm muted">{{ u("noVersions") }}</p>
        <div v-else class="card flush" style="box-shadow: none">
          <details v-for="version in versions" :key="version.revision">
            <summary class="stage" style="gap: 10px">
              <span class="sm medium">{{ u("version", { n: version.revision }) }}</span>
              <span class="badge badge-outline muted">{{ u(version.author === "user" ? "byUser" : "byModel") }}</span>
              <span class="xs muted num end">{{ new Date(version.created_at).toLocaleString(language === "zh" ? "zh-CN" : "en") }}</span>
              <Icon name="chevron-down" class="muted" />
            </summary>
            <div class="stack" style="gap: 8px; padding: 0 16px 16px">
              <template v-if="version.outline">
                <p v-if="version.outline.focus" class="sm">{{ version.outline.focus }}</p>
                <div v-for="section in version.outline.sections" :key="section.id" class="stack" style="gap: 2px">
                  <p class="sm medium">{{ section.title }}</p>
                  <p class="xs muted">{{ section.goal }}</p>
                </div>
              </template>
              <p v-for="(paragraph, index) in version.section?.body.paragraphs || []" :key="index" class="sm">{{ paragraph.text }}</p>
            </div>
          </details>
        </div>
      </AppDialog>
    </template>
  </main>
</template>
