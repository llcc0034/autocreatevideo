<script setup lang="ts">
import { computed, ref } from 'vue'
import type { JsonObject } from '@/services/platformTypes'
import { pathSchema, type StructurePath } from '@/services/structurePaths'
import { structuredContextPrefix, structuredContextSelector, structuredContextLabel } from '@/services/promptGenerationContexts'
import SchemaPathSelect from './SchemaPathSelect.vue'

const props = defineProps<{modelValue: string[]; schema: JsonObject | null; enabled: boolean; disabled?: boolean}>()
const emit = defineEmits<{'update:modelValue': [string[]]}>()
const draft = ref<StructurePath | null>(null)
const selected = computed(() => props.modelValue.filter(value => value.startsWith(structuredContextPrefix)))
const canAdd = computed(() => props.enabled && !props.disabled && props.schema && draft.value !== null
  && !pathSchema(props.schema, draft.value).error && !props.modelValue.includes(structuredContextSelector(draft.value)))
function add() {
  if (canAdd.value && draft.value !== null) emit('update:modelValue', [...props.modelValue, structuredContextSelector(draft.value)])
}
function remove(value: string) { if (!props.disabled) emit('update:modelValue', props.modelValue.filter(item => item !== value)) }
</script>
<template>
  <fieldset class="structured-contexts" :disabled="disabled">
    <legend>来自本次输入结构的附加字段（多选）</legend>
    <p>只传入本次输入中明确选择的字段值，保留数字、布尔、空值等原始类型；不会自动加入其他字段。用于逐镜视频调用时，只读取本镜头输入。素材标识不等于图片文件。</p>
    <ul v-if="selected.length"><li v-for="value in selected" :key="value"><span>{{ structuredContextLabel(value) }}</span><button type="button" class="button ghost tiny" @click="remove(value)">移除此参考字段</button></li></ul>
    <p v-if="!enabled">先选择模型生成或仅翻译；附加字段独立于主要原文来源，已有选择不会自动删除。</p>
    <p v-else-if="!schema" role="status">尚未读取到策略绑定的输入结构。</p>
    <template v-else><SchemaPathSelect v-model="draft" :schema="schema" :disabled="disabled" label="选择附加参考字段路径"/><button type="button" class="button ghost small" :disabled="!canAdd" @click="add">加入附加参考</button></template>
  </fieldset>
</template>
<style scoped>
.structured-contexts{min-width:0;padding:12px;border:1px solid var(--border);display:grid;gap:8px}.structured-contexts p{margin:0;color:var(--text-secondary);overflow-wrap:anywhere}.structured-contexts ul{padding:0;list-style:none;margin:0}.structured-contexts li{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:6px 0}.structured-contexts span{overflow-wrap:anywhere;min-width:0;flex:1}.structured-contexts button{min-height:36px}
</style>
