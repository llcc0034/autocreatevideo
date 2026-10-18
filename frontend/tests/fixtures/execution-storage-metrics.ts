export const preparationId = '00000000-0000-4000-8000-000000000297'
export function storageReceipt(revision = 8) {
  return { preparation_id: preparationId, preparation_revision: revision, observed_at: '2026-10-05T00:00:00Z', items:
    ['intent', 'resolved_inputs', 'review_contract', 'evaluation', 'prepared_request'].map((key, index) => {
      const missing = index === 3, size = [13407, 80251, 32, null, 72169][index]!
      const limit = [65536, 262144, 65536, 65536, 72168][index]!
      return { key, label: ['原始输入意图', '已解析输入与来源', '字段审核合同', '准备评估', '最终冻结请求文件'][index],
        state: missing ? 'not_generated' : 'recorded', basis: index === 4 ? 'canonical_request_file_utf8' : 'canonical_json_utf8',
        schema_version: missing ? null : 1, size_bytes: size, sha256: missing ? null : 'a'.repeat(64),
        current_limit_bytes: limit, over_current_limit_bytes: missing ? null : Math.max(0, size! - limit) }
    }) }
}
