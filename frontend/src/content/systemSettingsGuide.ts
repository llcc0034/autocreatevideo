import rawGuide from './systemSettingsGuide.json'
import type { SettingsSectionId } from './settingsSectionCatalog'

export type GuideStep = {
  title: string
  action: string
  expected: string
  ifBlocked: string
}

export type GuideFieldNote = { term: string; meaning: string }
export type GuideCodeExample = { title: string; language: string; code: string }
export type GuideMistake = { symptom: string; fix: string }

export type GuideComponentNode = {
  id: string
  label: string
  english: string
  detail: string
  sectionId?: SettingsSectionId
  guideAnchor?: string
  optional: boolean
}

export type GuideComponentStage = {
  number: number
  title: string
  summary: string
  success: string
  next: string
  nodes: GuideComponentNode[]
}

export type GuideComponentEdge = {
  from: string
  to: string
  label: string
  optional: boolean
}

export type GuideProductionFlowStep = {
  number: number
  id: string
  label: string
  english: string
  detail: string
  sectionId?: SettingsSectionId
  guideAnchor?: string
}

export type SystemSettingsGuideSection = {
  id: SettingsSectionId
  summary: string
  image: string
  imageAlt: string
  imageCaption: string
  purpose: string[]
  before: string[]
  steps: GuideStep[]
  codeExamples?: GuideCodeExample[]
  fieldNotes: GuideFieldNote[]
  mistakes: GuideMistake[]
}

export type SystemSettingsGuide = {
  title: string
  subtitle: string
  version: string
  updatedAt: string
  audience: string[]
  permissionNotes: string[]
  overviewImage: { image: string; alt: string; caption: string }
  statusSemantics: Array<{ label: string; meaning: string }>
  quickStart: Array<{ number: number; title: string; detail: string; sectionId: SettingsSectionId }>
  componentFlow: {
    title: string
    description: string
    legend: string[]
    stages: GuideComponentStage[]
    edges: GuideComponentEdge[]
    notes: string[]
    caption: string
  }
  productionFlow: {
    title: string
    description: string
    steps: GuideProductionFlowStep[]
    notes: string[]
    caption: string
  }
  sections: SystemSettingsGuideSection[]
  caseStudy: {
    title: string
    objective: string
    facts: string[]
    preflight: Array<{ sectionId: SettingsSectionId; title: string; checks: string[] }>
    runSteps: string[]
    result: string[]
    caveats: string[]
    image: string
    imageAlt: string
    imageCaption: string
  }
  troubleshooting: Array<{ symptom: string; cause: string; actions: string[] }>
  finalChecklist: string[]
  glossary: Array<{ chinese: string; english: string; meaning: string }>
}

export const systemSettingsGuide = rawGuide as SystemSettingsGuide

export const systemSettingsGuideSection = (id: SettingsSectionId) =>
  systemSettingsGuide.sections.find((section) => section.id === id)!

export const searchableSettingsGuideText = () => JSON.stringify(systemSettingsGuide).toLocaleLowerCase('zh-CN')
