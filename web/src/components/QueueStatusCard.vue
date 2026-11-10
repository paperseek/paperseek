<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { api } from "../api/client";
import type { RunDetail } from "../api/types";
import Icon from "./Icon.vue";
import { useUi } from "../locales/ui";

const props = defineProps<{ run: RunDetail; busy?: boolean; cancelLabel?: string }>();
const emit = defineEmits<{ cancel: []; update: [run: RunDetail] }>();
const u = useUi();
const snapshot = ref(props.run);
const disconnected = ref(false);
const now = ref(Date.now());
const queue = computed(() => snapshot.value.queue);
const waited = computed(() => {
  const seconds = Math.max(0, Math.floor((now.value - Date.parse(props.run.created_at)) / 1000));
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
});
let controller: AbortController | undefined;
let timer: ReturnType<typeof setTimeout> | undefined;
const clock = setInterval(() => (now.value = Date.now()), 1000);
function stop() {
  controller?.abort();
  if (timer) clearTimeout(timer);
}
// Separate sources: an array getter would fire on every replaced run object and restart the poll.
watch([() => props.run.id, () => props.run.status], ([id, status]) => {
  stop();
  snapshot.value = props.run;
  disconnected.value = false;
  if (status !== "queued") return;
  const current = new AbortController();
  controller = current;
  async function poll() {
    try {
      const value = await api.run(id, current.signal);
      if (current.signal.aborted) return;
      snapshot.value = value;
      disconnected.value = false;
      emit("update", value);
      if (value.status !== "queued") return;
    } catch {
      if (current.signal.aborted) return;
      disconnected.value = true;
    }
    timer = setTimeout(poll, 3000);
  }
  void poll();
}, { immediate: true });
onBeforeUnmount(() => {
  stop();
  clearInterval(clock);
});
</script>

<template>
  <section class="card c" role="status" aria-labelledby="queue-title" data-testid="queue-status">
    <div class="card-content row" style="gap: 16px; flex-wrap: wrap">
      <span class="empty-media" style="margin: 0"><Icon name="clock" size="lg" /></span>
      <div class="stack grow" style="gap: 2px">
        <h2 id="queue-title" class="card-title">{{ u("queueTitle") }}</h2>
        <p class="help" style="margin-top: 2px">{{ disconnected ? u("reconnecting") : u("queueHint") }}</p>
      </div>
      <dl class="row" style="gap: 24px; margin: 0">
        <div class="stack"><dt class="xs muted">{{ u("queueAhead") }}</dt><dd class="medium num" style="margin: 0; font-size: 18px; line-height: 28px">{{ queue ? queue.ahead : "—" }}</dd></div>
        <div class="stack"><dt class="xs muted">{{ u("queueRunning") }}</dt><dd class="medium num" style="margin: 0; font-size: 18px; line-height: 28px">{{ queue ? queue.running : "—" }}</dd></div>
        <div class="stack"><dt class="xs muted">{{ u("queueWaited") }}</dt><dd class="medium num" style="margin: 0; font-size: 18px; line-height: 28px">{{ waited }}</dd></div>
      </dl>
      <button class="btn btn-outline btn-sm danger" type="button" :disabled="busy" @click="emit('cancel')"><Icon v-if="busy" name="loader" />{{ cancelLabel || u("cancelQueue") }}</button>
    </div>
  </section>
</template>
