import { api, unwrap } from './api'

export interface BudgetCurrencyOption { value: string; label: string }

export async function loadBudgetCurrencies(): Promise<BudgetCurrencyOption[]> {
  const raw = unwrap<{ currencies: BudgetCurrencyOption[] }>((await api.get('/production-runs/budget-options/')).data)
  if (!raw || !Array.isArray(raw.currencies) || raw.currencies.some(row =>
    !row || typeof row.value !== 'string' || !/^[A-Z][A-Z0-9]{2,11}$/.test(row.value)
    || typeof row.label !== 'string' || !row.label.trim())
    || new Set(raw.currencies.map(row => row.value)).size !== raw.currencies.length) {
    throw new Error('预算币种选项不完整，请重新读取。')
  }
  return raw.currencies
}
