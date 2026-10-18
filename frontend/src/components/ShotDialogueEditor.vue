<script setup lang="ts">
import type { BibleItem, Shot } from '@/types'
import { removeDialogueLine } from '@/services/shotCharacters'
const props = defineProps<{ shot: Shot; characters: BibleItem[]; disabled?: boolean }>()
const emit = defineEmits<{ change: [] }>()
const speaker = (index: number) => props.shot.characterRelations.find((row) => row.role === 'speaker' && row.position === index)?.character_id || ''
function selectSpeaker(index: number, event: Event) {
  const id = (event.target as HTMLSelectElement).value
  const character = props.characters.find((item) => item.id === id)
  props.shot.characterRelations = [...props.shot.characterRelations.filter((row) => row.role !== 'speaker' || row.position !== index),
    { role: 'speaker', position: index, character_id: id }]
  props.shot.audio.dialogue[index].speaker_id = character?.stableSpeakerId || ''
  emit('change')
}
function addLine() {
  const position = props.shot.audio.dialogue.length
  props.shot.audio.dialogue.push({ speaker_id: '', text: '' })
  props.shot.characterRelations.push({ role: 'speaker', position, character_id: '' })
  emit('change')
}
function optionalText(index: number, key: 'delivery' | 'language', event: Event) {
  const value = (event.target as HTMLInputElement).value
  if (value === '') delete props.shot.audio.dialogue[index][key]
  else props.shot.audio.dialogue[index][key] = value
  emit('change')
}
function time(index: number, key: 'start_ms' | 'end_ms', event: Event) {
  const value = (event.target as HTMLInputElement).value
  if (value === '') delete props.shot.audio.dialogue[index][key]
  else props.shot.audio.dialogue[index][key] = Number(value)
  emit('change')
}
function removeLine(index: number) { removeDialogueLine(props.shot, index); emit('change') }
</script>

<template>
  <section class="dialogue-editor" aria-label="逐行对白与发言者">
    <header><strong>对白与发言者</strong><button type="button" :disabled="disabled" @click="addLine">添加对白</button></header>
    <p class="hint">发言者与出镜人物独立。选择确切人物；同名时以发言标识区分，不根据名字或画外音自动改出镜关系。</p>
    <p v-if="!shot.audio.dialogue.length" class="hint">本镜无对白。</p>
    <fieldset v-for="(line, index) in shot.audio.dialogue" :key="index" :disabled="disabled">
      <legend>对白 {{ index + 1 }}</legend>
      <label>发言者<select :aria-label="`对白 ${index + 1} 发言者`" :value="speaker(index)" @change="selectSpeaker(index, $event)">
        <option value="" disabled>请选择发言者</option>
        <option v-if="speaker(index) && !characters.some((item) => item.id === speaker(index))" :value="speaker(index)">原人物不可用，请重新选择</option>
        <option v-for="item in characters" :key="item.id" :value="item.id" :disabled="!item.stableSpeakerId">{{ item.name }} · {{ item.stableSpeakerId || '缺少发言标识' }}</option>
      </select></label>
      <label>对白原文<textarea v-model="line.text" :aria-label="`对白 ${index + 1} 原文`" rows="3" @input="emit('change')" /></label>
      <label>本句对白语言<input :value="line.language" :aria-label="`对白 ${index + 1} 语言`" placeholder="例如 Chinese、English 或语言代码" @input="optionalText(index, 'language', $event)" /></label>
      <small class="hint">同一人物可逐句切换；不改变台词原文。留空表示未声明，视频模板需要语言标记时必须填写，不继承项目或导演描述的语言。</small>
      <label>演绎描述（可选，不是台词）<input :value="line.delivery" :aria-label="`对白 ${index + 1} 演绎描述`" @input="optionalText(index, 'delivery', $event)" /></label>
      <label class="voiceover"><input v-model="line.voiceover" type="checkbox" :aria-label="`对白 ${index + 1} 画外音`" @change="emit('change')" />画外音（不自动更改出镜人物）</label>
      <div class="times">
        <label>开始（毫秒，可选）<input type="number" min="0" step="1" :value="line.start_ms" :aria-label="`对白 ${index + 1} 开始毫秒`" @input="time(index, 'start_ms', $event)" /></label>
        <label>结束（毫秒，可选）<input type="number" min="1" step="1" :value="line.end_ms" :aria-label="`对白 ${index + 1} 结束毫秒`" @input="time(index, 'end_ms', $event)" /></label>
      </div>
      <small class="hint">时间相对本镜起点；同时留空或成对填写，不得超过镜头时长。</small>
      <button type="button" :aria-label="`移除对白 ${index + 1}`" @click="removeLine(index)">移除此行</button>
    </fieldset>
  </section>
</template>

<style scoped>
.dialogue-editor{display:grid;gap:12px;min-width:0}.dialogue-editor header{display:flex;align-items:center;justify-content:space-between;gap:12px}.dialogue-editor fieldset{display:grid;gap:10px;margin:0;padding:12px;min-width:0;border:1px solid var(--border-strong);border-radius:6px}.dialogue-editor legend{padding:0 5px}.dialogue-editor label{display:grid;gap:5px;font-size:12px;min-width:0}.dialogue-editor input,.dialogue-editor select,.dialogue-editor textarea{box-sizing:border-box;width:100%;min-width:0;padding:8px;border:1px solid var(--border-strong);border-radius:4px;background:var(--surface);color:inherit;font:inherit}.dialogue-editor .voiceover{display:flex;align-items:center}.voiceover input{width:auto}.times{display:grid;grid-template-columns:1fr 1fr;gap:10px}.hint{margin:0;font-size:12px;color:var(--text-secondary);overflow-wrap:anywhere}.dialogue-editor button{justify-self:start;padding:6px 10px;border:1px solid var(--border-strong);border-radius:4px;background:var(--surface);color:inherit;cursor:pointer}.dialogue-editor button:disabled{cursor:not-allowed;opacity:.6}@media(max-width:480px){.times{grid-template-columns:1fr}}
</style>
