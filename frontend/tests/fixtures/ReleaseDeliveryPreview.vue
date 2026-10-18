<script setup lang="ts">
import { ref } from 'vue'
import Delivery from '@/components/ProductionReleaseDelivery.vue'
import { platformApi } from '@/services/platformApi'
import { releaseIdentity, releaseDocument, releaseArtifact } from './release-delivery'
const failed = ref(false), reads = ref(0)
const config = JSON.parse(document.querySelector('#fixture-data')!.textContent!)
platformApi.getVerifiedReleaseDocument = async () => {
  reads.value++
  if (failed.value) throw Error('隔离夹具：母版文件不可用，停止交付')
  const doc=releaseDocument(); doc.assemblies[0]!.artifact_sha256=config.sha; return doc
}
platformApi.getArtifact = async () => ({...releaseArtifact(),sha256:config.sha,sizeBytes:config.size,objectKey:'batch322/master.mp4',
  previewUrl:config.url,downloadUrl:config.url}) as any
</script>
<template>
  <main>
    <h1>M8-05 · 原交付组件隔离验证</h1>
    <p>隔离API响应 + 本地FFmpeg测试片；不是现用Run交付或真实模型验收。</p>
    <label><input v-model="failed" type="checkbox"> 模拟文件缺失</label>
    <p>清单读取次数：{{ reads }}</p>
    <Delivery :bundle-id="releaseIdentity.bundleId" :run-id="releaseIdentity.runId" :project-id="releaseIdentity.projectId" />
  </main>
</template>
