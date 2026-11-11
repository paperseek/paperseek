<script setup lang="ts">
import type { CitationMap } from "../../api/types";
import Icon from "../../components/Icon.vue";
import { useUi } from "../../locales/ui";
import CitationMapView from "../citation-map/CitationMapView.vue";
import { paperUrl, type Review } from "./api";

defineProps<{ review: Review; view: "claims" | "relations" }>();
const u = useUi();
</script>

<template>
  <div class="stack" style="gap: 16px">
    <template v-if="view === 'relations'">
      <p class="xs muted">{{ u("mapScope", { n: review.content.papers.length }) }}</p>
      <CitationMapView :source-run-id="review.source_run_id" :saved-map="(review.content.citation_map || undefined) as CitationMap | undefined" embedded />
    </template>
    <template v-else>
      <div v-if="!review.document?.sections?.length" class="empty">
        <span class="empty-media"><Icon name="file-text" size="lg" /></span>
        <p class="empty-title">{{ u("claims") }}</p>
        <p class="empty-desc">{{ u("emptyDraft") }}</p>
      </div>
      <section v-for="section in review.document?.sections" :key="section.id" class="card flush" :aria-label="section.title">
        <div class="row" style="padding: 12px 16px; border-bottom: 1px solid var(--border); justify-content: space-between">
          <h3 class="card-title">{{ section.title }}</h3>
          <span class="xs muted">{{ u("paragraphCount", { n: section.paragraphs.length }) }}</span>
        </div>
        <details v-for="(paragraph, index) in section.paragraphs" :key="index">
          <summary class="stage" style="gap: 10px">
            <span class="xs muted medium" style="white-space: nowrap">{{ u("paragraphN", { n: index + 1 }) }}</span>
            <span class="sm muted truncate grow">{{ (paragraph.spans || []).map((span) => span.text || span.citation?.label || "").join("") }}</span>
            <span class="badge badge-outline muted">{{ u("citationsN", { n: (paragraph.spans || []).filter((span) => span.citation).length }) }}</span>
            <Icon v-if="paragraph.citation_warnings?.length" name="warn" />
            <Icon name="chevron-down" class="muted" />
          </summary>
          <div class="stack" style="gap: 12px; padding: 0 16px 16px">
            <p class="prose" style="font-size: 14px; line-height: 24px">{{ (paragraph.spans || []).map((span) => span.text || span.citation?.label || "").join("") }}</p>
            <article v-for="(span, part) in (paragraph.spans || []).filter((span) => span.citation)" :key="part" class="stack" style="gap: 6px; padding: 10px 12px; border: 1px solid var(--border); border-radius: 8px">
              <p class="sm"><span class="cite">{{ span.citation!.label }}</span> <span class="medium">{{ review.content.papers.find((paper) => paper.id === span.citation!.paper_id)?.title }}</span></p>
              <blockquote v-for="item in review.content.cards?.flatMap((card) => card.evidence).filter((item) => span.citation!.evidence_ids.includes(item.id))" :key="item.id" class="quote">{{ item.quote }}</blockquote>
              <a v-if="paperUrl(review.content.papers.find((paper) => paper.id === span.citation!.paper_id)?.url || '')" class="link xs muted" :href="paperUrl(review.content.papers.find((paper) => paper.id === span.citation!.paper_id)?.url || '')" target="_blank" rel="noopener noreferrer">{{ u("record") }}</a>
            </article>
            <p v-for="warning in paragraph.citation_warnings" :key="warning" class="xs muted row" style="gap: 6px"><Icon name="warn" size="sm" />{{ warning }}</p>
          </div>
        </details>
      </section>
    </template>
  </div>
</template>
