import { defineConfig, type DefaultTheme } from 'vitepress'

const base = process.env.DOCS_BASE || '/'

type PageLink = [en: string, zh: string, path: string]
type SubGroup = [en: string, zh: string, pages: PageLink[]]
type SidebarSection = [en: string, zh: string, entries: (PageLink | SubGroup)[]]

const sections: SidebarSection[] = [
  ['Getting started', '开始使用', [
    ['Introduction', '认识 PaperSeek', 'get-started/'],
    ['Quick start online', '在线快速开始', 'get-started/online'],
    ['Quick start local', '本地快速开始', 'get-started/local']
  ]],
  ['User guides', '使用指南', [
    ['Overview', '概览', 'guide/'],
    ['Research workflow', '检索工作流', [
      ['Search', '文献检索', 'guide/search'],
      ['Results & history', '结果与历史', 'guide/results'],
      ['Citation maps', '引用图谱', 'guide/citation-map'],
      ['Bibliometrics', '文献计量', 'guide/bibliometrics'],
      ['Reviews · Experimental', '综述 · 实验性', 'guide/reviews']
    ]],
    ['Setup', '配置', [
      ['Data sources', '数据源', 'guide/sources'],
      ['Models & ranking', '模型与排序', 'guide/models']
    ]],
    ['Workspace & history', '工作区与历史', 'guide/workspace']
  ]],
  ['PaperSeek online', '在线服务', [
    ['Overview', '概览', 'online/'],
    ['Your account', '账户与登录', 'online/account'],
    ['Service & credits', '服务与积分', 'online/credits'],
    ['Your own keys', '自有密钥', 'online/byok'],
    ['Data & privacy', '数据与隐私', 'online/privacy']
  ]],
  ['Self-hosting', '自部署', [
    ['Deployment', '部署与运维', 'self-hosting/'],
    ['Upgrade to 1.0.0-beta5', '升级至 1.0.0-beta5', 'self-hosting/upgrade']
  ]],
  ['Developers', '开发者', [
    ['Integrations', '集成接口', [
      ['Overview', '概览', 'developers/'],
      ['Command line', '命令行', 'developers/cli'],
      ['HTTP API', 'HTTP API', 'developers/api'],
      ['MCP', 'MCP', 'developers/mcp'],
      ['Agent Skill', 'Agent Skill', 'developers/skill']
    ]],
    ['Contribute', '参与贡献', [
      ['Contributing', '贡献指南', 'developers/contributing'],
      ['Architecture', '内部架构', 'developers/architecture'],
      ['Interface guidelines', '界面规范', 'developers/design-system']
    ]]
  ]],
  ['Reference & help', '参考与帮助', [
    ['Configuration', '配置参考', 'reference/configuration'],
    ['Troubleshooting', '故障排查', 'reference/troubleshooting']
  ]],
  ['Changelog', '更新日志', [
    ['All releases', '版本记录', 'changelog']
  ]]
]

function isSubGroup(entry: PageLink | SubGroup): entry is SubGroup {
  return Array.isArray(entry[2])
}

function navigation(zh = false): DefaultTheme.Config {
  const prefix = zh ? '/zh/' : '/'
  const toItem = ([en, cn, path]: PageLink): DefaultTheme.SidebarItem => ({ text: zh ? cn : en, link: prefix + path })
  const sidebar: DefaultTheme.SidebarItem[] = sections.map(([en, cn, entries]) => ({
    text: zh ? cn : en,
    collapsed: false,
    items: entries.map(entry => isSubGroup(entry)
      ? { text: zh ? entry[1] : entry[0], collapsed: false, items: entry[2].map(toItem) }
      : toItem(entry))
  }))
  return {
    nav: [
      { text: zh ? '开始使用' : 'Get started', link: `${prefix}get-started/`, activeMatch: `^${prefix}get-started/` },
      { text: zh ? '使用指南' : 'Guides', link: `${prefix}guide/`, activeMatch: `^${prefix}(guide|online|self-hosting|reference)/` },
      { text: zh ? '开发者' : 'Developers', link: `${prefix}developers/`, activeMatch: `^${prefix}developers/` },
      { text: zh ? '更新日志' : 'Changelog', link: `${prefix}changelog` },
      { text: zh ? '打开 PaperSeek' : 'Open PaperSeek', link: 'https://paperseek.app/' }
    ],
    sidebar,
    editLink: { pattern: 'https://github.com/paperseek/paperseek/edit/main/docs/:path', text: zh ? '在 GitHub 上编辑此页' : 'Edit this page on GitHub' },
    outline: { label: zh ? '本页目录' : 'On this page', level: [2, 3] },
    docFooter: { prev: zh ? '上一页' : 'Previous page', next: zh ? '下一页' : 'Next page' },
    lastUpdated: { text: zh ? '最后更新' : 'Last updated' },
    returnToTopLabel: zh ? '回到顶部' : 'Back to top',
    sidebarMenuLabel: zh ? '目录' : 'Menu',
    darkModeSwitchLabel: zh ? '外观' : 'Appearance',
    lightModeSwitchTitle: zh ? '切换为浅色' : 'Switch to light theme',
    darkModeSwitchTitle: zh ? '切换为深色' : 'Switch to dark theme',
    langMenuLabel: zh ? '语言' : 'Language',
    skipToContentLabel: zh ? '跳到正文' : 'Skip to content',
    footer: { message: zh ? '开放源码，自由探索。Apache-2.0 许可。' : 'Open source. Open to exploration. Apache-2.0 licensed.', copyright: '© 2026 PaperSeek contributors' }
  }
}

export default defineConfig({
  title: 'PaperSeek',
  description: 'From a research question to a reading list. Open-source literature search, citation maps, and abstract-based reviews.',
  base,
  cleanUrls: true,
  lastUpdated: true,
  srcExclude: ['README.md', 'public/**/*.md'],
  outDir: process.env.DOCS_OUT_DIR,
  cacheDir: process.env.DOCS_CACHE_DIR,
  sitemap: { hostname: 'https://docs.paperseek.app' },
  markdown: { theme: { light: 'github-light', dark: 'github-dark' } },
  head: [
    ['link', { rel: 'icon', type: 'image/svg+xml', href: `${base}paperseek-mark.svg` }],
    ['meta', { name: 'theme-color', content: '#fafafa' }],
    ['meta', { property: 'og:type', content: 'website' }],
    ['meta', { property: 'og:site_name', content: 'PaperSeek' }]
  ],
  transformHead({ pageData }) {
    const path = pageData.relativePath.replace(/index\.md$/, '').replace(/\.md$/, '')
    return [['link', { rel: 'canonical', href: `https://docs.paperseek.app/${path}` }]]
  },
  locales: {
    root: { label: 'English', lang: 'en', themeConfig: navigation() },
    zh: { label: '简体中文', lang: 'zh-CN', description: '从研究问题到阅读清单。开源文献检索、引用图谱与基于摘要的文献综述。', themeConfig: navigation(true) }
  },
  themeConfig: {
    logo: { light: '/paperseek-lockup.svg', dark: '/paperseek-lockup-dark.svg', alt: 'PaperSeek' },
    siteTitle: false,
    socialLinks: [{ icon: 'github', link: 'https://github.com/paperseek/paperseek' }],
    search: { provider: 'local', options: { miniSearch: { options: {
      tokenize(text) {
        return Array.from(new Intl.Segmenter('zh', { granularity: 'word' }).segment(text))
          .filter(part => part.isWordLike).map(part => part.segment)
      }
    } }, locales: { zh: { translations: {
      button: { buttonText: '搜索文档', buttonAriaLabel: '搜索文档' },
      modal: { displayDetails: '显示详情', resetButtonTitle: '清除', backButtonTitle: '返回', noResultsText: '未找到相关内容', footer: { selectText: '选择', navigateText: '切换', closeText: '关闭' } }
    } } } } }
  }
})
