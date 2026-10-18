import { apiErrorPresentation } from './apiErrorPresentation'

export interface AuthoringIssue { field: string; message: string; section?: string }

// Only explicit field paths are mapped to UI sections. Message wording never
// determines a field, a dependency, or a suggested replacement value.
export function authoringIssues(cause: unknown, sections: Record<string, string>): AuthoringIssue[] {
  const result = apiErrorPresentation(cause)
  return Object.entries(result.fields).map(([field, messages]) => ({
    field, message: messages.join('；'), section: sections[field.split(/[.[]/, 1)[0]!],
  }))
}

export function authoringResponseStatus(cause: unknown): number {
  const response = (cause as { response?: { status?: unknown } } | null)?.response
  return typeof response?.status === 'number' ? response.status : 0
}

export function isExplicitAuthoringRejection(cause: unknown): boolean {
  return [400, 401, 403, 404, 405, 409, 422, 429].includes(authoringResponseStatus(cause))
}
