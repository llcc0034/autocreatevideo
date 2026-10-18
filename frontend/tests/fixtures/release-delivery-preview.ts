import { createApp } from 'vue'
import Preview from './ReleaseDeliveryPreview.vue'
document.addEventListener('error', event => {
  if (event.target instanceof HTMLVideoElement) console.error('Isolated media load diagnostic', event.target.error?.code, event.target.error?.message, event.target.currentSrc)
}, true)
createApp(Preview).mount('#app')
