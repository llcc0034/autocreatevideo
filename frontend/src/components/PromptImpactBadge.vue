<script setup lang="ts">
import { computed } from 'vue'

export type PromptImpactKind = 'label' | 'indirect' | 'prompt' | 'continuity' | 'production' | 'audit'

const props = withDefaults(defineProps<{
  kind: PromptImpactKind
  title?: string
}>(), { title: '' })

const profiles: Record<PromptImpactKind, { label: string; description: string }> = {
  label: { label: '仅标识', description: '用于名称、检索或任务识别，不直接改变生成内容。' },
  indirect: { label: '间接影响', description: '用于初始化、拆解或管理上下文，不会原样写入当前生成提示词。' },
  prompt: { label: '进入提示词', description: '会按当前已发布提示词策略进入模型输入；字段通道、顺序和负面内容是否提交以冻结合同为准。' },
  continuity: { label: '连续性参考', description: '参与人物、场景、道具或跨镜头一致性约束。' },
  production: { label: '生产参数', description: '控制工作流、尺寸、时长、音频或节点输入。' },
  audit: { label: '质量审计', description: '用于提交前检查、生成快照或结果审计。' }
}

const profile = computed(() => profiles[props.kind])
const tooltip = computed(() => props.title || profile.value.description)
</script>

<template>
  <span class="prompt-impact-badge" :class="`impact-${kind}`" :title="tooltip">
    <i aria-hidden="true"></i>{{ profile.label }}
  </span>
</template>

<style scoped>
.prompt-impact-badge{min-height:20px;padding:2px 7px;display:inline-flex;align-items:center;gap:4px;border:1px solid var(--impact-border);border-radius:10px;background:var(--impact-bg);color:var(--impact-color);font-size:10px!important;font-weight:600!important;line-height:1;letter-spacing:.02em;white-space:nowrap;vertical-align:middle;cursor:help}.prompt-impact-badge i{width:5px;height:5px;flex:0 0 auto;border-radius:50%;background:currentColor;box-shadow:0 0 5px currentColor}.impact-label{--impact-color:#90969d;--impact-border:rgba(144,150,157,.2);--impact-bg:rgba(144,150,157,.06)}.impact-indirect{--impact-color:#9a8f7f;--impact-border:rgba(154,143,127,.22);--impact-bg:rgba(154,143,127,.06)}.impact-prompt{--impact-color:#d7a568;--impact-border:rgba(230,167,90,.26);--impact-bg:rgba(230,167,90,.07)}.impact-continuity{--impact-color:#a399e4;--impact-border:rgba(159,140,255,.25);--impact-bg:rgba(159,140,255,.07)}.impact-production{--impact-color:#70c9ae;--impact-border:rgba(104,197,167,.25);--impact-bg:rgba(104,197,167,.07)}.impact-audit{--impact-color:#72aeca;--impact-border:rgba(100,179,216,.23);--impact-bg:rgba(100,179,216,.07)}
</style>
