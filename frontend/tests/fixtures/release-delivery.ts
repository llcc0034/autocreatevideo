export const releaseId = (n: number) => `20000000-0000-0000-0000-${String(n).padStart(12,'0')}`
export const releaseIdentity = { bundleId: releaseId(1), runId: releaseId(2), projectId: releaseId(3) }
export const releaseDocument = () => ({
  schema: 'autoprompt-production-release-bundle-v3', delivery_scope: 'episode_masters', bundle_id: releaseId(1),
  production_run_id: releaseId(2), project_id: releaseId(3), title: '隔离交付播放验证',
  verification: { status:'verified', signature_verified:true, live_verified:true, verified_at:'2026-10-06T01:20:00+08:00' },
  integrity: { payload_sha256:'a'.repeat(64), signature:'explicit-isolated-signature-not-live-evidence' },
  selection: { shots:[{shot_id:releaseId(4),artifact_id:releaseId(5)}], episodes:[{episode_id:releaseId(6),artifact_id:releaseId(7)}],series_artifact_id:null },
  assemblies: [{episode_id:releaseId(6),generated_artifact_id:releaseId(7),assembly_id:releaseId(8),scope:'episode',artifact_sha256:'b'.repeat(64)}],
  artifacts: [{generated_artifact_id:releaseId(5), explicit_fixture:'full-source-document-preserved'}],
})
export const releaseArtifact = () => ({id:releaseId(7),productionRun:releaseId(2),kind:'assembly',status:'available',sha256:'b'.repeat(64),
  sizeBytes:100,objectKey:'isolated/episode-master.mp4',mimeType:'video/mp4',previewUrl:'/isolated-preview',downloadUrl:'/isolated-download'})
