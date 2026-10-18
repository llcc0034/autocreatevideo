import { visualRuleReviewFixture } from '../visualRulesOutputFixture.ts'
export function fieldLineageFixture() {
  const rules = { continuity_summary: 'Keep the blue coat.', color_script: 'Cool blue palette.', negative_prompt: '' }
  const review = { schema_version: 3, kind: 'visual_rules_review_decision', reviewed_rules: rules,
    generation_stage_id: 'generated', artifact_id: 'artifact', artifact_sha256: 'a'.repeat(64), preparation_id: 'preparation', attempt_id: 'attempt', result_sha256: 'c'.repeat(64) }
  const artifact: any = { id: 'artifact', productionRun: 'run', stageRun: 'generated', generationAttempt: 'attempt', status: 'selected',
    sha256: 'a'.repeat(64), manifestMetadataSha256: 'b'.repeat(64), textCandidate: { operation: 'visual_rules.generate', warnings: [],
      preparation_id: 'preparation', result_sha256: 'c'.repeat(64), candidate: rules, review_contract: visualRuleReviewFixture() } }
  const source = { kind: 'context_field', selector_key: 'reviewed_visual_rules.color_script', scope: { kind: 'project' } }
  const definition = { schema_version: 3, interaction_mode: 'assisted', stages: [{ key: 'media_generation', mode: 'existing_pipeline', approval: 'auto',
    routing: { mode: 'fixed', routes: [{ execution_instance_version_id: '11111111-1111-4111-8111-111111111111', expected_definition_sha256: 'd'.repeat(64), parameter_policies: [],
      input_sources: { scene: { kind: 'structured', template: { kind: 'object', fields: { color: source } } } } }] } }] }
  const run: any = { id: 'run', project: 'project', planSource: 'recipe', executionPlan: { value: { recipe: { definition } }, sha256: 'e'.repeat(64) },
    stages: [{ id: 'generated', stageKey: 'visual_rules_generate', ordinal: 1, status: 'succeeded' },
      { id: 'reviewed', stageKey: 'visual_rules_review', ordinal: 2, status: 'succeeded', outputSnapshotSha256: 'f'.repeat(64),
        outputSnapshot: { formal_object_type: 'visual_rules', canonical: { project_id: 'project', rules }, review_payload: review } },
      { id: 'media', stageKey: 'media_generation', ordinal: 3, selectedRouteIndex: 0, status: 'succeeded' }] }
  const project: any = { id: 'project', continuitySummary: rules.continuity_summary, translationGlossary: {}, bible: { negativePrompt: '', colorScript: rules.color_script } }
  return { run, artifact, project, source, definition, review }
}
