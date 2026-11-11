<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from "vue";
import { useRoute } from "vue-router";

import { api } from "../../api/client";
import type { CitationMap, CitationNode, RunDetail } from "../../api/types";
import Icon from "../../components/Icon.vue";
import UiSelect from "../../components/UiSelect.vue";
import { useLocale } from "../../locales";
import { useUi } from "../../locales/ui";
import { setHeader } from "../workspace/state";
import { GRAPH_HEIGHT, GRAPH_WIDTH, fitCitationViewport, layoutCitationMap, nodeRadius } from "./citation-layout";

type NodeRole = "result" | "seed" | "forward" | "backward";

const props = defineProps<{ snapshot?: { run: RunDetail; citationMap: CitationMap }; sourceRunId?: string; embedded?: boolean; savedMap?: CitationMap }>();
const { sourceName, t } = useLocale();
const u = useUi();
const route = useRoute();
const runId = computed(() => props.sourceRunId || String(route.params.runId || ""));
const run = ref<RunDetail | null>(null);
const citationMap = ref<CitationMap | null>(null);
const pageState = ref<"loading" | "error" | "loaded">("loading");
const selectedId = ref("");
const focusId = ref("");
const hoveredId = ref("");
const focusedNodeId = ref("");
const zoom = ref(1);
const pan = reactive({ x: 0, y: 0 });
// User drags persist across zoom resets and seed-focus changes (§10.3).
const positions = ref<Record<string, { x: number; y: number }>>({});
let pointer: { id: number; mode: "pan" | "node"; nodeId?: string; x: number; y: number; moved: boolean } | null = null;
let generation = 0;
let controller: AbortController | null = null;

const seeds = computed(() => (citationMap.value?.nodes || []).filter((node) => node.roles?.includes("seed")));
const visibleIds = computed(() => {
  if (!focusId.value || !citationMap.value) return null;
  const ids = new Set([focusId.value]);
  for (const edge of citationMap.value.edges || []) {
    if (edge.source === focusId.value) ids.add(edge.target);
    if (edge.target === focusId.value) ids.add(edge.source);
  }
  return ids;
});
const nodes = computed(() => (citationMap.value?.nodes || []).filter((node) => !visibleIds.value || visibleIds.value.has(node.id)));
const fittedViewport = computed(() => fitCitationViewport(nodes.value.map(node => point(node.id))));
const edges = computed(() => (citationMap.value?.edges || []).filter((edge) => nodes.value.some((node) => node.id === edge.source) && nodes.value.some((node) => node.id === edge.target)));
const selected = computed(() => citationMap.value?.nodes?.find((node) => node.id === selectedId.value) || null);

// Empty states are classified, never a blank canvas (§10.4).
const emptyState = computed(() => {
  const map = citationMap.value;
  if (!map) return "";
  if (map["enabled"] === false) return "citationNotEnabled";
  if (map["supported"] === false) return "citationNotSupported";
  if (!(map.nodes || []).length) return "citationEmpty";
  return "";
});

const metricsLine = computed(() => {
  const map = citationMap.value;
  if (!map) return "";
  const parts: string[] = [];
  if (typeof map["seed_count"] === "number") parts.push(`${map["seed_count"]} ${t("seedsCount")}`);
  if (typeof map["added_candidates"] === "number") parts.push(`${map["added_candidates"]} ${t("citationAdded")}`);
  return parts.join(" · ");
});

// Color and shape together encode the role; legend glyphs use the same geometry.
function primaryRole(node: CitationNode): NodeRole {
  const roles = node.roles || [];
  if (roles.includes("seed")) return "seed";
  if (roles.includes("forward")) return "forward";
  if (roles.includes("backward")) return "backward";
  return "result";
}

const legend: NodeRole[] = ["result", "seed", "forward", "backward"];

function radius(node: CitationNode): number {
  return nodeRadius(node.citations);
}

function shapePath(role: NodeRole, r: number): string {
  if (role === "forward") return `M0 ${-r} L${r} ${r} L${-r} ${r} Z`;
  if (role === "backward") return `M0 ${-r} L${r} 0 L0 ${r} L${-r} 0 Z`;
  return "";
}

function shortTitle(node: CitationNode): string {
  const title = node.title || "";
  return title.length > 28 ? `${title.slice(0, 28)}…` : title;
}

function showLabel(node: CitationNode): boolean {
  return primaryRole(node) === "seed"
    || selectedId.value === node.id
    || hoveredId.value === node.id
    || focusedNodeId.value === node.id
    || zoom.value >= 1.5;
}

function nodeLabel(node: CitationNode): string {
  if (showLabel(node)) return shortTitle(node);
  return node.rank ? `#${node.rank}` : "";
}

function resetGraph(): void {
  // Reset restores the viewport only; dragged node positions are kept (§10.3).
  zoom.value = fittedViewport.value.zoom;
  pan.x = fittedViewport.value.x;
  pan.y = fittedViewport.value.y;
}

function point(id: string): { x: number; y: number } {
  return positions.value[id] || { x: GRAPH_WIDTH / 2, y: GRAPH_HEIGHT / 2 };
}

function clampZoom(value: number): number {
  return Math.min(2.5, Math.max(Math.min(0.5, fittedViewport.value.zoom / 2), Number(value.toFixed(3))));
}

function onWheel(event: WheelEvent): void {
  zoom.value = clampZoom(zoom.value * (event.deltaY < 0 ? 1.12 : 0.89));
}

function onPointerDown(event: PointerEvent): void {
  pointer = {
    id: event.pointerId,
    mode: "pan",
    x: event.clientX,
    y: event.clientY,
    moved: false,
  };
  (event.currentTarget as SVGElement).setPointerCapture(event.pointerId);
}

function onNodePointerDown(event: PointerEvent, nodeId: string): void {
  pointer = { id: event.pointerId, mode: "node", nodeId, x: event.clientX, y: event.clientY, moved: false };
  const svg = (event.currentTarget as SVGGElement).ownerSVGElement;
  svg?.setPointerCapture(event.pointerId);
}

function onPointerMove(event: PointerEvent): void {
  if (!pointer || pointer.id !== event.pointerId) return;
  const dx = event.clientX - pointer.x;
  const dy = event.clientY - pointer.y;
  if (Math.abs(dx) + Math.abs(dy) > 1) pointer.moved = true;
  if (pointer.mode === "node" && pointer.nodeId) {
    const current = point(pointer.nodeId);
    positions.value = { ...positions.value, [pointer.nodeId]: { x: current.x + dx / zoom.value, y: current.y + dy / zoom.value } };
  } else {
    pan.x += dx;
    pan.y += dy;
  }
  pointer.x = event.clientX;
  pointer.y = event.clientY;
}

function onPointerUp(event: PointerEvent): void {
  if (!pointer || pointer.id !== event.pointerId) return;
  if (pointer.mode === "node" && pointer.nodeId && !pointer.moved) selectNode(pointer.nodeId);
  pointer = null;
  (event.currentTarget as SVGElement).releasePointerCapture(event.pointerId);
}

function selectNode(id: string): void {
  selectedId.value = id;
}

function onNodeKeydown(event: KeyboardEvent, nodeId: string): void {
  if (event.key !== "Enter" && event.key !== " ") return;
  event.preventDefault();
  selectNode(nodeId);
}

function isRecordLink(id: string): boolean {
  return /^https?:\/\//.test(id);
}

function applyMap(map: CitationMap): void {
  citationMap.value = map;
  positions.value = layoutCitationMap(map.nodes || [], map.edges || []);
  selectedId.value = seeds.value[0]?.id || map.nodes?.[0]?.id || "";
  resetGraph();
}

async function load(value: string): Promise<void> {
  if (!value) return;
  const current = ++generation;
  controller?.abort();
  controller = new AbortController();
  run.value = null;
  citationMap.value = null;
  selectedId.value = "";
  focusId.value = "";
  positions.value = {};
  pageState.value = "loading";
  if (props.savedMap) {
    applyMap(props.savedMap); pageState.value = "loaded"; return;
  }
  if (props.snapshot?.run.id === value) {
    run.value = props.snapshot.run;
    applyMap(props.snapshot.citationMap);
    pageState.value = "loaded";
    return;
  }
  const [detail, map] = await Promise.allSettled([
    api.run(value, controller.signal),
    api.citationMap(value, controller.signal),
  ]);
  if (current !== generation || runId.value !== value) return;
  if (detail.status === "fulfilled") run.value = detail.value;
  if (map.status === "fulfilled") {
    applyMap(map.value);
    pageState.value = "loaded";
  } else {
    pageState.value = "error";
  }
}

watch(runId, (value) => void load(value), { immediate: true });
watch(run, (value) => {
  if (!props.embedded) setHeader(value?.display_title || value?.question || t("citationMap"), value ? { label: u("search"), to: `/runs/${value.id}/results` } : null);
}, { immediate: true });
const roleColor: Record<NodeRole, { fill: string; stroke: string }> = {
  result: { fill: "#000", stroke: "#fff" },
  seed: { fill: "#155dfc", stroke: "#fff" },
  forward: { fill: "#8ec5ff", stroke: "#fff" },
  backward: { fill: "#fff", stroke: "#737373" },
};
const seedOptions = computed(() => [{ value: "", label: u("allSeeds") }, ...seeds.value.map((seed) => ({ value: seed.id, label: seed.title || seed.id }))]);
function connected(edge: { source: string; target: string }) {
  return edge.source === selectedId.value || edge.target === selectedId.value;
}
watch(focusId, resetGraph);

onBeforeUnmount(() => {
  generation += 1;
  controller?.abort();
});
</script>

<template>
  <component :is="embedded ? 'div' : 'main'" :class="{ page: !embedded }" class="stack" style="gap: 16px">
    <div v-if="pageState === 'loading'" class="card flush" data-testid="citation-loading" aria-busy="true" style="height: 420px; display: grid; place-items: center"><Icon name="loader" size="lg" class="muted" /></div>
    <div v-else-if="pageState === 'error'" class="alert" role="alert"><Icon name="fail" /><p class="alert-title">{{ u("loadError") }}</p><div class="alert-act"><button class="btn btn-outline btn-sm" type="button" @click="load(runId)">{{ u("retry") }}</button></div></div>
    <div v-else-if="!citationMap || emptyState" class="empty" data-testid="citation-empty">
      <span class="empty-media"><Icon name="chart" size="lg" /></span>
      <p class="empty-title">{{ emptyState === "citationNotEnabled" ? u("mapNotEnabled") : emptyState === "citationNotSupported" ? u("mapNotSupported") : u("mapEmpty") }}</p>
      <RouterLink class="btn btn-outline btn-sm" style="margin-top: 8px" :to="`/runs/${runId}`">{{ t("backToRun") }}</RouterLink>
    </div>
    <div v-else style="display: flex; flex-wrap: wrap; gap: 16px; align-items: flex-start">
      <section class="card flush" style="flex: 999 1 560px" :aria-label="u('relations')">
        <div class="toolbar" style="padding: 8px 8px 8px 16px; border-bottom: 1px solid var(--border)">
          <ul class="legend" :aria-label="u('legend')">
            <li v-for="role in legend" :key="role" class="row" style="gap: 6px">
              <svg width="12" height="12" viewBox="-8 -8 16 16" aria-hidden="true">
                <circle v-if="role === 'result'" r="6" :fill="roleColor.result.fill" />
                <rect v-else-if="role === 'seed'" x="-6" y="-6" width="12" height="12" rx="2" :fill="roleColor.seed.fill" />
                <path v-else :d="shapePath(role, 7)" :fill="roleColor[role].fill" :stroke="role === 'backward' ? roleColor.backward.stroke : undefined" stroke-width="1.5" />
              </svg>{{ u(`node_${role}`) }}
            </li>
          </ul>
          <div class="row end" style="gap: 6px">
            <span class="xs muted num" style="margin-right: 4px">{{ u("nodesEdges", { nodes: citationMap.nodes?.length || 0, edges: citationMap.edges?.length || 0 }) }}<template v-if="metricsLine"> · {{ metricsLine }}</template></span>
            <UiSelect v-if="seeds.length" v-model="focusId" :options="seedOptions" :label="u('seedFocus')" size="sm" inline chevron="down" width="160px" />
            <div class="btn-group">
              <button class="btn btn-outline btn-icon btn-sm" type="button" :aria-label="u('zoomOut')" @click="zoom = clampZoom(zoom / 1.12)"><Icon name="minus" /></button>
              <button class="btn btn-outline btn-sm" type="button" @click="resetGraph">{{ u("reset") }}</button>
              <button class="btn btn-outline btn-icon btn-sm" type="button" :aria-label="u('zoomIn')" @click="zoom = clampZoom(zoom * 1.12)"><Icon name="plus" /></button>
            </div>
          </div>
        </div>
        <svg class="graph" data-testid="citation-graph" :viewBox="`0 0 ${GRAPH_WIDTH} ${GRAPH_HEIGHT}`" role="img" :aria-label="u('relations')" @wheel.prevent="onWheel" @pointerdown="onPointerDown" @pointermove="onPointerMove" @pointerup="onPointerUp" @pointercancel="onPointerUp">
          <g data-testid="citation-viewport" :data-zoom="zoom" :transform="`translate(${pan.x} ${pan.y}) scale(${zoom})`">
            <line v-for="edge in edges" :key="`${edge.source}-${edge.target}`" :x1="point(edge.source).x" :y1="point(edge.source).y" :x2="point(edge.target).x" :y2="point(edge.target).y" :stroke="connected(edge) ? '#000' : '#e5e5e5'" :stroke-width="connected(edge) ? 1.75 : 1.25" />
            <circle v-if="selected && nodes.some((node) => node.id === selectedId)" :cx="point(selectedId).x" :cy="point(selectedId).y" :r="radius(selected) + 6" fill="none" stroke="#000" stroke-opacity=".2" stroke-width="6" />
            <g
              v-for="node in nodes"
              :key="node.id"
              data-testid="graph-node"
              :data-node-id="node.id"
              :data-role="primaryRole(node)"
              :transform="`translate(${point(node.id).x} ${point(node.id).y})`"
              tabindex="0"
              role="button"
              :aria-label="`${t('selectNode')}: ${node.title}`"
              :aria-pressed="selectedId === node.id"
              @pointerdown.stop="onNodePointerDown($event, node.id)"
              @pointerenter="hoveredId = node.id"
              @pointerleave="hoveredId = ''"
              @focus="focusedNodeId = node.id"
              @blur="focusedNodeId = ''"
              @keydown="onNodeKeydown($event, node.id)"
            >
              <circle :r="Math.max(radius(node) + 8, 18)" fill="transparent" />
              <circle v-if="primaryRole(node) === 'result'" :r="radius(node)" :fill="roleColor.result.fill" :stroke="roleColor.result.stroke" stroke-width="2" />
              <rect v-else-if="primaryRole(node) === 'seed'" :x="-radius(node)" :y="-radius(node)" :width="radius(node) * 2" :height="radius(node) * 2" rx="3" :fill="roleColor.seed.fill" :stroke="roleColor.seed.stroke" stroke-width="2" />
              <path v-else :d="shapePath(primaryRole(node), radius(node))" :fill="roleColor[primaryRole(node)].fill" :stroke="roleColor[primaryRole(node)].stroke" stroke-width="1.5" />
              <text v-if="nodeLabel(node)" x="0" :y="radius(node) + 14" text-anchor="middle" font-family="Geist Variable, sans-serif" font-size="11" fill="#525252">{{ nodeLabel(node) }}</text>
            </g>
          </g>
        </svg>
      </section>
      <aside class="stack" style="flex: 1 1 300px; gap: 12px; min-width: 0" :aria-label="u('selectedNode')">
        <section v-if="selected" class="card c" data-testid="citation-detail">
          <div class="card-header"><div>
            <h2 class="card-title" style="font-size: 15px">{{ selected.title }}</h2>
            <p class="card-desc">{{ [selected.source ? sourceName(selected.source, selected.source) : "", selected.year, u("citations", { n: selected.citations || 0 })].filter((value) => value !== "" && value != null).join(" · ") }}</p>
          </div></div>
          <div class="card-content stack" style="gap: 12px">
            <dl class="kv">
              <dt>{{ u("role") }}</dt><dd>{{ u(`node_${primaryRole(selected)}`) }}</dd>
              <dt>{{ u("score") }}</dt><dd data-testid="citation-detail-score"><span v-if="selected.score != null" class="badge" :class="selected.score >= 8 ? '' : selected.score >= 5 ? 'badge-secondary' : 'badge-outline'">{{ selected.score }}/10</span><span v-else class="badge badge-outline muted">{{ u("unscored") }}</span></dd>
            </dl>
            <p v-if="selected.reasoning" class="sm" style="color: #404040"><span class="medium">{{ u("reasoning") }} · </span>{{ selected.reasoning }}</p>
          </div>
          <div v-if="isRecordLink(selected.id)" class="card-footer"><a class="btn btn-outline btn-sm" :href="selected.id" target="_blank" rel="noopener">{{ u("record") }}<Icon name="arrow-up-right" /></a></div>
        </section>
        <details class="card flush" data-testid="citation-text-list">
          <summary class="row" style="cursor: pointer; padding: 12px 16px; justify-content: space-between; flex-wrap: nowrap"><span class="sm medium">{{ u("textList") }}</span><Icon name="chevron-down" class="muted" /></summary>
          <div class="list" style="padding: 0 6px 6px; max-height: 360px; overflow-y: auto">
            <button v-for="node in nodes" :key="`text-${node.id}`" class="list-row" :class="{ active: selectedId === node.id }" type="button" :aria-pressed="selectedId === node.id" @click="selectNode(node.id)">
              <span class="xs muted" style="width: 56px; flex-shrink: 0">{{ u(`node_${primaryRole(node)}`) }}</span><span class="grow sm truncate">{{ node.title }}</span>
            </button>
          </div>
        </details>
      </aside>
    </div>
  </component>
</template>
