import type { H3Mode } from '@/types'

export const DEFAULT_PROJECT_H3_MODE: H3Mode = 'T2VA'

const PROJECT_DEFAULT_H3_MODES = new Set<H3Mode>(['T2VA', 'FL2VA', 'Ref2VA'])

export const projectDefaultH3Mode = (value: unknown): H3Mode => (
  typeof value === 'string' && PROJECT_DEFAULT_H3_MODES.has(value as H3Mode)
    ? value as H3Mode
    : DEFAULT_PROJECT_H3_MODE
)
