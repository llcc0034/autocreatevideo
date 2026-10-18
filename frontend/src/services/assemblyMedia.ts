import type { GeneratedArtifact } from './platformTypes'

export type AssemblyMediaIdentity = {
  artifactId: string
  sha256: string
  manifestSha256?: string
  sizeBytes?: number
}

// Both the pre-seal review and the sealed delivery read the same exact master.
export function verifyAssemblyMedia(artifact: GeneratedArtifact, expected: AssemblyMediaIdentity, runId: string): GeneratedArtifact {
  if (artifact.id !== expected.artifactId || artifact.productionRun !== runId || artifact.kind !== 'assembly'
      || !['available', 'selected'].includes(artifact.status) || artifact.sha256 !== expected.sha256
      || !Number.isSafeInteger(artifact.sizeBytes) || artifact.sizeBytes! <= 0 || !artifact.mimeType.startsWith('video/')
      || !artifact.objectKey || !artifact.previewUrl || !artifact.downloadUrl
      || (expected.manifestSha256 !== undefined && artifact.manifestMetadataSha256 !== expected.manifestSha256)
      || (expected.sizeBytes !== undefined && artifact.sizeBytes !== expected.sizeBytes)) {
    throw new Error('母版文件与所读清单不一致或不可用，不能播放或下载；请重新核对原装配任务')
  }
  return artifact
}
