import type { H3PromptPayload } from '../types/index.ts'

type RawPromptArtifact = Record<string, any>

/** Convert the persisted backend prompt artifact into the editor contract. */
export const adaptPromptArtifact = (raw: RawPromptArtifact): H3PromptPayload => {
  const structured = raw.structured_payload || {}
  const fields = structured.fields || structured
  const fieldText = (value: unknown) => Array.isArray(value)
    ? value.map((item) => String(item || '').trim()).filter(Boolean).join('\n')
    : String(value || '').trim()
  const mediaType: H3PromptPayload['mediaType'] = raw.prompt_type === 'image' ? 'image' : 'video'
  const durationMs = Number(raw.total_duration_ms || structured.duration_ms || 0)
  const includeAudio = mediaType === 'video' && raw.include_audio !== false
  const promptText = String(raw.prompt_text || '').trim()
  return {
    ...(typeof raw.compiler_current === 'boolean' ? { compiler_current: raw.compiler_current } : {}),
    ...(raw.id ? { artifact_id: String(raw.id) } : {}),
    ...(Number.isFinite(Number(raw.revision)) ? { artifact_revision: Number(raw.revision) } : {}),
    ...(raw.created_at ? { artifact_created_at: String(raw.created_at) } : {}),
    model: String(raw.model || structured.model || (mediaType === 'image' ? '图片工作流' : 'MiniMax H3')),
    mediaType,
    ...(mediaType === 'video' ? {
      mode: raw.h3_mode || structured.mode || 'T2VA',
      ...(Array.isArray(structured.reference_bindings) ? { reference_bindings: structured.reference_bindings } : {}),
      ...(durationMs > 0 ? { duration: durationMs / 1000 } : {})
    } : {}),
    ...(structured.effective_conditioning_mode ? {
      effective_conditioning_mode: structured.effective_conditioning_mode
    } : {}),
    ...(structured.prompt_template_key ? {
      prompt_template_key: String(structured.prompt_template_key)
    } : {}),
    prompt_text: promptText || fieldText(fields.integrated_multimodal_description || fields.detailed_description),
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
