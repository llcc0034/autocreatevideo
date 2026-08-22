<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  AlertTriangle, Check, ChevronDown, Clock3, Database, LoaderCircle,
  RefreshCw, ShieldCheck, Sparkles, Trash2, WandSparkles,
} from 'lucide-vue-next'
import { repository } from '@/services/repository'
import {
  VISUAL_RULE_COLORS,
  VISUAL_RULE_CONTRAST,
  VISUAL_RULE_EXPOSURE,
  VISUAL_RULE_LIGHTING,
  VISUAL_RULE_PALETTE_RELATIONS,
  VISUAL_RULE_PROGRESSION,
  VISUAL_RULE_SATURATION,
  visualRulesColorDesignErrors,
  visualRulesColorDesignLabel,
  visualRulesColorDesignPayload,
  visualRulesColorDesignSummary,
  visualRulesDraftError,
  visualRulesDraftValidationErrors,
  visualRulesLocalContextSnapshot,
  visualRulesVariationNoteErrors,
} from '@/services/visualRulesDraft'
import type {
  SeriesProject, StylePreset, VisualRulesDraftApplyResult,
  VisualRulesColor, VisualRulesColorDesign, VisualRulesDraftData, VisualRulesDraftResult,
} from '@/types'

const props = defineProps<{
  project: SeriesProject
  style?: StylePreset
  blockedReason?: string
}>()
const emit = defineEmits<{ applied: [result: VisualRulesDraftApplyResult] }>()

type Phase = 'idle' | 'generating' | 'ready' | 'busy' | 'failed' | 'conflict' | 'applying' | 'applied'
const phase = ref<Phase>('idle')
const result = ref<VisualRulesDraftResult | null>(null)
const candidate = ref<VisualRulesDraftData | null>(null)
const colorDesign = ref<VisualRulesColorDesign | null>(null)
const initialColorDesignSnapshot = ref('')
const sourceSnapshot = ref('')
const variationNote = ref('')
const attempt = ref(0)
const generatedVariationNote = ref('')
const error = ref('')
const notice = ref('')

const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value))
const current = computed<VisualRulesDraftData>(() => ({
  negativePrompt: props.project.bible.negativePrompt || '',
  colorScript: props.project.bible.colorScript || '',
  continuitySummary: props.project.continuitySummary || '',
}))
const sourceChanged = computed(() => Boolean(
  result.value
  && phase.value !== 'applied'
  && sourceSnapshot.value
  && sourceSnapshot.value !== visualRulesLocalContextSnapshot(props.project, props.style),
))
const noteValidationErrors = computed(() => visualRulesVariationNoteErrors(variationNote.value))
const designValidationErrors = computed(() => colorDesign.value
  ? visualRulesColorDesignErrors(visualRulesColorDesignPayload(colorDesign.value))
  : candidate.value ? ['色彩设计候选缺失；请重新生成'] : [])
const validationErrors = computed(() => [
  ...(candidate.value ? visualRulesDraftValidationErrors(candidate.value) : []),
  ...designValidationErrors.value,
])
const designChanged = computed(() => Boolean(
  colorDesign.value
  && initialColorDesignSnapshot.value
  && JSON.stringify(colorDesign.value) !== initialColorDesignSnapshot.value,
))
const candidateChanged = computed(() => Boolean(candidate.value && (
  candidate.value.negativePrompt.trim() !== current.value.negativePrompt.trim()
  || candidate.value.colorScript.trim() !== current.value.colorScript.trim()
  || candidate.value.continuitySummary.trim() !== current.value.continuitySummary.trim()
  || designChanged.value
)))
const actionBusy = computed(() => phase.value === 'generating' || phase.value === 'applying')
const canApply = computed(() => Boolean(
  result.value && candidate.value && candidateChanged.value && !sourceChanged.value
  && !validationErrors.value.length && !actionBusy.value && phase.value !== 'applied' && !props.blockedReason,
))
const phaseProfile = computed(() => ({
  idle: { label: '尚未生成', tone: 'muted' },
  generating: { label: '正在调用本地 Qwen', tone: 'working' },
  ready: { label: '候选待人工采用', tone: 'warning' },
  busy: { label: '资源忙 · 正式规则未变', tone: 'danger' },
  failed: { label: '生成失败 · 正式规则未变', tone: 'danger' },
  conflict: { label: '故事源已变化 · 必须重生成', tone: 'danger' },
  applying: { label: '正在复核并原子保存', tone: 'working' },
  applied: { label: '已明确采用并保存', tone: 'success' },
}[phase.value]))

const generate = async () => {
  if (actionBusy.value) return
  if (props.blockedReason) {
    phase.value = 'failed'
    error.value = props.blockedReason
    return
  }
  if (noteValidationErrors.value.length) {
    phase.value = 'failed'
    error.value = noteValidationErrors.value.join('；')
    return
  }
  const restartingAfterApply = phase.value === 'applied'
  const localSourceChanged = sourceChanged.value
  error.value = ''; notice.value = ''; phase.value = 'generating'
  try {
    const saved = await repository.getProject(props.project.id)
    const requestSnapshot = visualRulesLocalContextSnapshot(saved, props.style)
    const nextAttempt = Math.min(100, attempt.value + 1)
    const next = await repository.generateVisualRulesDraft(saved.id, {
      attempt: nextAttempt,
      variationNote: variationNote.value,
      expectedContextFingerprint: result.value && !localSourceChanged && !restartingAfterApply
        ? result.value.contextFingerprint
        : undefined,
    })
    attempt.value = nextAttempt
    result.value = next
    candidate.value = clone(next.draft)
    colorDesign.value = clone(next.colorDesign)
    initialColorDesignSnapshot.value = JSON.stringify(next.colorDesign)
    generatedVariationNote.value = variationNote.value.trim()
    sourceSnapshot.value = requestSnapshot
    phase.value = 'ready'
    notice.value = sourceChanged.value
      ? '英文候选已生成，但当前页面与服务端已保存来源不一致。请先保存或刷新项目，再重新生成后采用。'
      : '英文候选只保存在当前页面；尚未修改任何正式字段。请逐项核对后再采用。'
  } catch (cause) {
    const detail = visualRulesDraftError(cause)
    phase.value = detail.kind === 'resource_busy' ? 'busy' : detail.kind === 'source_conflict' ? 'conflict' : 'failed'
    error.value = detail.message
  }
}

const apply = async () => {
  if (!result.value || !candidate.value || !colorDesign.value || actionBusy.value) return
  if (props.blockedReason) {
    phase.value = 'failed'
    error.value = props.blockedReason
    return
  }
  if (sourceChanged.value) {
    phase.value = 'conflict'
    error.value = '页面中的故事、风格、Bible、剧集梗概或现行规则已变化。请重新生成候选，不能采用旧上下文结果。'
    return
  }
  const applyErrors = [...validationErrors.value, ...noteValidationErrors.value]
  if (applyErrors.length) {
    error.value = applyErrors.join('；')
    return
  }
  phase.value = 'applying'; error.value = ''; notice.value = ''
  try {
    const applied = await repository.applyVisualRulesDraft(props.project.id, {
      contextFingerprint: result.value.contextFingerprint,
      generationFingerprint: result.value.generation.generationFingerprint,
      attempt: result.value.generation.attempt,
      directionNote: '',
      variationNote: generatedVariationNote.value,
      colorDesign: colorDesign.value,
    })
    candidate.value = clone(applied.applied)
    initialColorDesignSnapshot.value = JSON.stringify(colorDesign.value)
    phase.value = 'applied'
    notice.value = '服务端已在锁内复核故事指纹，并原子保存三项正式规则。'
    emit('applied', applied)
  } catch (cause) {
    const detail = visualRulesDraftError(cause)
    phase.value = detail.kind === 'resource_busy' ? 'busy' : detail.kind === 'source_conflict' ? 'conflict' : 'failed'
    error.value = detail.message
  }
}

const discard = () => {
  phase.value = 'idle'; result.value = null; candidate.value = null; colorDesign.value = null
  initialColorDesignSnapshot.value = ''
  sourceSnapshot.value = ''; generatedVariationNote.value = ''; error.value = ''; notice.value = ''; attempt.value = 0
}

const rows = computed(() => [
  { key: 'negativePrompt' as const, label: '全局负面提示词', help: '确定性保护项：平台安全基线与已保存英文排除项合并，Qwen 不参与改写，采用时由服务端重新计算。' },
  { key: 'colorScript' as const, label: '色彩脚本', help: '服务端权威预览：Qwen 只提出下方结构化色彩设计；此英文文本不能直接编辑，调整枚举后由服务端在采用时重新渲染。' },
  { key: 'continuitySummary' as const, label: '连续性摘要', help: '确定性保护项：根据稳定 Bible 与项目规则构造跨集连续性，Qwen 不参与改写，采用时由服务端重新计算。' },
])

const baseColorAt = (index: number): VisualRulesColor | '' => colorDesign.value?.baseColors[index] || ''
const baseColorDisabled = (color: VisualRulesColor, index: number): boolean => Boolean(
  colorDesign.value?.baseColors.some((item, itemIndex) => itemIndex !== index && item === color),
)
const setBaseColor = (index: number, event: Event) => {
  if (!colorDesign.value) return
  const value = (event.target as HTMLSelectElement).value as VisualRulesColor | ''
  const next = [...colorDesign.value.baseColors]
  if (!value) next.splice(index)
  else next[index] = value
  colorDesign.value = { ...colorDesign.value, baseColors: next.slice(0, 3) }
  error.value = ''
}
const rowChanged = (key: keyof VisualRulesDraftData): boolean => Boolean(candidate.value && (
  candidate.value[key].trim() !== current.value[key].trim()
  || (key === 'colorScript' && designChanged.value)
))
</script>

<template>
  <section class="visual-draft-workbench">
    <header>
      <div class="visual-draft-title"><div><WandSparkles :size="18"/></div><span><small>LOCAL QWEN · ISOLATED CANDIDATE</small><strong>根据故事信息生成全剧视觉规则候选</strong><p>Qwen 只提出结构化色彩设计；三项英文规则由服务端确定性生成，只有明确采用后才写入正式项目。</p></span></div>
      <span class="visual-draft-status" :class="phaseProfile.tone"><LoaderCircle v-if="actionBusy" :size="12" class="spinning"/><Check v-else-if="phase==='applied'" :size="12"/><AlertTriangle v-else-if="['busy','failed','conflict'].includes(phase)" :size="12"/><Clock3 v-else :size="12"/>{{ phaseProfile.label }}</span>
    </header>

    <div class="visual-draft-sources">
      <Database :size="15"/><span><strong>自动读取已保存来源</strong><small>项目名称、故事简介、全剧母稿、风格文本、画幅与尺寸、目标模型、三类 Bible 稳定身份字段、canonical 术语表、现行项目规则，以及已有剧集的编号 / 标题 / 梗概。</small><em>不会读取剧集剧本、分镜、参考图像素，也不会修改风格模板或术语表。</em><em v-if="result">本次服务端确认：角色 {{ result.sourceSummary.characterCount }}、场景 {{ result.sourceSummary.sceneCount }}、道具 {{ result.sourceSummary.propCount }}、剧集摘要 {{ result.sourceSummary.episodeSummaryCount }}；剧集剧本 / 分镜均未读取。</em></span>
      <details><summary>查看边界 <ChevronDown :size="12"/></summary><p>本地 Qwen 只提议结构化色彩设计，不得输出人物、场景、道具事实或某一集的具体动作；负面词与连续性摘要由服务端确定性构造。生成候选前不会自动保存当前页面资料，只读取服务端已保存来源；候选生成后若任何来源变化，旧候选会被阻止采用。</p></details>
    </div>

    <div class="visual-draft-controls">
      <label><span>本次视觉倾向 / 重生成要求（可选）</span><textarea v-model="variationNote" rows="2" maxlength="2000" :disabled="actionBusy" placeholder="例如：冷青灰占主导，线索只用克制暗红；高潮可转暖金，但保持自然肤色。不要新增具体人物动作。"></textarea><small>{{ variationNote.length }}/2000 · 只影响本次候选，不会写入正式字段</small><small v-for="item in noteValidationErrors" :key="item" class="field-error">{{ item }}</small></label>
      <button class="button primary" type="button" :disabled="actionBusy || noteValidationErrors.length > 0 || Boolean(blockedReason)" :title="blockedReason||''" @click="generate"><LoaderCircle v-if="phase==='generating'" :size="14" class="spinning"/><Sparkles v-else :size="14"/>{{ result ? '重新生成候选' : '生成英文候选' }}</button>
    </div>

    <div v-if="blockedReason" class="visual-draft-message danger"><AlertTriangle :size="14"/><span><strong>canonical 术语碰撞 · 已暂停生成与采用</strong><small>{{ blockedReason }} 请先在上方术语表修正并保存。</small></span></div>
    <div v-if="sourceChanged" class="visual-draft-message danger"><AlertTriangle :size="14"/><span><strong>候选依据已变化</strong><small>当前页面与生成时的已保存故事上下文不一致。旧候选不能采用；请先手动保存或刷新项目，再点击“重新生成候选”建立新的故事指纹。</small></span></div>
    <div v-if="error" class="visual-draft-message danger"><AlertTriangle :size="14"/><span><strong>{{ phaseProfile.label }}</strong><small>{{ error }}</small></span><button type="button" :disabled="actionBusy" @click="generate"><RefreshCw :size="12"/>重试</button></div>
    <div v-if="notice" class="visual-draft-message success"><ShieldCheck :size="14"/><span><strong>{{ phase==='applied'?'正式规则已保存':'候选与正式值隔离' }}</strong><small>{{ notice }}</small></span></div>
    <div v-if="result?.warnings.length" class="visual-draft-message warning"><AlertTriangle :size="14"/><span><strong>服务端提示</strong><small v-for="warning in result.warnings" :key="warning">{{ warning }}</small></span></div>

    <div v-if="candidate" class="visual-draft-comparison">
      <article v-for="row in rows" :key="row.key">
        <header><span><strong>{{ row.label }}</strong><small>{{ row.help }}</small></span><em :class="{changed:rowChanged(row.key)}">{{ rowChanged(row.key)?'候选有变化':'与当前相同' }}</em></header>
        <div><section><span>当前正式值 · 只读</span><p>{{ current[row.key] || '（尚未填写）' }}</p></section><label><span>服务端英文候选 · 只读</span><textarea :value="candidate[row.key]" rows="5" readonly class="protected"></textarea></label></div>
      </article>
    </div>

    <section v-if="candidate && colorDesign" class="visual-color-design">
      <header>
        <span><small>STRUCTURED COLOR DESIGN</small><strong>调整结构化色彩设计</strong><p>这里只能调整色板、光线、对比度、饱和度、曝光与跨集阶段变化。人物、场景、道具事实请修改上方正式字段或 Bible；服务端会拒绝把剧情事实混入色彩设计。</p></span>
        <em :class="{changed:designChanged}">{{ designChanged ? '设计已调整' : '服务端原始设计' }}</em>
      </header>
      <div class="visual-color-design-grid">
        <label><span>主色 1（必选）</span><select :value="baseColorAt(0)" :disabled="actionBusy || phase==='applied'" @change="setBaseColor(0, $event)"><option v-for="item in VISUAL_RULE_COLORS" :key="item" :value="item" :disabled="baseColorDisabled(item, 0)">{{ visualRulesColorDesignLabel(item) }}</option></select></label>
        <label><span>主色 2（可选）</span><select :value="baseColorAt(1)" :disabled="actionBusy || phase==='applied'" @change="setBaseColor(1, $event)"><option value="">不使用</option><option v-for="item in VISUAL_RULE_COLORS" :key="item" :value="item" :disabled="baseColorDisabled(item, 1)">{{ visualRulesColorDesignLabel(item) }}</option></select></label>
        <label><span>主色 3（可选）</span><select :value="baseColorAt(2)" :disabled="actionBusy || phase==='applied' || !baseColorAt(1)" @change="setBaseColor(2, $event)"><option value="">不使用</option><option v-for="item in VISUAL_RULE_COLORS" :key="item" :value="item" :disabled="baseColorDisabled(item, 2)">{{ visualRulesColorDesignLabel(item) }}</option></select></label>
        <label><span>强调色</span><select v-model="colorDesign.accentColor" :disabled="actionBusy || phase==='applied'"><option value="none">{{ visualRulesColorDesignLabel('none') }}</option><option v-for="item in VISUAL_RULE_COLORS" :key="item" :value="item">{{ visualRulesColorDesignLabel(item) }}</option></select></label>
        <label><span>色彩关系</span><select v-model="colorDesign.paletteRelation" :disabled="actionBusy || phase==='applied'"><option v-for="item in VISUAL_RULE_PALETTE_RELATIONS" :key="item" :value="item">{{ visualRulesColorDesignLabel(item) }}</option></select></label>
        <label><span>光线</span><select v-model="colorDesign.lighting" :disabled="actionBusy || phase==='applied'"><option v-for="item in VISUAL_RULE_LIGHTING" :key="item" :value="item">{{ visualRulesColorDesignLabel(item) }}</option></select></label>
        <label><span>对比度</span><select v-model="colorDesign.contrast" :disabled="actionBusy || phase==='applied'"><option v-for="item in VISUAL_RULE_CONTRAST" :key="item" :value="item">{{ visualRulesColorDesignLabel(item) }}</option></select></label>
        <label><span>饱和度</span><select v-model="colorDesign.saturation" :disabled="actionBusy || phase==='applied'"><option v-for="item in VISUAL_RULE_SATURATION" :key="item" :value="item">{{ visualRulesColorDesignLabel(item) }}</option></select></label>
        <label><span>曝光</span><select v-model="colorDesign.exposure" :disabled="actionBusy || phase==='applied'"><option v-for="item in VISUAL_RULE_EXPOSURE" :key="item" :value="item">{{ visualRulesColorDesignLabel(item) }}</option></select></label>
        <label><span>跨集阶段变化</span><select v-model="colorDesign.progression" :disabled="actionBusy || phase==='applied'"><option v-for="item in VISUAL_RULE_PROGRESSION" :key="item" :value="item">{{ visualRulesColorDesignLabel(item) }}</option></select></label>
        <label class="skin-exposure"><input v-model="colorDesign.preserveNaturalSkinExposure" type="checkbox" :disabled="actionBusy || phase==='applied'"><span><strong>保持自然肤色曝光</strong><small>关闭后，肤色曝光跟随所选整体曝光。</small></span></label>
      </div>
      <div class="visual-color-summary"><strong>结构化选择摘要</strong><p>{{ visualRulesColorDesignSummary(colorDesign) }}</p><small v-if="designChanged">上方英文色彩脚本仍是生成时的服务端预览；采用时服务端会按当前枚举重新渲染并返回最终权威文本。</small><small v-else>上方英文色彩脚本是本次生成结果的服务端权威预览。</small></div>
    </section>

    <div v-if="validationErrors.length" class="visual-draft-validation"><AlertTriangle :size="13"/><span><small v-for="item in validationErrors" :key="item">{{ item }}</small></span></div>
    <footer v-if="candidate"><span><template v-if="result">故事指纹 {{ result.contextFingerprint.slice(0,12) }}… · </template>候选不会自动覆盖当前值<template v-if="result?.runtime.elapsedMs"> · 本次 {{ (result.runtime.elapsedMs/1000).toFixed(1) }} 秒</template></span><div><button type="button" class="button ghost" :disabled="actionBusy" @click="discard"><Trash2 :size="13"/>放弃候选</button><button type="button" class="button primary" :disabled="!canApply" @click="apply"><LoaderCircle v-if="phase==='applying'" :size="14" class="spinning"/><Check v-else :size="14"/>{{ phase==='applying'?'正在复核并保存…':'采用并保存三项规则' }}</button></div></footer>
  </section>
</template>

<style scoped>
.visual-draft-workbench{margin-top:14px;overflow:hidden;border:1px solid rgba(111,88,177,.28);border-radius:10px;background:linear-gradient(135deg,rgba(89,62,148,.06),rgba(10,14,18,.92) 44%)}
.visual-draft-workbench>header{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;padding:14px 15px;border-bottom:1px solid rgba(111,88,177,.18)}
.visual-draft-title{display:flex;gap:10px;align-items:flex-start}.visual-draft-title>div{width:31px;height:31px;display:flex;align-items:center;justify-content:center;flex:0 0 auto;border-radius:8px;background:rgba(133,94,211,.12);color:#aa82ef}.visual-draft-title>span{display:flex;flex-direction:column;gap:3px}.visual-draft-title small{color:#9872d4;font-size:7px;letter-spacing:.15em}.visual-draft-title strong{color:#d7dce1;font-size:12px}.visual-draft-title p{margin:0;color:#737c85;font-size:9px;line-height:1.5}
.visual-draft-status{height:24px;padding:0 8px;display:flex;align-items:center;gap:5px;flex:0 0 auto;border:1px solid #303740;border-radius:999px;color:#808991;font-size:8px}.visual-draft-status.working{border-color:rgba(218,154,70,.34);color:#dda350}.visual-draft-status.warning{border-color:rgba(218,154,70,.34);color:#dda350}.visual-draft-status.danger{border-color:rgba(233,107,119,.35);color:#e27e89}.visual-draft-status.success{border-color:rgba(91,198,163,.34);color:#67c9a8}
.visual-draft-sources{margin:12px 14px 0;padding:11px 12px;display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:9px;align-items:flex-start;border:1px solid rgba(83,155,196,.2);border-radius:8px;background:rgba(83,155,196,.035);color:#68a8cc}.visual-draft-sources>span{display:flex;min-width:0;flex-direction:column;gap:3px}.visual-draft-sources strong{color:#8cbcd5;font-size:9px}.visual-draft-sources small,.visual-draft-sources em{font-size:8px;line-height:1.5;color:#718793;font-style:normal}.visual-draft-sources em{color:#6f777e}.visual-draft-sources details{position:relative}.visual-draft-sources summary{display:flex;align-items:center;gap:4px;color:#82adc4;font-size:8px;cursor:pointer;list-style:none}.visual-draft-sources details p{width:340px;margin:8px 0 0;padding:9px;position:absolute;right:0;z-index:3;border:1px solid #303a42;border-radius:7px;background:#0b1015;color:#8c969d;font-size:8px;line-height:1.55;box-shadow:0 12px 30px rgba(0,0,0,.35)}
.visual-draft-controls{padding:12px 14px;display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:end}.visual-draft-controls label{display:flex;min-width:0;flex-direction:column;gap:5px}.visual-draft-controls label>span{color:#8d969e;font-size:9px}.visual-draft-controls textarea{min-height:48px;padding:8px 9px;resize:vertical;border:1px solid #2b3239;border-radius:7px;background:#090d11;color:#c7cdd2;font:9px/1.55 inherit}.visual-draft-controls small{color:#606970;font-size:7px}.visual-draft-controls small.field-error{color:#df7b86}.visual-draft-controls .button{height:34px}
.visual-draft-message{margin:0 14px 10px;padding:9px 10px;display:flex;align-items:flex-start;gap:8px;border:1px solid rgba(90,195,160,.24);border-radius:7px;background:rgba(90,195,160,.045);color:#64c4a4}.visual-draft-message>span{display:flex;min-width:0;flex:1;flex-direction:column;gap:3px}.visual-draft-message strong{font-size:9px}.visual-draft-message small{font-size:8px;line-height:1.45}.visual-draft-message.danger{border-color:rgba(234,105,119,.28);background:rgba(234,105,119,.05);color:#df7b86}.visual-draft-message.warning{border-color:rgba(218,154,70,.28);background:rgba(218,154,70,.05);color:#d09a51}.visual-draft-message button{display:flex;align-items:center;gap:4px;border:0;background:transparent;color:inherit;font-size:8px;cursor:pointer}
.visual-draft-comparison{padding:2px 14px 12px;display:flex;flex-direction:column;gap:9px}.visual-draft-comparison>article{overflow:hidden;border:1px solid #262d34;border-radius:8px;background:#0b0f13}.visual-draft-comparison>article>header{padding:8px 10px;display:flex;align-items:flex-start;justify-content:space-between;gap:10px;border-bottom:1px solid #232a31}.visual-draft-comparison header>span{display:flex;min-width:0;flex-direction:column;gap:3px}.visual-draft-comparison header strong{color:#b8c0c6;font-size:9px}.visual-draft-comparison header small{color:#68727a;font-size:8px;line-height:1.45}.visual-draft-comparison header em{padding:3px 6px;flex:0 0 auto;border:1px solid #2b3239;border-radius:999px;color:#717a82;font-size:7px;font-style:normal}.visual-draft-comparison header em.changed{border-color:rgba(218,154,70,.3);color:#d29a50}.visual-draft-comparison>article>div{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:#242a30}.visual-draft-comparison section,.visual-draft-comparison label{padding:9px;display:flex;min-width:0;flex-direction:column;gap:6px;background:#0d1216}.visual-draft-comparison section>span,.visual-draft-comparison label>span{color:#69727a;font-size:7px;letter-spacing:.06em}.visual-draft-comparison p{min-height:72px;max-height:150px;margin:0;overflow:auto;white-space:pre-wrap;color:#848d94;font-size:9px;line-height:1.55}.visual-draft-comparison textarea{min-height:90px;padding:8px;resize:vertical;border:1px solid rgba(218,154,70,.22);border-radius:6px;background:#080c10;color:#cdd2d6;font:9px/1.55 inherit}.visual-draft-comparison textarea.protected{border-color:rgba(83,155,196,.18);background:#0a1014;color:#8b9aa3;cursor:not-allowed}.visual-draft-comparison textarea:disabled{opacity:.68}
.visual-color-design{margin:0 14px 12px;padding:12px;border:1px solid rgba(218,154,70,.24);border-radius:8px;background:rgba(218,154,70,.035)}.visual-color-design>header{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:11px}.visual-color-design>header>span{display:flex;min-width:0;flex-direction:column;gap:3px}.visual-color-design>header small{color:#b78543;font-size:7px;letter-spacing:.13em}.visual-color-design>header strong{color:#c9ced2;font-size:10px}.visual-color-design>header p{max-width:780px;margin:0;color:#747d84;font-size:8px;line-height:1.5}.visual-color-design>header em{padding:3px 6px;flex:0 0 auto;border:1px solid #2d343a;border-radius:999px;color:#717a82;font-size:7px;font-style:normal}.visual-color-design>header em.changed{border-color:rgba(218,154,70,.35);color:#dda350}.visual-color-design-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}.visual-color-design-grid>label{display:flex;min-width:0;flex-direction:column;gap:5px}.visual-color-design-grid>label>span{color:#7b858d;font-size:8px}.visual-color-design-grid select{width:100%;height:31px;padding:0 8px;border:1px solid #29323a;border-radius:6px;background:#090e12;color:#b8c1c8;font:8px inherit}.visual-color-design-grid select:disabled{opacity:.55}.visual-color-design-grid .skin-exposure{min-height:31px;padding:7px 8px;flex-direction:row;align-items:flex-start;border:1px solid #29323a;border-radius:6px;background:#090e12}.visual-color-design-grid .skin-exposure input{margin:2px 0 0;accent-color:#d89a49}.visual-color-design-grid .skin-exposure>span{display:flex;flex-direction:column;gap:2px}.visual-color-design-grid .skin-exposure strong{color:#aeb7bd;font-size:8px}.visual-color-design-grid .skin-exposure small{color:#69737a;font-size:7px;line-height:1.4}.visual-color-summary{margin-top:9px;padding:8px 9px;border:1px solid rgba(83,155,196,.17);border-radius:6px;background:rgba(83,155,196,.025)}.visual-color-summary strong{color:#8eb5ca;font-size:8px}.visual-color-summary p{margin:4px 0;color:#aeb8be;font-size:8px;line-height:1.5}.visual-color-summary small{display:block;color:#71808a;font-size:7px;line-height:1.45}
.visual-draft-validation{margin:0 14px 10px;padding:8px 10px;display:flex;align-items:flex-start;gap:7px;border:1px solid rgba(234,105,119,.24);border-radius:7px;color:#db7782}.visual-draft-validation span{display:flex;flex-direction:column;gap:3px}.visual-draft-validation small{font-size:8px}
.visual-draft-workbench>footer{padding:11px 14px;display:flex;align-items:center;justify-content:space-between;gap:12px;border-top:1px solid #242a30;color:#687078;font-size:8px}.visual-draft-workbench>footer>div{display:flex;gap:7px}
@media(max-width:1100px){.visual-color-design-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:900px){.visual-draft-workbench>header,.visual-draft-workbench>footer{align-items:stretch;flex-direction:column}.visual-draft-status{width:max-content}.visual-draft-sources{grid-template-columns:auto minmax(0,1fr)}.visual-draft-sources details{grid-column:2}.visual-draft-sources details p{width:min(70vw,340px);left:0;right:auto}.visual-draft-controls{grid-template-columns:1fr}.visual-draft-comparison>article>div{grid-template-columns:1fr}.visual-color-design-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.visual-draft-workbench>footer>div{justify-content:flex-end}}
@media(max-width:560px){.visual-color-design-grid{grid-template-columns:1fr}}
</style>
