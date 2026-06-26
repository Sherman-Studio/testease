// Shared Vitest setup — runs before every test file.
//
// happy-dom doesn't ship a full canvas/image stack. The Avatar tests
// generate SVG data URIs and assert on string shape rather than actual
// rendering, so no shim is needed today. If a future test mounts a
// component that calls `Image()` or `canvas.getContext('2d')` we'll add
// stubs here.

// Silence Vue Router warnings when a test mounts a component containing
// <router-link> without a real router. Tests stub router via global mocks
// per-test where the link's behaviour matters.
import { config } from '@vue/test-utils'

config.global.stubs = {
  // Default-stub <router-link> so unrelated components mount cleanly.
  // A test that cares about the link does `config.global.stubs['router-link']
  // = false` then mounts with a real router.
  'router-link': {
    template: '<a :href="to"><slot /></a>',
    props: ['to'],
  },
}
