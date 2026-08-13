<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Check, ChevronDown } from 'lucide-vue-next'
import {
  COMMON_PRODUCTION_RESOLUTION_OPTIONS,
  DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE,
  MAX_PRODUCTION_RESOLUTION_MULTIPLE,
  MIN_PRODUCTION_RESOLUTION_MULTIPLE,
  MORE_PRODUCTION_RESOLUTION_OPTIONS,
  PRODUCTION_RESOLUTION_MULTIPLE_STEP,
  PRODUCTION_RESOLUTION_OPTIONS
} from '@/constants/productionResolution'
import type { ProductionResolutionProfile } from '@/types'

const props = withDefaults(defineProps<{
  modelValue: ProductionResolutionProfile
  multiple?: number
  disabled?: boolean
  compact?: boolean
  label?: string
  multipleStep?: 4 | 8
}>(), {
  multiple: DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE,
  disabled: false,
  compact: false,
  label: '官方分辨率 / 显存档位',
  multipleStep: PRODUCTION_RESOLUTION_MULTIPLE_STEP,
})

const emit = defineEmits<{
  'update:modelValue': [value: ProductionResolutionProfile]
  'update:multiple': [value: number]
}>()

const isMoreSelection = computed(() => MORE_PRODUCTION_RESOLUTION_OPTIONS.some((option) => option.value === props.modelValue))
const selectedOption = computed(() => PRODUCTION_RESOLUTION_OPTIONS.find((option) => option.value === props.modelValue) || PRODUCTION_RESOLUTION_OPTIONS[2])
const moreOpen = ref(isMoreSelection.value)

watch(isMoreSelection, (selected) => {
  if (selected) moreOpen.value = true
})

const choose = (value: ProductionResolutionProfile) => {
  if (!props.disabled) emit('update:modelValue', value)
}

const updateMultiple = (event: Event) => {
  if (props.disabled) return
  const input = event.target as HTMLInputElement
  const numeric = Number(input.value)
  const normalized = Number.isFinite(numeric)
    ? Math.min(
        MAX_PRODUCTION_RESOLUTION_MULTIPLE,
        Math.max(
          MIN_PRODUCTION_RESOLUTION_MULTIPLE,
          Math.round(numeric / props.multipleStep) * props.multipleStep,
        ),
      )
    : DEFAULT_PRODUCTION_RESOLUTION_MULTIPLE
  input.value = String(normalized)
  emit('update:multiple', normalized)
}
</script>

<template>
  <section class="production-resolution-picker" :class="{ compact, disabled }">
    <header>
      <span>{{ label }}</span>
      <small>已选 {{ selectedOption.label }} · 倍数 {{ multiple }}</small>
    </header>

    <div class="resolution-quick-options">
      <button
        v-for="option in COMMON_PRODUCTION_RESOLUTION_OPTIONS"
        :key="option.value"
        type="button"
        :class="{ active: modelValue === option.value }"
        :disabled="disabled"
        :aria-pressed="modelValue === option.value"
        @click="choose(option.value)"
      >
        <strong>{{ option.label }}</strong>
        <small>{{ option.shortHint }}</small>
        <Check :size="11" />
      </button>
    </div>

    <button
      type="button"
      class="resolution-more-toggle"
      :class="{ active: isMoreSelection }"
      :disabled="disabled"
      :aria-expanded="moreOpen"
      @click="moreOpen = !moreOpen"
    >
      <span>
        <strong>{{ isMoreSelection ? selectedOption.label : '更多官方档位' }}</strong>
        <small>0.4–2.0 MP 与项目目标尺寸</small>
      </span>
      <ChevronDown :size="14" :class="{ open: moreOpen }" />
    </button>

    <div v-if="moreOpen" class="resolution-more-options">
      <button
        v-for="option in MORE_PRODUCTION_RESOLUTION_OPTIONS"
        :key="option.value"
        type="button"
        :class="{ active: modelValue === option.value, project: option.value === 'project', high: option.risk === 'high' }"
        :disabled="disabled"
        :aria-pressed="modelValue === option.value"
        @click="choose(option.value)"
      >
        <span><strong>{{ option.label }}</strong><small>{{ option.shortHint }}</small></span>
        <Check :size="11" />
      </button>
    </div>

    <label class="resolution-multiple-field">
      <span>
        <strong>尺寸倍数 multiple</strong>
        <small>范围 8–128，步长 {{ multipleStep }}；默认 32</small>
      </span>
      <input
        type="number"
        :value="multiple"
        :min="MIN_PRODUCTION_RESOLUTION_MULTIPLE"
        :max="MAX_PRODUCTION_RESOLUTION_MULTIPLE"
        :step="multipleStep"
        :disabled="disabled"
        inputmode="numeric"
        @change="updateMultiple"
      >
    </label>

    <p v-if="modelValue === 'project'">本次任务使用项目目标尺寸；倍数参数会保留，切换到 MP 档位后生效。</p>
    <p v-else>本次任务使用 {{ selectedOption.label }}（{{ selectedOption.shortHint }}），宽高按 {{ multiple }} 的倍数取整；不会修改项目默认规格。</p>
  </section>
</template>

<style scoped>
.production-resolution-picker{display:flex;flex-direction:column;gap:7px}.production-resolution-picker>header{display:flex;align-items:center;justify-content:space-between;gap:8px}.production-resolution-picker>header>span{font-size:11px;color:#92979d}.production-resolution-picker>header>small{font-size:10px;color:#656b72}.resolution-quick-options{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:5px}.resolution-quick-options>button{min-width:0;min-height:52px;padding:8px;position:relative;display:flex;flex-direction:column;align-items:flex-start;justify-content:center;gap:3px;border:1px solid var(--border);border-radius:6px;background:#0b0e12;color:#71777e;text-align:left;cursor:pointer}.resolution-quick-options strong{font-size:11px;color:#aeb3b8}.resolution-quick-options small{font-size:9px;color:#5e646b}.resolution-quick-options svg{display:none;position:absolute;right:6px;top:6px;color:var(--accent)}.resolution-quick-options>button.active{border-color:rgba(230,167,90,.4);background:rgba(230,167,90,.055)}.resolution-quick-options>button.active strong,.resolution-quick-options>button.active small{color:var(--accent)}.resolution-quick-options>button.active svg{display:block}.resolution-more-toggle{min-height:43px;padding:7px 9px;display:flex;align-items:center;justify-content:space-between;gap:8px;border:1px solid var(--border);border-radius:6px;background:#0b0e12;color:#777d84;text-align:left;cursor:pointer}.resolution-more-toggle>span{display:flex;flex-direction:column;gap:2px}.resolution-more-toggle strong{font-size:11px;color:#989da3}.resolution-more-toggle small{font-size:9px;color:#565c63}.resolution-more-toggle>svg{transition:transform .18s}.resolution-more-toggle>svg.open{transform:rotate(180deg)}.resolution-more-toggle.active{border-color:rgba(230,167,90,.32);background:rgba(230,167,90,.035)}.resolution-more-toggle.active strong{color:var(--accent)}.resolution-more-options{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:4px}.resolution-more-options>button{min-width:0;min-height:42px;padding:6px 7px;display:flex;align-items:center;justify-content:space-between;gap:4px;border:1px solid var(--border);border-radius:5px;background:#0a0d11;color:#747a81;text-align:left;cursor:pointer}.resolution-more-options>button>span{min-width:0;display:flex;flex-direction:column;gap:2px}.resolution-more-options strong{font-size:10px;color:#9ba0a6;white-space:nowrap}.resolution-more-options small{font-size:9px;color:#545a61;white-space:nowrap}.resolution-more-options svg{visibility:hidden;color:var(--accent);flex:0 0 auto}.resolution-more-options>button.active{border-color:rgba(230,167,90,.38);background:rgba(230,167,90,.05)}.resolution-more-options>button.active strong,.resolution-more-options>button.active small{color:var(--accent)}.resolution-more-options>button.active svg{visibility:visible}.resolution-more-options>button.project{grid-column:1/-1}.resolution-more-options>button.high:not(.active) small{color:#8c6266}.resolution-multiple-field{min-height:48px;padding:7px 9px;display:flex;align-items:center;justify-content:space-between;gap:10px;border:1px solid var(--border);border-radius:6px;background:#0b0e12}.resolution-multiple-field>span{display:flex;flex-direction:column;gap:2px}.resolution-multiple-field strong{font-size:11px;color:#989da3}.resolution-multiple-field small{font-size:9px;color:#565c63}.resolution-multiple-field input{width:76px;height:32px;padding:0 7px;border:1px solid var(--border-strong);border-radius:5px;background:#10141a;color:#b2b7bd;font-size:11px;text-align:center}.production-resolution-picker>p{margin:0;font-size:9px;line-height:1.5;color:#656b72}.production-resolution-picker.disabled{opacity:.68}.production-resolution-picker button:disabled,.production-resolution-picker input:disabled{cursor:not-allowed}.production-resolution-picker.compact{gap:6px}.production-resolution-picker.compact .resolution-quick-options>button{min-height:48px;padding:7px}.production-resolution-picker.compact .resolution-more-options{grid-template-columns:repeat(2,minmax(0,1fr))}.production-resolution-picker.compact .resolution-more-options>button.project{grid-column:1/-1}
</style>
