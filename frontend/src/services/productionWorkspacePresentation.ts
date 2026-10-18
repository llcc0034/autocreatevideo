import type { GeneratedArtifact, StageRun } from './platformTypes'
// UI grouping only. Available actions, object kinds and execution rules remain server-owned.
export const productionWorkspaceSections = [
  { key: 'script', label: '剧本', description: '导入、生成与确认本轮剧本', stages: ['script_input', 'script_generate', 'script_structure', 'script_review'] },
  { key: 'visual', label: '视觉与术语', description: '全剧视觉规则与统一名称', stages: ['visual_rules_generate', 'visual_rules_review'] },
  { key: 'bible', label: 'Bible 与参考素材', description: '人物、场景、道具及参考图', stages: ['bible_materialize', 'bible_review', 'bible_image_generation', 'bible_image_review'] },
  { key: 'storyboard', label: '剧集与分镜', description: '核对镜头内容与参考素材', stages: ['storyboard', 'storyboard_review'] },
  { key: 'media', label: '媒体与选片', description: '媒体候选与选用结果', stages: ['media_generation', 'artifact_selection'] },
  { key: 'delivery', label: '装配与交付', description: '逐集装配、核对与封存', stages: ['assembly', 'delivery'] },
  { key: 'records', label: '任务与记录', description: '找回执行准备、查看实际状态', stages: [] },
] as const

export type ProductionWorkspaceSection = typeof productionWorkspaceSections[number]['key']

export function workspaceSectionIncludes(section: ProductionWorkspaceSection, stageKey: string): boolean {
  const row = productionWorkspaceSections.find(item => item.key === section)
  return row ? (row.stages as readonly string[]).includes(stageKey) : false
}

export function workspaceSection(value: unknown): ProductionWorkspaceSection {
  return productionWorkspaceSections.find(row => row.key === value)?.key ?? 'script'
}

// Unknown/custom steps remain visible in records; never guess their meaning from a name.
export function workspaceStageSection(stageKey: string): ProductionWorkspaceSection {
  return productionWorkspaceSections.find(row => (row.stages as readonly string[]).includes(stageKey))?.key ?? 'records'
}

export function workspaceArtifactSection(artifact: GeneratedArtifact, stages: StageRun[] = []): ProductionWorkspaceSection {
  const source = stages.find(stage => stage.id === artifact.stageRun)
  if (source && workspaceSectionIncludes('bible', source.stageKey)) return 'bible'
  return artifact.kind === 'assembly' ? 'delivery' : 'media'
}
