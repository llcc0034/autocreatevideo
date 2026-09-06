import type { PromptArtifactPayload } from '../types/index.ts'

type RawPromptArtifact = Record<string, any>

/** Convert the persisted backend prompt artifact into the editor contract. */
export const adaptPromptArtifact = (raw: RawPromptArtifact): PromptArtifactPayload => {
  if (raw.prompt_type !== 'image' && raw.prompt_type !== 'video') {
    throw new Error('PromptArtifact 缺少有效 prompt_type')
  }
  const structured = raw.structured_payload || {}
  const fields = structured.fields || structured
  const profileAssembly = structured.prompt_profile && typeof structured.prompt_profile === 'object'
    ? structured.prompt_profile as RawPromptArtifact
    : null
  const profileFieldRules = Array.isArray(profileAssembly?.field_rules)
    ? profileAssembly.field_rules.flatMap((value: unknown) => {
      const rule = value && typeof value === 'object' ? value as RawPromptArtifact : null
      if (!rule || !rule.field_key || !rule.source_selector || !rule.channel || !Number.isInteger(Number(rule.position))) return []
      return [{
        fieldKey: String(rule.field_key),
        ...(rule.field_label ? { fieldLabel: String(rule.field_label) } : {}),
        sourceSelector: String(rule.source_selector),
        channel: String(rule.channel),
        position: Number(rule.position),
        labelStyle: String(rule.label_style || ''),
        separator: String(rule.separator || ''),
        maxChars: Number(rule.max_chars || 0),
        ...(rule.generation_mode ? { generationMode: String(rule.generation_mode) } : {}),
        ...(rule.generation_output_semantics ? {
          generationOutputSemantics: String(rule.generation_output_semantics),
        } : {}),
      }]
    })
    : []
  const promptProfileGuide = profileAssembly
    && profileAssembly.profile_key
    && profileAssembly.source_projection_key
    && Number.isInteger(Number(profileAssembly.version))
    ? {
      profileKey: String(profileAssembly.profile_key),
      name: String(profileAssembly.profile_name || profileAssembly.profile_key),
      version: Number(profileAssembly.version),
      sourceProjectionKey: String(profileAssembly.source_projection_key),
      ...(profileAssembly.official_reference ? {
        officialReference: String(profileAssembly.official_reference),
      } : {}),
      fieldRules: profileFieldRules,
    }
    : null
  const fieldText = (value: unknown) => Array.isArray(value)
    ? value.map((item) => String(item || '').trim()).filter(Boolean).join('\n')
    : String(value || '').trim()
  const mediaType: PromptArtifactPayload['mediaType'] = raw.prompt_type
  const durationMs = Number(raw.total_duration_ms || 0)
  if (typeof raw.include_audio !== 'boolean') {
    throw new Error('PromptArtifact 缺少有效 include_audio 冻结事实')
  }
  if (typeof raw.is_multi_shot !== 'boolean') {
    throw new Error('PromptArtifact 缺少有效 is_multi_shot 冻结事实')
  }
  if (mediaType === 'image' && raw.include_audio) {
    throw new Error('图片 PromptArtifact 不能包含音频')
  }
  const includeAudio = raw.include_audio
  const isMultiShot = raw.is_multi_shot
  const promptText = String(raw.prompt_text || '').trim()
  const model = String(raw.model || '').trim()
  if (!model) throw new Error('PromptArtifact 缺少显式模型标识')
  if (!promptText) throw new Error('PromptArtifact 缺少实际 prompt_text')
  if (mediaType === 'video') {
    if (!String(raw.conditioning_mode || '').trim()) {
      throw new Error('视频 PromptArtifact 缺少显式 conditioning_mode')
    }
    if (!String(structured.effective_conditioning_mode || '').trim()) {
      throw new Error('视频 PromptArtifact 缺少显式 effective_conditioning_mode')
    }
  }
  return {
    ...(typeof raw.compiler_current === 'boolean' ? { compiler_current: raw.compiler_current } : {}),
    ...(raw.id ? { artifact_id: String(raw.id) } : {}),
    ...(Number.isFinite(Number(raw.revision)) ? { artifact_revision: Number(raw.revision) } : {}),
    ...(raw.created_at ? { artifact_created_at: String(raw.created_at) } : {}),
    model,
    mediaType,
    includeAudio,
    isMultiShot,
    ...(mediaType === 'video' ? {
      mode: raw.conditioning_mode || '',
      ...(Array.isArray(structured.reference_bindings) ? { reference_bindings: structured.reference_bindings } : {}),
      ...(durationMs > 0 ? { duration: durationMs / 1000 } : {})
    } : {}),
    ...(structured.effective_conditioning_mode ? {
      effective_conditioning_mode: structured.effective_conditioning_mode
    } : {}),
    ...(structured.prompt_template_key ? {
      prompt_template_key: String(structured.prompt_template_key)
    } : {}),
    ...(promptProfileGuide ? { prompt_profile_guide: promptProfileGuide } : {}),
    prompt_text: promptText,
    integrated_multimodal_description: fieldText(fields.integrated_multimodal_description || fields.detailed_description || raw.prompt_text),
    ...(Array.isArray(fields.subject_definitions) ? { subject_definitions: fields.subject_definitions.map(String) } : {}),
    ...(fieldText(fields.summary) ? { summary: fieldText(fields.summary) } : {}),
    ...(Array.isArray(fields.retention_analysis) ? { retention_analysis: fields.retention_analysis.map(String) } : {}),
    ...(Array.isArray(fields.detailed_description) ? { detailed_description: fields.detailed_description.map(String) } : {}),
    ...(includeAudio && fieldText(fields.overall_soundscape) ? { overall_soundscape: fieldText(fields.overall_soundscape) } : {}),
    ...(includeAudio && fieldText(fields.non_diegetic_music) ? { non_diegetic_music: fieldText(fields.non_diegetic_music) } : {}),
    ...(structured.quality && typeof structured.quality === 'object' ? { quality: structured.quality } : {}),
    negative_prompt: raw.negative_prompt || '',
    continuity: raw.consistency_snapshot || {}
  }
}
