<script setup lang="ts">
import { nextTick, ref, useId } from 'vue'
import type { ReferenceRenderingOption } from '@/services/platformTypes'

const props = defineProps<{ item: ReferenceRenderingOption; modelValue: string; disabled?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()
const textarea = ref<HTMLTextAreaElement | null>(null)
const id = useId()
defineExpose({ focus: () => { textarea.value?.scrollIntoView({ block: 'center' }); textarea.value?.focus() } })
const insert = async (token: string) => {
  if (props.disabled || !props.item.placeholders.includes(token)) return
  const target = textarea.value
  const start = target?.selectionStart ?? props.modelValue.length
  const end = target?.selectionEnd ?? start
  const text = `{${token}}`
  emit('update:modelValue', props.modelValue.slice(0, start) + text + props.modelValue.slice(end))
  await nextTick()
  target?.focus()
  target?.setSelectionRange(start + text.length, start + text.length)
}
</script>

<template>
  <section class="rendering-template field wide" :aria-labelledby="`${id}-label`">
    <label :id="`${id}-label`" :for="`${id}-template`">{{ item.label }} <small>{{ item.key }}</small></label>
    <p>{{ item.description }}</p>
    <p class="requirement">{{ item.requirement }}</p>
    <slot name="usage" />
    <textarea :id="`${id}-template`" ref="textarea" :value="modelValue" :disabled="disabled" rows="3" :aria-describedby="`${id}-hint`" @input="emit('update:modelValue', ($event.target as HTMLTextAreaElement).value)" />
    <p :id="`${id}-hint`">写普通句子，点击下方按钮在光标处插入动态内容。只修改括号外的文字，不要翻译或改写括号内的英文标识。</p>
    <div class="rendering-placeholders" v-if="item.placeholders.length">
      <button v-for="token in item.placeholders" :key="token" type="button" class="button ghost tiny" :disabled="disabled" :title="item.placeholderHelp[token].description" :aria-label="`插入${item.placeholderHelp[token].label} {${token}}`" @click="insert(token)">+ {{ item.placeholderHelp[token].label }} <code>{{ '{' + token + '}' }}</code></button>
    </div>
    <p v-else>这段只输出固定文字，没有可插入的占位符。</p>
    <details>
      <summary>查看取值说明与替换示例</summary>
      <p>组装去向：{{ item.assembly }}。需在字段组合规则中选用对应原语，再设置排列顺序和输出通道。</p>
      <dl class="placeholder-help">
        <div v-for="token in item.placeholders" :key="token">
          <dt>{{ item.placeholderHelp[token].label }} <code>{{ '{' + token + '}' }}</code></dt>
          <dd>{{ item.placeholderHelp[token].description }}<small>示例值：<code>{{ item.placeholderHelp[token].example || '空字符串' }}</code></small></dd>
        </div>
      </dl>
      <p class="example-note">下面是虚构数据的教学示例，不是当前模板预览，不会填入配置、绑定素材或发起模型调用。</p>
      <div class="example-pair"><div><strong>示例模板</strong><pre>{{ item.example.template }}</pre></div><div><strong>替换后的一段文字</strong><pre>{{ item.example.result }}</pre></div></div>
      <p>实际取值为空时，该位置会变为空文字，周围的固定文字仍保留；必要素材和已审核内容缺失会由完整编译校验阻止。保存草稿后，可用“最终提示词预览”核对示例字段的排版与通道；真实素材替换需结合实际项目和镜头验收。</p>
    </details>
  </section>
</template>

<style scoped>
.rendering-template{display:grid;gap:10px;min-width:0;border-top:1px solid var(--border);padding-top:16px}
label{font-weight:600}label small{margin-left:8px;font-weight:400}p{margin:0;font-size:12px;line-height:1.7;color:var(--text-secondary);overflow-wrap:anywhere}.requirement{color:var(--text)}
textarea{width:100%;box-sizing:border-box;resize:vertical;min-height:96px}.rendering-placeholders{display:flex;flex-wrap:wrap;gap:6px}.rendering-placeholders button{min-height:36px;font-size:12px;white-space:normal;text-align:left;overflow-wrap:anywhere}code{overflow-wrap:anywhere}
summary{cursor:pointer;min-height:36px;line-height:36px}details[open]{display:grid}details>p{margin:10px 0}
.placeholder-help{margin:10px 0;display:grid;gap:10px}.placeholder-help>div{display:grid;grid-template-columns:minmax(120px,1fr) minmax(0,3fr);gap:12px;border-bottom:1px solid var(--border);padding-bottom:8px}dt{font-size:12px}dd{margin:0;font-size:12px;line-height:1.7;color:var(--text-secondary)}dd small{display:block}
.example-pair{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;font-size:12px}pre{margin:8px 0;white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.7;padding:12px;border:1px solid var(--border);border-radius:var(--radius-sm,6px)}
@media(max-width:700px){.example-pair,.placeholder-help>div{grid-template-columns:minmax(0,1fr)}}
</style>
