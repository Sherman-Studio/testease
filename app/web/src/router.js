import { createRouter, createWebHistory } from 'vue-router'
import Admin from './views/Admin.vue'
import Discovered from './views/Discovered.vue'
import MCPTools from './views/MCPTools.vue'
import NewRun from './views/NewRun.vue'
import Personas from './views/Personas.vue'
import Settings from './views/Settings.vue'
import PersonaDetail from './views/PersonaDetail.vue'
import RunDetail from './views/RunDetail.vue'
import RunsList from './views/RunsList.vue'
import SiteModel from './views/SiteModel.vue'
import SiteTarget from './views/SiteTarget.vue'

const routes = [
  // The onboarding redesign makes Sites the home: the journey is
  // add a site → configure it → run personas → review. `/` redirects there;
  // the Runs list moves to /runs (RunDetail already lives under /runs/:id).
  { path: '/', redirect: '/site' },
  // Sites — register + browse the targets and their site model.
  { path: '/site', name: 'site', component: SiteModel },
  {
    path: '/site/:targetId',
    name: 'site-target',
    component: SiteTarget,
    props: true,
  },
  { path: '/personas', name: 'personas', component: Personas },
  {
    path: '/personas/:personaId',
    name: 'persona',
    component: PersonaDetail,
    props: true,
  },
  { path: '/runs', name: 'runs', component: RunsList },
  { path: '/runs/:runId', name: 'run', component: RunDetail, props: true },
  { path: '/new-run', name: 'new-run', component: NewRun },
  { path: '/discovered', name: 'discovered', component: Discovered },
  { path: '/settings', name: 'settings', component: Settings },
  { path: '/mcp-tools', name: 'mcp-tools', component: MCPTools },
  { path: '/admin', name: 'admin', component: Admin },
  // Old bookmarks still resolve: scenario presets live in the New Run console
  // now, and transcript search lives inside each run's Timeline tab.
  { path: '/scenarios', redirect: '/new-run' },
  { path: '/transcripts', redirect: '/runs' },
]

export default createRouter({
  history: createWebHistory(),
  routes,
})
