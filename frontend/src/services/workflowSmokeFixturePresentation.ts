import type {
  ComfyWorkflowParameterScalar,
  WorkflowSmokeFixtureParameter,
} from './platformTypes'

export type WorkflowSmokeFixtureParameterControl =
  | 'fixed'
  | 'select'
  | 'boolean'
  | 'number'
  | 'text'
  | 'textarea'

const semanticKeySegments = (semanticKey: string) => (
  semanticKey.trim().toLocaleLowerCase().split(/[.:/_-]+/).filter(Boolean)
)

/**
 * Choose a smoke-fixture editor from the frozen parameter type and semantic
 * key. Prompt is a generic semantic segment (for example prompt.positive or
 * generation.negative_prompt), so model-specific workflow names stay out of
 * the presentation layer.
 */
export const workflowSmokeFixtureParameterControl = (
  parameter: WorkflowSmokeFixtureParameter,
): WorkflowSmokeFixtureParameterControl => {
  if (parameter.required && parameter.options.length === 1) return 'fixed'
  if (parameter.options.length) return 'select'
  if (parameter.type === 'boolean') return 'boolean'
  if (parameter.type === 'integer' || parameter.type === 'number') return 'number'
  if (parameter.type === 'string' && semanticKeySegments(parameter.semanticKey).includes('prompt')) {
    return 'textarea'
  }
  return 'text'
}

/**
 * Convert one native form-control value without trimming or normalizing text.
 * In particular, textarea line breaks remain part of the frozen fixture.
 */
export const workflowSmokeFixtureValueFromControl = (
  parameter: WorkflowSmokeFixtureParameter,
  rawValue: string,
): ComfyWorkflowParameterScalar | undefined => {
  if (parameter.options.length) {
    return parameter.options[Number.parseInt(rawValue, 10)]
  }
  if (parameter.type === 'integer') {
    if (rawValue === '') return undefined
    const value = Number(rawValue)
    return Number.isFinite(value) ? value : undefined
  }
  if (parameter.type === 'number') {
    if (rawValue === '') return undefined
    const value = Number(rawValue)
    return Number.isFinite(value) ? value : undefined
  }
  if (parameter.type === 'boolean') {
    if (rawValue === '') return undefined
    return rawValue === 'true'
  }
  return rawValue
}

// Production workflow inputs and Smoke fixtures deliberately share the same
// typed-control conversion.  These aliases make that reuse explicit without
// breaking the existing editor imports.
export const workflowExecutionParameterControl = workflowSmokeFixtureParameterControl
export const workflowExecutionValueFromControl = workflowSmokeFixtureValueFromControl
