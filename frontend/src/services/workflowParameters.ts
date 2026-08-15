import type { WorkflowUserParameterDefinition } from '@/types'

const finiteNumber = (value: unknown): number | undefined => {
  const numeric = Number(value)
  return Number.isFinite(numeric) ? numeric : undefined
}

const decimalPlaces = (value: number): number => {
  const text = String(value).toLowerCase()
  if (!text.includes('e')) return (text.split('.')[1] || '').length
  const [coefficient, exponentText] = text.split('e')
  const coefficientPlaces = (coefficient.split('.')[1] || '').length
  return Math.max(0, coefficientPlaces - Number(exponentText))
}

/** Normalize one UI value to the same min/max/step lattice enforced by the backend. */
export const normalizeWorkflowParameterValue = (
  rawValue: unknown,
  definition: WorkflowUserParameterDefinition,
): number => {
  const fallback = finiteNumber(definition.default) ?? 0
  let value = finiteNumber(rawValue) ?? fallback
  const minimum = finiteNumber(definition.min)
  const maximum = finiteNumber(definition.max)
  const configuredStep = finiteNumber(definition.step)
  const step = configuredStep !== undefined && configuredStep > 0
    ? configuredStep
    : definition.type === 'integer' ? 1 : undefined

  if (minimum !== undefined) value = Math.max(minimum, value)
  if (maximum !== undefined) value = Math.min(maximum, value)
  if (step === undefined) return value

  // The backend uses min as the step origin and zero when min is absent.
  // Integer scaling avoids values such as 0.15000000000000002 reaching the API.
  const origin = minimum ?? 0
  const precision = Math.min(12, Math.max(
    decimalPlaces(origin),
    decimalPlaces(step),
    minimum === undefined ? 0 : decimalPlaces(minimum),
    maximum === undefined ? 0 : decimalPlaces(maximum),
  ))
  const scale = 10 ** precision
  const originUnits = Math.round(origin * scale)
  const stepUnits = Math.max(1, Math.round(step * scale))
  const valueUnits = Math.round(value * scale)
  let stepIndex = Math.round((valueUnits - originUnits) / stepUnits)

  if (minimum !== undefined) {
    const minimumUnits = Math.round(minimum * scale)
    stepIndex = Math.max(stepIndex, Math.ceil((minimumUnits - originUnits) / stepUnits))
  }
  if (maximum !== undefined) {
    const maximumUnits = Math.round(maximum * scale)
    stepIndex = Math.min(stepIndex, Math.floor((maximumUnits - originUnits) / stepUnits))
  }

  const normalized = (originUnits + stepIndex * stepUnits) / scale
  return definition.type === 'integer' ? Math.round(normalized) : normalized
}
