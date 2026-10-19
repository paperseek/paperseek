<script setup lang="ts">
// Settings › User: clear saved keys or all history, each behind a typed confirmation.
import { computed, ref } from "vue";
import { json } from "../../api/client";
import type { components } from "../../api/generated";
import { toast } from "../../components/toast";
import { useUi } from "../../locales/ui";
import { invalidateHistory, resetWorkspace } from "../workspace/state";
import { loadSettings, workspaceSettings } from "./store";
import TypedConfirmDialog from "./TypedConfirmDialog.vue";

type Summary = components["schemas"]["WorkspaceSummary"];
type ClearResult = components["schemas"]["HistoryClearResult"];

// Hosted words the key note for its own-key mode and adds that credit records are kept.
const props = defineProps<{ keysAfter?: string; historyNote?: string; countKeys?: boolean; keys?: { connections: number; sources: number } }>();
const emit = defineEmits<{ cleared: [what: "keys" | "history"] }>();
const u = useUi();
const open = ref<"" | "keys" | "history">("");
const busy = ref(false);
const error = ref("");
const status = ref("");
const summary = ref<Summary | null>(null);
const keyCounts = computed(() => props.keys ?? {
  connections: workspaceSettings.connections.length,
  sources: workspaceSettings.sources.filter((item) => item.key_origin === "settings").length,
});
const keysDescription = computed(() => u("clearKeysConfirm", keyCounts.value) + (props.keysAfter ?? u("keysAfter"))
  + (workspaceSettings.proxy?.origin === "settings" ? u("clearKeysProxy") : "") + u("irreversible"));
const historyDescription = computed(() => {
  const value = summary.value;
  if (!value) return "";
  return u("clearHistoryConfirm", { searches: value.searches, analyses: value.analyses, reviews: value.reviews })
    + (value.active ? u("runningStop") : "") + (props.historyNote || "") + u("irreversible");
});

function close() {
  if (busy.value) return;
  open.value = "";
  error.value = status.value = "";
}
async function openHistory() {
  error.value = status.value = "";
  try {
    summary.value = await json<Summary>("/api/v1/data/summary");
    open.value = "history";
  } catch {
    toast(u("actionFailed"), { tone: "fail" });
  }
}
async function clearKeys() {
  busy.value = true;
  error.value = "";
  try {
    await json("/api/v1/settings/secrets", { method: "DELETE" });
    await loadSettings(true);
    busy.value = false;
    close();
    toast(u("keysCleared"), { tone: "ok" });
    emit("cleared", "keys");
  } catch {
    error.value = u("actionFailed");
  } finally {
    busy.value = false;
  }
}
async function clearHistory() {
  busy.value = true;
  error.value = "";
  try {
    // Running tasks are stopped first; the request reports "stopping" until none remain.
    for (let attempt = 0; attempt < 60; attempt++) {
      const result = await json<ClearResult>("/api/v1/data/history/clear", { method: "POST" });
      if (result.status === "cleared") {
        resetWorkspace();
        for (const module of ["search", "bibliometrics", "reviews"] as const) invalidateHistory(module);
        busy.value = false;
        close();
        toast(u("historyCleared"), { tone: "ok" });
        emit("cleared", "history");
        return;
      }
      status.value = u("stoppingTasks", { n: result.active ?? 0 });
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
    error.value = u("stopTimeout");
  } catch {
    error.value = u("actionFailed");
  } finally {
    busy.value = false;
    status.value = "";
  }
}
</script>

<template>
  <section class="card c" aria-labelledby="data-title">
    <div class="card-header"><div><h2 id="data-title" class="card-title">{{ u("dataManagement") }}</h2><p class="help" style="margin-top: 4px">{{ u("dataManagementDesc") }}</p></div></div>
    <div class="card-content stack" style="gap: 0">
      <div class="setrow">
        <span class="stack" style="gap: 2px"><span class="sm medium">{{ u("clearKeys") }}</span><span class="xs muted">{{ countKeys ? u("clearKeysCountDesc", keyCounts) : u("clearKeysDesc") }}</span></span>
        <button class="btn btn-outline btn-sm danger" type="button" style="height: 28px" @click="open = 'keys'">{{ u("clearKeysAction") }}</button>
      </div>
      <div class="setrow">
        <span class="stack" style="gap: 2px"><span class="sm medium">{{ u("clearHistory") }}</span><span class="xs muted">{{ u("clearHistoryDesc") }}{{ historyNote || "" }}</span></span>
        <button class="btn btn-outline btn-sm danger" type="button" style="height: 28px" @click="openHistory">{{ u("clearHistoryAction") }}</button>
      </div>
    </div>
    <TypedConfirmDialog v-if="open === 'keys'" :title="u('clearKeysTitle')" :description="keysDescription" :phrase="u('phraseKeys')" :action="u('clearKeysAction')" :busy="busy" :error="error" @confirm="clearKeys" @close="close" />
    <TypedConfirmDialog v-if="open === 'history'" :title="u('clearHistoryTitle')" :description="historyDescription" :phrase="u('phraseHistory')" :action="u('clearHistoryAction')" :busy="busy" :status="status" :error="error" @confirm="clearHistory" @close="close" />
  </section>
</template>
