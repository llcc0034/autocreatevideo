import type { ExecutionInputSuggestion } from './executionRegistry'
import type { ExecutionRequestDraft } from './executionRequestForm'

export function applyExecutionInputSuggestion(row: ExecutionRequestDraft['literals'][string], suggestion: ExecutionInputSuggestion,
  contract: { sha256: string }, inputKey: string): void {
  if (suggestion.input_key !== inputKey || suggestion.definition_sha256 !== contract.sha256 || !suggestion.reference || !suggestion.content.trim()) {
    throw new Error('参考内容与当前输入不一致，请重新预览')
  }
  row.value = suggestion.content
  row.reference = { token: suggestion.reference, label: suggestion.label, content: suggestion.content }
}
