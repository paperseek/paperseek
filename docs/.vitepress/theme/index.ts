import DefaultTheme from 'vitepress/theme'
import type { Theme } from 'vitepress'
import HomePage from './HomePage.vue'
import DocsLayout from './DocsLayout.vue'
import StartOptions from './StartOptions.vue'
import '@fontsource-variable/geist'
import '@fontsource-variable/geist-mono'
import '@fontsource-variable/noto-sans-sc'
import './style.css'
import './features/features.css'
import './docs.css'
import './nav.css'

export default {
  extends: DefaultTheme,
  Layout: DocsLayout,
  enhanceApp({ app }) {
    app.component('HomePage', HomePage)
    app.component('StartOptions', StartOptions)
  }
} satisfies Theme
