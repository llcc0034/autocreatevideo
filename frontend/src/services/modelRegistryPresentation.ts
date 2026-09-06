import type {
  AcceptanceSmokeDeploymentRow,
  HostCapabilitySnapshot,
  ModelArtifact,
  ModelDeployment,
  ModelProfileVersion,
  ProviderProfile,
  RuntimeModelAvailability,
} from './platformTypes'

export const modelDeploymentWorkflowSmokeUrl = (
  row: AcceptanceSmokeDeploymentRow | null,
  deploymentId: string,
): string => row?.actions.find((action) => action.kind === 'view_workflow_associations')?.uiUrl
  || `/settings/acceptance-smoke?subject=workflow&deployment_id=${encodeURIComponent(deploymentId)}`

export type ModelArtifactAvailabilityEvidence = {
  snapshot: HostCapabilitySnapshot
  availability: RuntimeModelAvailability
  deployment: ModelDeployment | null
  exactContract: boolean
  verificationEligible: boolean
}

export type ModelRegistryVersionGroup = {
  profile: ModelProfileVersion
  artifacts: ModelArtifact[]
  deployments: ModelDeployment[]
}

export type ModelRegistryFamily = {
  key: string
  providerNames: string[]
  versions: ModelRegistryVersionGroup[]
}

export type ModelRegistryFamilyOptions = {
  search?: string
  showRetired?: boolean
}

const trustedObservationSources = new Set(['local_filesystem', 'remote_attestation'])

const registrySearchText = (values: unknown[]) => values
  .flatMap((value) => Array.isArray(value) ? value : [value])
  .map((value) => String(value ?? ''))
  .join(' ')
  .toLocaleLowerCase('zh-CN')

const profileSearchValues = (
  profile: ModelProfileVersion,
  provider: ProviderProfile | undefined,
  artifacts: readonly ModelArtifact[],
  deployments: readonly ModelDeployment[],
) => [
  profile.profileKey,
  profile.version,
  profile.modelIdentifier,
  profile.modality,
  profile.status,
  profile.specification.capabilities,
  provider?.key,
  provider?.name,
  provider?.driverType,
  ...artifacts.flatMap((item) => [
    item.logicalName,
    item.filename,
    item.storageUri,
    item.inventoryFolder,
    item.role,
    item.status,
  ]),
  ...deployments.flatMap((item) => [
    item.key,
    item.name,
    item.status,
    item.providerDriver,
    item.runtimeHost,
    item.latestSmoke?.status,
    item.latestSmoke?.unresolvedSubmission?.status,
  ]),
]

/**
 * Builds the model-centred presentation tree without changing the registry's
 * version, artifact or deployment ownership boundaries.
 */
export const buildModelRegistryFamilies = (
  profiles: readonly ModelProfileVersion[],
  artifacts: readonly ModelArtifact[],
  deployments: readonly ModelDeployment[],
  providers: readonly ProviderProfile[],
  options: ModelRegistryFamilyOptions = {},
): ModelRegistryFamily[] => {
  const query = String(options.search || '').trim().toLocaleLowerCase('zh-CN')
  const showRetired = options.showRetired === true
  const providerById = new Map(providers.map((item) => [item.id, item]))
  const visibleProfiles = profiles.filter((item) => showRetired || item.status !== 'retired')
  const visibleProfileIds = new Set(visibleProfiles.map((item) => item.id))
  const artifactsByProfile = new Map<string, ModelArtifact[]>()
  const deploymentsByProfile = new Map<string, ModelDeployment[]>()

  for (const artifact of artifacts) {
    if (!visibleProfileIds.has(artifact.profileVersion)) continue
    artifactsByProfile.set(artifact.profileVersion, [...(artifactsByProfile.get(artifact.profileVersion) || []), artifact])
  }
  for (const deployment of deployments) {
    if (!visibleProfileIds.has(deployment.profileVersion)) continue
    if (!showRetired && (deployment.status === 'retired' || deployment.status === 'disabled')) continue
    deploymentsByProfile.set(deployment.profileVersion, [...(deploymentsByProfile.get(deployment.profileVersion) || []), deployment])
  }

  const familyMap = new Map<string, ModelRegistryVersionGroup[]>()
  for (const profile of visibleProfiles) {
    const artifactsForProfile = artifactsByProfile.get(profile.id) || []
    const deploymentsForProfile = deploymentsByProfile.get(profile.id) || []
    const values = profileSearchValues(
      profile,
      providerById.get(profile.provider),
      artifactsForProfile,
      deploymentsForProfile,
    )
    if (query && !registrySearchText(values).includes(query)) continue
    familyMap.set(profile.profileKey, [
      ...(familyMap.get(profile.profileKey) || []),
      { profile, artifacts: artifactsForProfile, deployments: deploymentsForProfile },
    ])
  }

  return [...familyMap.entries()]
    .map(([key, versions]) => ({
      key,
      providerNames: [...new Set(versions.map(({ profile }) => {
        const provider = providerById.get(profile.provider)
        return provider?.name || provider?.key || `未知 Provider ${profile.provider.slice(0, 8)}`
      }))],
      versions: versions.sort((left, right) => right.profile.version - left.profile.version),
    }))
    .sort((left, right) => left.key.localeCompare(right.key, 'zh-CN'))
}

const isLoopbackDeployment = (
  deployment: ModelDeployment,
  modelProfileById: ReadonlyMap<string, ModelProfileVersion>,
  providerById: ReadonlyMap<string, ProviderProfile>,
) => {
  const profile = modelProfileById.get(deployment.profileVersion)
  const provider = profile ? providerById.get(profile.provider) : null
  const rawUrl = String(deployment.runtimeConfig.base_url || provider?.driverConfig.base_url || '').trim()
  if (!rawUrl) return false
  try {
    const hostname = new URL(rawUrl).hostname.toLocaleLowerCase('en-US').replace(/^\[|\]$/g, '')
    return hostname === 'localhost' || hostname === '::1' || hostname.startsWith('127.')
  } catch {
    return false
  }
}

export const modelArtifactAvailabilityEvidence = (
  artifact: ModelArtifact,
  snapshots: readonly HostCapabilitySnapshot[],
  deployments: readonly ModelDeployment[],
  now = Date.now(),
  modelProfiles: readonly ModelProfileVersion[] = [],
  providers: readonly ProviderProfile[] = [],
): { latest: ModelArtifactAvailabilityEvidence | null; verificationCandidate: ModelArtifactAvailabilityEvidence | null } => {
  const deploymentById = new Map(deployments.map((item) => [item.id, item]))
  const modelProfileById = new Map(modelProfiles.map((item) => [item.id, item]))
  const providerById = new Map(providers.map((item) => [item.id, item]))
  const latestSnapshotByHost = new Map<string, HostCapabilitySnapshot>()
  for (const snapshot of snapshots) {
    const current = latestSnapshotByHost.get(snapshot.host)
    const snapshotTime = Date.parse(snapshot.capturedAt)
    const currentTime = current ? Date.parse(current.capturedAt) : Number.NEGATIVE_INFINITY
    if (!current || snapshotTime > currentTime || (snapshotTime === currentTime && snapshot.revision > current.revision)) {
      latestSnapshotByHost.set(snapshot.host, snapshot)
    }
  }
  const evidence = [...latestSnapshotByHost.values()].flatMap((snapshot) => snapshot.modelAvailability
    .filter((availability) => availability.artifact === artifact.id)
    .map((availability): ModelArtifactAvailabilityEvidence => {
      const deployment = deploymentById.get(availability.deployment) || null
      const exactContract = artifact.sizeBytes !== null
        && Boolean(artifact.sha256)
        && availability.observedSizeBytes === artifact.sizeBytes
        && availability.observedSha256 === artifact.sha256
      const trustedSource = trustedObservationSources.has(availability.observationSource)
      const sourceMatchesHost = availability.observationSource !== 'local_filesystem'
        || (deployment !== null && isLoopbackDeployment(deployment, modelProfileById, providerById))
      const verificationEligible = Date.parse(snapshot.expiresAt) > now
        && snapshot.probeStatus !== 'failed'
        && availability.status === 'available'
        && exactContract
        && trustedSource
        && sourceMatchesHost
        && deployment?.profileVersion === artifact.profileVersion
        && deployment.runtimeHost === snapshot.host
      return { snapshot, availability, deployment, exactContract, verificationEligible }
    }))
    .sort((left, right) => Date.parse(right.snapshot.capturedAt) - Date.parse(left.snapshot.capturedAt))

  const latest = evidence[0] || null
  return {
    latest,
    // Each host is authoritative only for its own newest snapshot. A newer
    // missing fact on host B must not hide an exact, fresh fact on host A, while
    // an older snapshot from the same host can never be used as a fallback.
    verificationCandidate: evidence.find((item) => item.verificationEligible) || null,
  }
}
