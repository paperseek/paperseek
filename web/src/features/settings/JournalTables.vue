<script setup lang="ts">
import { onMounted, ref } from "vue";
import { ApiError, api, type JournalPlatform, type JournalTableStatus } from "../../api/client";
import Icon from "../../components/Icon.vue";
import { toast } from "../../components/toast";
import { useUi, useUiError } from "../../locales/ui";
import { useDates } from "../workspace/format";

const u = useUi();
const uiError = useUiError();
const { ago } = useDates();
// Only a server that keeps ranking tables (Community) answers; elsewhere the section stays hidden.
const tables = ref<JournalTableStatus[] | null>(null);
const busy = ref<JournalPlatform | "all" | "">("");
const inputs: Partial<Record<JournalPlatform, HTMLInputElement | null>> = {};

async function load() {
  try {
    tables.value = await api.journalTables();
  } catch {
    tables.value = null;
  }
}
async function refresh() {
  busy.value = "all";
  try {
    tables.value = await api.refreshJournalTables();
    toast(u("journalTablesRefreshed"), { tone: "ok" });
  } catch (error) {
    toast(uiError(error instanceof ApiError ? error.code : "request_failed"), { tone: "fail" });
    await load();
  } finally {
    busy.value = "";
  }
}
async function importFile(platform: JournalPlatform, event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = "";
  if (!file) return;
  busy.value = platform;
  try {
    const saved = await api.importJournalTable(platform, file);
    toast(u("journalTableImportedToast", { n: saved.journals.toLocaleString() }), { tone: "ok" });
    await load();
  } catch (error) {
    toast(uiError(error instanceof ApiError ? error.code : "request_failed"), { tone: "fail" });
  } finally {
    busy.value = "";
  }
}
onMounted(load);
</script>

<template>
  <section v-if="tables" class="card c" aria-labelledby="journal-tables-title" style="margin-top: 24px" data-testid="journal-tables">
    <div class="card-header">
      <div><h2 id="journal-tables-title" class="card-title">{{ u("journalTablesTitle") }}</h2><p class="help" style="margin-top: 4px">{{ u("journalTablesDesc") }}</p></div>
      <button class="btn btn-outline btn-sm" type="button" style="height: 28px; flex-shrink: 0" :disabled="busy !== ''" @click="refresh"><Icon :name="busy === 'all' ? 'loader' : 'download'" />{{ u("refreshTables") }}</button>
    </div>
    <div class="card-content stack" style="gap: 0">
      <div v-for="table in tables" :key="table.platform" class="setrow">
        <span class="stack" style="gap: 2px">
          <span class="sm medium">{{ u(`journalPlatform_${table.platform}`) }}</span>
          <span v-if="table.imported" class="xs muted num">{{ [u("journalTableImported", { n: table.journals.toLocaleString() }), table.year ? u("journalTableYear", { year: table.year }) : "", u(`journalTableSource_${table.source}`), ago(table.imported_at)].filter(Boolean).join(" · ") }}</span>
          <span v-else class="xs muted">{{ u("journalTableNone") }}</span>
        </span>
        <div class="row" style="gap: 2px; flex-wrap: nowrap">
          <input :ref="(element) => (inputs[table.platform] = element as HTMLInputElement | null)" type="file" accept=".csv,text/csv" class="sr-only" tabindex="-1" :aria-label="u('importTable')" @change="importFile(table.platform, $event)" />
          <button class="btn btn-ghost btn-sm" type="button" style="height: 28px" :disabled="busy !== ''" @click="inputs[table.platform]?.click()"><Icon v-if="busy === table.platform" name="loader" />{{ u("importTable") }}</button>
        </div>
      </div>
    </div>
  </section>
  <p v-if="tables" class="xs muted" style="margin-top: 8px">{{ u("journalTablesFoot") }}</p>
</template>
