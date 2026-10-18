<script setup lang="ts">
import { ref } from 'vue'
import ManualDeliveryReview from '@/components/ManualDeliveryReview.vue'
import ProductionReleaseDelivery from '@/components/ProductionReleaseDelivery.vue'
import { platformApi } from '@/services/platformApi'
import { releaseIdentity, releaseDocument, releaseArtifact, releaseId } from './release-delivery'
const config = JSON.parse(document.querySelector('#fixture-data')!.textContent!)
const failed = ref(false), reads = ref(0)
const run = { id: releaseIdentity.runId, project: releaseIdentity.projectId, status: 'awaiting_input', planSource: 'manual', executionPlan: { sha256: 'a'.repeat(64) } } as any
const options = { runId: run.id, runPlanSha256: run.executionPlan.sha256, actions: [] } as any
platformApi.getManualDelivery = async () => ({
  run_id: run.id, run_plan_sha256: run.executionPlan.sha256, contract_sha256: 'a'.repeat(64), blockers: ['隔离只读夹具：不允许封存'], bundle: null,
  contract: { schema_version: 1, kind: 'manual_delivery', run_plan_sha256: run.executionPlan.sha256, assembly_contract_sha256: 'a'.repeat(64), selection_revision: 1,
    episodes: [{ episode_id: releaseId(6), number: 1, title: '同一已有媒体母版', assembly_job_id: releaseId(8), artifact_id: releaseId(7),
      sha256: config.sha, manifest_sha256: 'a'.repeat(64), size_bytes: config.size, mime_type: 'video/mp4' }] },
})
platformApi.getVerifiedReleaseDocument = async () => {
  if (failed.value) throw Error('隔离夹具：母版文件不可用')
  const doc = releaseDocument(); doc.assemblies[0]!.artifact_sha256 = config.sha; return doc
}
platformApi.getArtifact = async () => {
  reads.value++
  if (failed.value) throw Error('隔离夹具：403 母版文件不可用')
  return { ...releaseArtifact(), sha256: config.sha, sizeBytes: config.size, manifestMetadataSha256: 'a'.repeat(64),
    objectKey: 'isolated/master.mp4', previewUrl: config.url, downloadUrl: config.url } as any
}
</script>
<template>
  <main>
    <h1>M8-06 · 双入口共用母版读取</h1>
    <p>原人工与交付组件；隔离 API、既有 FFmpeg 测试片。不是现用 Run 装配或审核。</p>
    <label><input v-model="failed" type="checkbox"> 模拟母版读取失败</label>
    <p>确切产物读取次数：{{ reads }}</p>
    <ManualDeliveryReview :run="run" :options="options" />
    <hr>
    <ProductionReleaseDelivery :bundle-id="releaseIdentity.bundleId" :run-id="releaseIdentity.runId" :project-id="releaseIdentity.projectId" />
  </main>
</template>
