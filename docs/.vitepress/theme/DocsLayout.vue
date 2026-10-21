<script setup lang="ts">
import { computed } from 'vue'
import { useData } from 'vitepress'
import DefaultTheme from 'vitepress/theme'

const { frontmatter, page } = useData()
const isDoc = computed(() => !page.value.isNotFound && (!frontmatter.value.layout || frontmatter.value.layout === 'doc'))
</script>

<template>
  <DefaultTheme.Layout class="ps-site-layout" :class="{ 'ps-docs-layout': isDoc }" />
</template>
