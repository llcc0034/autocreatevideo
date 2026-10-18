<script setup lang="ts">
import { computed } from 'vue'
import type { ShotTiming } from '@/services/shotTiming'
import { shotTimingPayload, timingMilliseconds } from '@/services/shotTiming'
const props = defineProps<{ shot: ShotTiming; minimumSeconds?: number; maximumSeconds?: number }>()
const emit = defineEmits<{ change: [value: ShotTiming] }>()
const interval = computed(() => props.shot.episodeStartMs != null || props.shot.episodeEndMs != null)
const error = computed(() => { try { shotTimingPayload(props.shot); return '' } catch (e) { return String((e as Error).message) } })
const mode = (event: Event) => {
  const enabled = (event.target as HTMLSelectElement).value === 'interval'
  // Visible initial range, not inferred from episode order or source media.
  emit('change', { ...props.shot, episodeStartMs: enabled ? 0 : null, episodeEndMs: enabled ? props.shot.duration * 1000 : null })
}
const update = (key: 'episodeStartMs' | 'episodeEndMs' | 'duration', event: Event) => {
  const input = (event.target as HTMLInputElement).value
  let parsed = input === '' ? null : Number(input)
  if (parsed !== null && key !== 'duration') {
    try { parsed = timingMilliseconds(parsed) } catch { parsed = Number.NaN }
  }
  const value = { ...props.shot, [key]: parsed }
  if (key !== 'duration' && value.episodeStartMs != null && value.episodeEndMs != null && value.episodeEndMs > value.episodeStartMs) {
    value.duration = (value.episodeEndMs - value.episodeStartMs) / 1000
  }
  emit('change', value as ShotTiming)
}
</script>
<template>
  <fieldset class="shot-timing">
    <legend>镜头时间</legend>
    <label><span>配置方式</span><select :value="interval ? 'interval' : 'duration'" @change="mode"><option value="duration">只填时长，按镜头顺序排列</option><option value="interval">明确本集起止时间</option></select></label>
    <div v-if="interval" class="timing-range">
      <label><span>本集开始时间（秒）</span><input type="number" min="0" step="0.001" :value="shot.episodeStartMs == null ? '' : shot.episodeStartMs / 1000" @input="update('episodeStartMs', $event)"></label>
      <label><span>本集结束时间（秒）</span><input type="number" min="0" step="0.001" :value="shot.episodeEndMs == null ? '' : shot.episodeEndMs / 1000" @input="update('episodeEndMs', $event)"></label>
    </div>
    <label><span>镜头时长（秒）{{ interval ? '· 由起止时间计算' : '' }}</span><input type="number" step="0.001" :min="minimumSeconds" :max="maximumSeconds" :value="shot.duration" :readonly="interval" @input="update('duration', $event)"></label>
    <small>起止时间相对本集开头，不是源视频截取区间。只填时长时不生成原稿未声明的时间点；区间有空隙或重叠时，装配前需明确调整。</small>
    <p v-if="error" role="alert">{{ error }}</p>
  </fieldset>
</template>
<style scoped>
.shot-timing{display:grid;gap:12px;border:1px solid var(--border);border-radius:var(--radius-md,8px);padding:12px;min-width:0}.shot-timing label{display:grid;gap:6px}.timing-range{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}.shot-timing input,.shot-timing select{min-width:0;min-height:36px}.shot-timing small{color:var(--text-secondary)}.shot-timing p{color:var(--color-danger,var(--text-primary))}
</style>
