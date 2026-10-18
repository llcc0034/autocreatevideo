export const unusedLanguageForm = () => ({
  choices: [{ value: 'en', label: '英文 / English' }, { value: 'zh-Hans', label: '简体中文 / Simplified Chinese' }],
  allow_custom: true, source: { required: false, fields: [] as string[] },
  target: { required: false, fields: [] as string[] }, fixed_fields: [] as { label: string; language: string }[],
})
