<template>
  <div class="flex min-h-screen">
    <!--
      Desktop sidebar — #1822 collapsed the old 7-item nav to the three
      destinations that map to the real loop (history → trigger →
      registry) plus a ⚙ utility menu for the reference/maintenance
      pages. Collapse state persists in localStorage under
      ``testease.nav.collapsed`` (#1078 Slice 0) so it survives reloads.
    -->
    <aside
      class="sticky top-0 hidden h-screen shrink-0 flex-col border-r border-hairline/[0.06] bg-ink-50/80 py-5 backdrop-blur transition-all duration-150 lg:flex"
      :class="navCollapsed ? 'w-14 px-1' : 'w-56 px-3'"
      data-testid="desktop-sidebar"
    >
      <SidebarBrand :collapsed="navCollapsed" />
      <nav class="flex-1 space-y-0.5">
        <router-link
          v-for="item in NAV"
          :key="item.to"
          :to="item.to"
          class="nav-row"
          :class="{ active: isActive(item), 'nav-row-collapsed': navCollapsed }"
          :title="navCollapsed ? item.label : undefined"
        >
          <component :is="item.icon" class="h-4 w-4 shrink-0" />
          <span v-if="!navCollapsed" class="flex-1 truncate">{{ item.label }}</span>
          <span
            v-if="!navCollapsed && item.prefix === '/runs' && activeRunBadge"
            class="flex items-center gap-1 text-[10px] font-medium text-emerald-300"
          >
            <span class="lamp lamp-live"></span>
            live
          </span>
          <span
            v-else-if="navCollapsed && item.prefix === '/runs' && activeRunBadge"
            class="lamp lamp-live"
            aria-label="Run in progress"
          ></span>
        </router-link>
      </nav>

      <ActiveRunBadge
        v-if="activeRunBadge && !navCollapsed"
        :run-id="activeRunBadge"
        :pod-count="activeRunPodCount"
      />

      <!-- ⚙ utility menu — MCP tools · Admin · API docs.
           Reference and maintenance surfaces, demoted out of the main nav. -->
      <div class="relative mt-3">
        <button
          class="nav-row w-full"
          :class="{
            active: utilityActive,
            'nav-row-collapsed': navCollapsed,
          }"
          :title="navCollapsed ? 'Utilities' : undefined"
          data-testid="utility-menu-toggle"
          @click="utilityOpen = !utilityOpen"
        >
          <IconGear class="h-4 w-4 shrink-0" />
          <span v-if="!navCollapsed" class="flex-1 truncate text-left">Utilities</span>
          <svg
            v-if="!navCollapsed"
            viewBox="0 0 24 24"
            class="h-3 w-3 transition-transform"
            :class="utilityOpen ? 'rotate-180' : ''"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
          >
            <path d="M6 15l6-6 6 6" />
          </svg>
        </button>
        <div
          v-if="utilityOpen"
          class="absolute bottom-full left-0 z-30 mb-1 w-48 rounded-lg border border-hairline/10 bg-panel p-1 shadow-xl"
          data-testid="utility-menu"
        >
          <router-link
            v-for="item in UTILITY"
            :key="item.to"
            :to="item.to"
            class="nav-row !py-1.5 text-xs"
            :class="{ active: route.path.startsWith(item.to) }"
          >
            <component :is="item.icon" class="h-3.5 w-3.5 shrink-0" />
            {{ item.label }}
          </router-link>
          <a
            href="/docs"
            target="_blank"
            rel="noopener"
            class="nav-row !py-1.5 text-xs hover:no-underline"
          >
            <IconDocs class="h-3.5 w-3.5 shrink-0" />
            API docs ↗
          </a>
        </div>
      </div>

      <button
        class="mt-1 flex items-center justify-center gap-2 rounded-md py-1.5 text-xs text-ink-500 transition hover:bg-ink-100 hover:text-ink-800"
        :title="theme === 'light' ? 'Switch to the dark theme' : 'Switch to the light theme'"
        :aria-label="theme === 'light' ? 'Switch to the dark theme' : 'Switch to the light theme'"
        data-testid="theme-toggle"
        @click="toggleTheme"
      >
        <component :is="theme === 'light' ? IconMoon : IconSun" class="h-4 w-4 shrink-0" />
        <span v-if="!navCollapsed">{{ theme === 'light' ? 'Dark theme' : 'Light theme' }}</span>
      </button>
      <button
        class="mt-1 flex items-center justify-center gap-2 rounded-md py-1.5 text-xs text-ink-500 transition hover:bg-ink-100 hover:text-ink-800"
        :title="navCollapsed ? 'Expand sidebar' : 'Collapse sidebar'"
        :aria-label="navCollapsed ? 'Expand sidebar' : 'Collapse sidebar'"
        data-testid="nav-collapse-toggle"
        @click="toggleNavCollapsed"
      >
        <svg
          viewBox="0 0 24 24"
          class="h-4 w-4 transition-transform"
          :class="navCollapsed ? '' : 'rotate-180'"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
        >
          <path d="M9 6l6 6-6 6" />
        </svg>
        <span v-if="!navCollapsed">Collapse</span>
      </button>
    </aside>

    <!-- Mobile drawer + backdrop. Mounted only when open so the hidden
         drawer's links aren't reachable via keyboard tab order. -->
    <Teleport to="body">
      <div
        v-if="mobileNavOpen"
        class="fixed inset-0 z-40 bg-black/60 lg:hidden"
        @click="closeMobileNav"
      >
        <aside
          class="flex h-full w-64 max-w-[80vw] flex-col border-r border-hairline/10 bg-ink-50 px-3 py-5 shadow-xl"
          @click.stop
        >
          <button
            class="self-end rounded-md p-1 text-ink-500 hover:bg-ink-100"
            aria-label="Close menu"
            @click="closeMobileNav"
          >
            <svg viewBox="0 0 24 24" class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M6 6l12 12M6 18L18 6" />
            </svg>
          </button>
          <SidebarBrand class="mt-2" />
          <nav class="flex-1 space-y-0.5">
            <router-link
              v-for="item in NAV"
              :key="item.to"
              :to="item.to"
              class="nav-row"
              :class="{ active: isActive(item) }"
            >
              <component :is="item.icon" class="h-4 w-4 shrink-0" />
              <span class="flex-1 truncate">{{ item.label }}</span>
              <span
                v-if="item.prefix === '/runs' && activeRunBadge"
                class="flex items-center gap-1 text-[10px] font-medium text-emerald-300"
              >
                <span class="lamp lamp-live"></span>
                live
              </span>
            </router-link>
            <div class="my-2 border-t border-hairline/[0.06] pt-2">
              <div class="px-3 pb-1 text-[10px] uppercase tracking-wider text-ink-500">
                Utilities
              </div>
              <router-link
                v-for="item in UTILITY"
                :key="item.to"
                :to="item.to"
                class="nav-row"
                :class="{ active: route.path.startsWith(item.to) }"
              >
                <component :is="item.icon" class="h-4 w-4 shrink-0" />
                {{ item.label }}
              </router-link>
              <a href="/docs" target="_blank" rel="noopener" class="nav-row hover:no-underline">
                <IconDocs class="h-4 w-4 shrink-0" />
                API docs ↗
              </a>
              <button class="nav-row w-full" @click="toggleTheme">
                <component :is="theme === 'light' ? IconMoon : IconSun" class="h-4 w-4 shrink-0" />
                {{ theme === 'light' ? 'Dark theme' : 'Light theme' }}
              </button>
            </div>
          </nav>
          <ActiveRunBadge
            v-if="activeRunBadge"
            :run-id="activeRunBadge"
            :pod-count="activeRunPodCount"
          />
        </aside>
      </div>
    </Teleport>

    <main class="flex min-h-screen min-w-0 flex-1 flex-col">
      <!-- Mobile-only top bar with the hamburger. -->
      <header
        class="sticky top-0 z-20 flex items-center gap-2 border-b border-hairline/[0.06] bg-ink-50/90 px-4 py-2 backdrop-blur lg:hidden"
      >
        <button
          class="rounded-md p-1 text-ink-700 hover:bg-ink-100"
          aria-label="Open menu"
          @click="mobileNavOpen = true"
        >
          <svg viewBox="0 0 24 24" class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M4 6h16M4 12h16M4 18h16" />
          </svg>
        </button>
        <div class="flex items-center gap-2">
          <BrandMark class="h-6 w-6" />
          <span class="font-display text-sm font-semibold leading-none text-ink-900">Test Ease</span>
        </div>
        <span
          v-if="activeRunBadge"
          class="ml-auto flex items-center gap-1 text-[10px] font-medium text-emerald-300"
        >
          <span class="lamp lamp-live"></span>
          live
        </span>
      </header>

      <div class="flex-1">
        <router-view />
      </div>
      <footer class="mx-auto mt-12 w-full max-w-7xl border-t border-hairline/[0.06] px-6 py-4 text-xs text-ink-500">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <span>
            Test Ease
            <span v-if="version" class="text-ink-400">· v{{ version }}</span>
            <span class="text-ink-400"> · Persona QA workbench</span>
          </span>
          <span class="flex flex-wrap items-center gap-x-3">
            <a href="/docs" target="_blank" rel="noopener">API docs</a>
            <a
              href="https://github.com/mccullya/slyreply/issues/new?labels=area:testease"
              target="_blank"
              rel="noopener"
              title="File a bug or request against testease itself"
            >
              Report an issue
            </a>
          </span>
        </div>
      </footer>
    </main>
  </div>
</template>

<script setup>
import { onMounted, onUnmounted, ref, h, watch, computed } from 'vue'
import { useRoute, RouterLink } from 'vue-router'
import { getActiveRun } from './api.js'

const route = useRoute()

// Tiny inline icon components — outline 24×24, no external icon dep.
const Icon = (path) =>
  h(
    'svg',
    {
      viewBox: '0 0 24 24',
      fill: 'none',
      stroke: 'currentColor',
      'stroke-width': '1.75',
      'stroke-linecap': 'round',
      'stroke-linejoin': 'round',
    },
    [h('path', { d: path })],
  )

const IconRuns = () => Icon('M4 6h16M4 12h16M4 18h10')
const IconLaunch = () => Icon('M6 4l12 8-12 8z')
const IconPersonas = () =>
  Icon('M16 11a4 4 0 1 0-8 0 4 4 0 0 0 8 0zM3 21v-1a6 6 0 0 1 12 0v1m3-4a4 4 0 0 0-3-3.87')
const IconDiscovered = () =>
  Icon('M12 3v4M12 17v4M3 12h4M17 12h4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M5.6 18.4l2.8-2.8M15.6 8.4l2.8-2.8')
const IconMCP = () =>
  Icon('M14.7 6.3a4 4 0 0 1-5.4 5.4l-5.6 5.6a2 2 0 0 0 2.8 2.8l5.6-5.6a4 4 0 0 1 5.4-5.4l-2.8 2.8 1 3 3 1 2.8-2.8a4 4 0 0 1-6.8 6.8z')
const IconAdmin = () =>
  Icon('M12 2l8 4v6c0 5-3.5 9-8 10-4.5-1-8-5-8-10V6l8-4zM7 10l4 4 6-6')
const IconDocs = () => Icon('M4 4h16v12H4zM4 20h10M8 8h8M8 12h6')
const IconSun = () =>
  Icon('M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM12 2v2m0 16v2M4.2 4.2l1.4 1.4m12.8 12.8 1.4 1.4M2 12h2m16 0h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4')
const IconMoon = () => Icon('M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z')
const IconGear = () =>
  Icon('M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM12 2v2m0 16v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4')
const IconSite = () =>
  Icon('M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM3 12h18M12 3c2.5 2.4 3.8 5.6 3.8 9s-1.3 6.6-3.8 9c-2.5-2.4-3.8-5.6-3.8-9s1.3-6.6 3.8-9z')

// Primary destinations, ordered as the operator journey: add a site →
// configure it → run personas → review. Sites is home.
const NAV = [
  { to: '/site', label: 'Sites', icon: IconSite, prefix: '/site' },
  { to: '/personas', label: 'Personas', icon: IconPersonas, prefix: '/personas' },
  { to: '/runs', label: 'Runs', icon: IconRuns, prefix: '/runs' },
  { to: '/new-run', label: 'New Run', icon: IconLaunch, prefix: '/new-run' },
  { to: '/discovered', label: 'Discovered', icon: IconDiscovered, prefix: '/discovered' },
]
// Reference + maintenance surfaces.
const UTILITY = [
  { to: '/mcp-tools', label: 'MCP tools', icon: IconMCP },
  { to: '/admin', label: 'Admin', icon: IconAdmin },
]

function isActive(item) {
  return route.path.startsWith(item.prefix)
}
const utilityActive = computed(() =>
  UTILITY.some((u) => route.path.startsWith(u.to)),
)

// Brand mark — a status lamp inside a rounded square: the glyph of the
// whole redesign (lamps everywhere a run can be alive).
const BrandMark = {
  setup(_, { attrs }) {
    return () =>
      h(
        'div',
        {
          class: [
            attrs.class,
            'flex items-center justify-center rounded-md border border-brand-500/40 bg-brand-50 text-brand-700',
          ],
          style: 'box-shadow: 0 0 14px -4px rgba(39,194,228,0.6)',
        },
        [
          h(
            'svg',
            {
              viewBox: '0 0 24 24',
              class: 'h-3.5 w-3.5',
              fill: 'none',
              stroke: 'currentColor',
              'stroke-width': '2.5',
              'stroke-linecap': 'round',
              'stroke-linejoin': 'round',
            },
            [h('path', { d: 'M5 12l4 4 10-10' })],
          ),
        ],
      )
  },
}

const SidebarBrand = {
  props: {
    class: { default: 'mb-6' },
    collapsed: { type: Boolean, default: false },
  },
  setup(props, { attrs }) {
    return () =>
      h(
        RouterLink,
        {
          to: '/',
          class: [
            attrs.class || 'mb-6',
            'flex items-center gap-2 text-ink-900 hover:no-underline',
            props.collapsed ? 'justify-center px-0' : 'px-2',
          ],
          title: props.collapsed ? 'Test Ease — persona QA' : undefined,
        },
        () => [
          h(BrandMark, { class: 'h-8 w-8 shrink-0' }),
          !props.collapsed &&
            h('div', null, [
              h(
                'div',
                { class: 'font-display text-sm font-semibold leading-tight' },
                'Test Ease',
              ),
              h(
                'div',
                { class: 'text-[10px] uppercase tracking-wider text-ink-500' },
                'persona QA',
              ),
            ]),
        ],
      )
  },
}

const ActiveRunBadge = {
  // #1821 — podCount folds the N pods of a multi-pod run (N labelled Jobs,
  // Option B) into the badge. Defaults to 1 so a single-pod run renders
  // without a pod line.
  props: {
    runId: { type: String, required: true },
    podCount: { type: Number, default: 1 },
  },
  setup(props) {
    return () =>
      h(
        'div',
        {
          class:
            'mt-3 rounded-md border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-xs',
        },
        [
          h('div', { class: 'flex items-center gap-1.5 font-semibold text-emerald-300' }, [
            h('span', { class: 'lamp lamp-live' }),
            'Run in progress',
          ]),
          props.podCount > 1
            ? h(
                'div',
                {
                  class: 'text-emerald-400',
                  'data-testid': 'active-run-pod-count',
                },
                `${props.podCount} pods running`,
              )
            : null,
          h(
            RouterLink,
            {
              to: `/runs/${props.runId}`,
              class: 'font-mono text-emerald-400 hover:underline',
            },
            () => `${props.runId} →`,
          ),
        ],
      )
  },
}

// #1822 — light/dark theme. Dark ("control room") is the default; the
// choice persists under ``testease.theme`` and is applied pre-paint by
// the boot script in index.html (keep key + colours in sync with it).
const THEME_KEY = 'testease.theme'
const theme = ref('dark')
try {
  theme.value = localStorage.getItem(THEME_KEY) === 'light' ? 'light' : 'dark'
} catch {
  /* localStorage unavailable — dark default */
}
function toggleTheme() {
  theme.value = theme.value === 'light' ? 'dark' : 'light'
  try {
    localStorage.setItem(THEME_KEY, theme.value)
  } catch {
    /* ignore — applies in-session only */
  }
  const el = document.documentElement
  el.dataset.theme = theme.value
  el.style.backgroundColor = theme.value === 'light' ? '#eef1f6' : '#07090e'
}

// #1078 Slice 0 — desktop nav collapse state, persisted to localStorage.
const NAV_COLLAPSED_KEY = 'testease.nav.collapsed'
const navCollapsed = ref(false)
try {
  navCollapsed.value = localStorage.getItem(NAV_COLLAPSED_KEY) === '1'
} catch {
  /* localStorage unavailable (SSR, privacy mode) — fall through */
}
function toggleNavCollapsed() {
  navCollapsed.value = !navCollapsed.value
  try {
    localStorage.setItem(NAV_COLLAPSED_KEY, navCollapsed.value ? '1' : '0')
  } catch {
    /* ignore — state still updates in-session, just won't persist */
  }
}

// ⚙ menu + mobile drawer both close on navigation.
const utilityOpen = ref(false)
const mobileNavOpen = ref(false)
function closeMobileNav() {
  mobileNavOpen.value = false
}
watch(
  () => route.path,
  () => {
    mobileNavOpen.value = false
    utilityOpen.value = false
  },
)

// Version pulled from the OpenAPI spec so the footer reflects the
// running API exactly. Failures fall through to null.
const version = ref(null)
;(async () => {
  try {
    const r = await fetch('/openapi.json')
    if (!r.ok) return
    const spec = await r.json()
    version.value = spec?.info?.version || null
  } catch {
    /* offline / no API — fine, footer just won't show version */
  }
})()

// Active-run polling — once every 5s, lightweight.
const activeRunBadge = ref(null)
const activeRunPodCount = ref(1)
let pollTimer = null

async function refresh() {
  try {
    const active = await getActiveRun()
    activeRunBadge.value = active?.run_id || null
    activeRunPodCount.value = active?.pod_count || 1
  } catch {
    activeRunBadge.value = null
    activeRunPodCount.value = 1
  }
}

onMounted(() => {
  refresh()
  pollTimer = setInterval(refresh, 5000)
})
onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
})
</script>
