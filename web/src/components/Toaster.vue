<script setup lang="ts">
import Icon from "./Icon.vue";
import { dismiss, toasts } from "./toast";
import { useUi } from "../locales/ui";

const u = useUi();
</script>

<template>
  <section class="toasts" aria-live="polite" :aria-label="u('notifications')">
    <div v-for="item in toasts" :key="item.id" class="toast" role="status">
      <Icon v-if="item.tone !== 'default'" :name="item.tone" />
      <span class="grow">{{ item.message }}</span>
      <button v-if="item.action" class="btn btn-outline btn-sm" type="button" @click="item.action.run(); dismiss(item.id)">{{ item.action.label }}</button>
    </div>
  </section>
</template>
