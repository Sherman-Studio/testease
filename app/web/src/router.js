import { createRouter, createWebHistory } from 'vue-router'
import Admin from './views/Admin.vue'
import Discovered from './views/Discovered.vue'
import MCPTools from './views/MCPTools.vue'
import NewRun from './views/NewRun.vue'
import Personas from './views/Personas.vue'
import PersonaDetail from './views/PersonaDetail.vue'
import RunDetail from './views/RunDetail.vue'
import RunsList from './views/RunsList.vue'
import SiteModel from './views/SiteModel.vue'
import SiteTarget from './views/SiteTarget.vue'

const routes = [
  // #1822 — three primary destinations (Runs / New Run / Personas) plus
  // the ⚙ utility pages (Discovered / MCP tools / Admin).
  { path: '/', name: 'runs', component: RunsList },
  { path: '/new-run', name: 'new-run', component: NewRun },
  { path: '/runs/:runId', name: 'run', component: RunDetail, props: true },
  { path: '/personas', name: 'personas', component: Personas },
  {
    path: '/personas/:personaId',
    name: 'persona',
    component: PersonaDetail,
    props: true,
  },
  // Site Model — browse a target's map/plan + curate its by-design knowledge.
  { path: '/site', name: 'site', component: SiteModel },
  {
    path: '/site/:targetId',
    name: 'site-target',
    component: SiteTarget,
    props: true,
  },
  { path: '/discovered', name: 'discovered', component: Discovered },
  { path: '/mcp-tools', name: 'mcp-tools', component: MCPTools },
  { path: '/admin', name: 'admin', component: Admin },
  // #1822 retired two top-level pages. Old bookmarks still resolve:
  // scenario presets are saved/loaded from the New Run console now, and
  // transcript search lives inside each run's Timeline tab.
  { path: '/scenarios', redirect: '/new-run' },
  { path: '/transcripts', redirect: '/' },
]

export default createRouter({
  history: createWebHistory(),
  routes,
})
