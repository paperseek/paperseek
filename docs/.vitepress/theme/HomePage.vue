<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useData, withBase } from 'vitepress'
import HeroPainting from './HeroPainting.vue'
import FeatureSection from './features/FeatureSection.vue'
import SearchDemo from './features/SearchDemo.vue'
import BiblioDemo from './features/BiblioDemo.vue'
import ReviewDemo from './features/ReviewDemo.vue'
import { pageSnap } from './features/pageSnap'

const { lang } = useData()
const zh = computed(() => lang.value.startsWith('zh'))
const t = (en: string, cn: string) => zh.value ? cn : en
const link = (path: string) => withBase((zh.value ? '/zh/' : '/') + path)
// On large screens the home page moves one section at a time (features/features.css, features/pageSnap.ts).
let stopSnap: (() => void) | undefined
onMounted(() => { document.documentElement.classList.add('ps-snap'); stopSnap = pageSnap() })
onBeforeUnmount(() => { document.documentElement.classList.remove('ps-snap'); stopSnap?.() })
const sources = computed(() => ['OpenAlex', 'arXiv', 'Crossref', 'Semantic Scholar', 'PubMed', 'Google Scholar', 'OpenReview', 'Web of Science',
  t('Wanfang Data', '万方数据'), t('NSSD', '国家哲社科期刊数据库'), t('Chinese Medical Journals', '中华医学期刊库')])
// The names stay on one row and scroll only when the row is wider than the page.
const strip = ref<HTMLElement>()
const scrolling = ref(false)
let stripObserver: ResizeObserver | undefined
onMounted(() => {
  const measure = () => {
    const set = strip.value?.querySelector<HTMLElement>('.ps-sources-set')
    if (strip.value && set) scrolling.value = set.scrollWidth > strip.value.clientWidth
  }
  // Watch the names too: web fonts can widen them after the strip has its size.
  stripObserver = new ResizeObserver(measure)
  for (const element of [strip.value, strip.value?.querySelector('.ps-sources-set')]) if (element) stripObserver.observe(element)
  measure()
})
onBeforeUnmount(() => stripObserver?.disconnect())
</script>

<template>
  <main class="ps-home" id="main-content">
    <section class="ps-hero" aria-labelledby="hero-heading">
      <HeroPainting />
      <div class="ps-hero-body ps-container">
        <div>
          <a class="ps-release" :href="link('changelog')"><span class="ps-release-dot" />{{ t('Introducing PaperSeek 1.0.0-beta5', '认识 PaperSeek 1.0.0-beta5') }}<span aria-hidden="true">↗</span></a>
          <h1 id="hero-heading">{{ t('Follow the question.', '从问题出发，') }}<br>{{ t('Find the evidence.', '找到研究的线索。') }}</h1>
        </div>
        <div class="ps-hero-side">
          <p class="ps-hero-description">{{ t('Turn your research question into a reading list, explore how papers connect, and bring the evidence together.', '把研究问题变成值得阅读的文献清单，沿着引用发现新的线索，再将分散的证据整理成综述。') }}</p>
          <div class="ps-actions">
            <a class="ps-button ps-button-primary" href="https://paperseek.app/">{{ t('Start researching', '开始研究') }}<span aria-hidden="true">↗</span></a>
            <a class="ps-button" :href="link('get-started/local')">{{ t('Run it yourself', '在本地运行') }}<span aria-hidden="true">→</span></a>
          </div>
        </div>
      </div>
      <a ref="strip" class="ps-sources" :class="{ 'is-scrolling': scrolling }" :href="link('guide/sources')" :aria-label="t('Supported scholarly sources', '支持的学术数据源')">
        <div class="ps-sources-track">
          <div v-for="copy in (scrolling ? 2 : 1)" :key="copy" class="ps-sources-set" :aria-hidden="copy > 1 || undefined">
            <span v-for="source in sources" :key="source">{{ source }}</span>
          </div>
        </div>
      </a>
    </section>

    <div class="ps-features">
      <FeatureSection id="feature-search" index="01" :eyebrow="t('Literature search', '文献检索')"
        :title="t('Ask a question. Get a reading list.', '提出问题，得到一份阅读清单。')"
        :body="t('PaperSeek turns your question into search strategies, refines them over several rounds, follows citations, and ranks every candidate with a reason you can read.', 'PaperSeek 把研究问题转化为检索式，多轮迭代、沿引用扩展，再为每篇候选文献评分，并写明排序理由。')"
        :points="[t('Eleven scholarly sources, from OpenAlex to Wanfang Data', '十一个学术数据源，从 OpenAlex 到万方数据'), t('Every stage of the search visible while it runs', '检索的每个阶段都实时可见'), t('A score and a written reason for each paper', '每篇文献都有评分和排序理由')]"
        :href="link('guide/search')" :link-text="t('How search works', '了解检索流程')">
        <SearchDemo :zh="zh" />
      </FeatureSection>
      <FeatureSection id="feature-biblio" index="02" :eyebrow="t('Bibliometrics', '文献计量')" reverse
        :title="t('See the shape of the field.', '看清一个领域的全貌。')"
        :body="t('Analyse the whole retrieved corpus, not only the top results: publication trends, venues, authors and countries, with keyword topics and bibliographic coupling generated on demand.', '分析实际获取的完整候选集，而不只是排在前面的结果：发表趋势、期刊、作者与国家地区，并按需生成关键词主题与文献耦合网络。')"
        :points="[t('Distributions across years, venues, authors and countries', '年份、期刊、作者与国家地区分布'), t('Keyword co-occurrence networks', '关键词共现网络'), t('Shared-reference networks between papers', '基于共同参考文献的文献耦合网络')]"
        :href="link('guide/bibliometrics')" :link-text="t('Explore bibliometrics', '了解文献计量')">
        <BiblioDemo :zh="zh" />
      </FeatureSection>
      <FeatureSection id="feature-review" index="03" :eyebrow="t('Literature review', '文献综述')" :badge="t('Experimental', '实验性')"
        :title="t('From evidence to a draft you can check.', '从证据到可核对的综述初稿。')"
        :body="t('Choose the papers. PaperSeek quotes evidence from each abstract, proposes an outline for you to confirm, then writes section by section. Every citation leads back to the sentence it came from.', '选定文献后，PaperSeek 从每篇摘要中摘取证据，生成供你确认的大纲，再逐节撰写正文。每条引用都能追溯到出处的原句。')"
        :points="[t('Evidence quoted from each abstract and checked against it', '证据摘自每篇摘要，并与原文核对'), t('An outline you review before any writing starts', '先确认大纲，再开始写作'), t('Export to Word, Markdown or BibTeX', '导出为 Word、Markdown 或 BibTeX')]"
        :href="link('guide/reviews')" :link-text="t('Meet the review workflow', '了解综述流程')"
        :note="t('Animated illustrations of the workflow. Your results depend on the question, source and model you choose.', '流程动画示意。实际结果取决于研究问题、数据源和所选模型。')">
        <ReviewDemo :zh="zh" />
      </FeatureSection>
    </div>

    <section class="ps-choice-section">
      <div class="ps-container">
        <div class="ps-section-heading"><div><p class="ps-eyebrow">{{ t('MAKE IT YOURS', '选择适合你的方式') }}</p><h2>{{ t('Your research.', '你的研究，') }}<br><span>{{ t('Your way of working.', '用你习惯的方式。') }}</span></h2></div><p>{{ t('Use PaperSeek in your browser, or keep the workspace on your own machine. The same search foundation, with a different home for your data.', '直接打开在线版，或在自己的设备上运行。相同的检索基础，不同的数据存储与模型配置方式。') }}</p></div>
        <div class="ps-choice-grid">
          <article class="ps-choice"><div class="ps-choice-icon" aria-hidden="true">↗</div><p class="ps-eyebrow">PAPERSEEK ONLINE</p><h3>{{ t('Open a tab. Follow an idea.', '打开浏览器，开始探索。') }}</h3><p>{{ t('Sign in for PaperSeek Service with free daily credits during the public beta, or use your own model key. History and reviews stay with your account.', '登录后使用 PaperSeek 服务，公测期间每日免费积分；也可以使用自己的模型密钥。检索历史和综述随账户保存。') }}</p><a class="ps-button ps-button-primary" href="https://paperseek.app/">{{ t('Open PaperSeek', '打开 PaperSeek') }} ↗</a><a class="ps-text-link" :href="link('get-started/online')">{{ t('Read the online guide', '阅读在线版指南') }} →</a></article>
          <article class="ps-choice"><div class="ps-choice-icon" aria-hidden="true">⌘</div><p class="ps-eyebrow">COMMUNITY EDITION</p><h3>{{ t('A workspace of your own.', '在自己的设备上研究。') }}</h3><p>{{ t('Run the open-source app locally or with Docker. Bring your model credentials and keep history in local storage.', '本地安装或使用 Docker 部署。配置自己的模型，把历史与文件保存在自己管理的存储中。') }}</p><a class="ps-button" :href="link('get-started/local')">{{ t('Install Community', '安装社区版') }} →</a><a class="ps-text-link" :href="link('developers/skill')">{{ t('Use the Agent Skill', '使用 Agent Skill') }} →</a></article>
        </div>
      </div>
    </section>

    <section class="ps-section ps-container ps-resources" aria-labelledby="resources-heading">
      <div><p class="ps-eyebrow">{{ t('A GOOD PLACE TO START', '从这里继续') }}</p><h2 id="resources-heading">{{ t('A little guidance.', '一点指引，') }}<br><span>{{ t('A lot to discover.', '更多发现。') }}</span></h2><p>{{ t('Learn the workflow at your own pace, then make it part of your research practice.', '按自己的节奏了解工作流程，再把 PaperSeek 融入日常研究。') }}</p><a class="ps-text-link" href="https://github.com/paperseek/paperseek">{{ t('Built in the open. Join us on GitHub.', '在开放中成长。欢迎在 GitHub 参与贡献。') }} ↗</a></div>
      <div class="ps-resource-links">
        <a :href="link('get-started/local')"><span><small>01</small><strong>{{ t('Your first search', '开始第一次检索') }}</strong><em>{{ t('Installation, configuration, and your first question.', '安装、配置，提出你的第一个问题。') }}</em></span><b aria-hidden="true">↗</b></a>
        <a :href="link('guide/models')"><span><small>02</small><strong>{{ t('Find your model setup', '选择模型与排序方式') }}</strong><em>{{ t('Local fusion, embedding, reranking, and decision models.', '理解本地融合、Embedding、Rerank 与决策模型。') }}</em></span><b aria-hidden="true">↗</b></a>
        <a :href="link('reference/troubleshooting')"><span><small>03</small><strong>{{ t('Get unstuck', '遇到问题时') }}</strong><em>{{ t('Practical answers to common search and setup questions.', '查找检索、配置与写作中的常见问题。') }}</em></span><b aria-hidden="true">↗</b></a>
      </div>
    </section>
  </main>
</template>
