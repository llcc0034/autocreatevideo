<script setup lang="ts">
import { computed, ref } from 'vue'
import { Check, Clipboard, ChevronDown, Sparkles } from 'lucide-vue-next'
import {
  promptWritingGuideContract,
  type PromptWritingGuideProfile,
} from '@/services/promptWritingGuide'

const props = withDefaults(defineProps<{
  scope?: 'project' | 'bible' | 'shot'
  profile?: PromptWritingGuideProfile | null
}>(), { scope: 'project', profile: null })
const copied = ref('')

const projectTemplate = `第1集：雨夜来客
镜头1【时长=8秒】【首帧连续=否】【尾帧连续=是】：雨夜客栈外。
[0-3秒] 动作：沈砚推门进入，右手始终握住玄纹剑。
[3-6秒] 沈砚（in a low, restrained voice）：谁在等我？
[6-8秒] 动作：他在柜台前停住，保持可衔接的最终姿态。

镜头2【时长=6秒】【首帧连续=是】【尾帧连续=否】：掌柜退到灯下。
[0-2.5秒] 动作：掌柜的视线落在玄纹剑上。
[2.5-5秒] 掌柜（voiceover; softly, with controlled breath）：你终于来了。
[5-6秒] 动作：掌柜抬眼，画面稳定结束。`

const bibleTemplate = `【人物 · 主图设定】
共同身份：年轻剑客，窄长脸，右眉尾有浅疤。
主图外观：黑发高束，穿旧青灰交领袍，深色皮护腕。

【场景 · 主图设定】
共同身份：木构客栈，右侧入口，后墙长柜台，二层连续回廊。
主图外观：雨夜，纸窗透入冷青月光，暖琥珀色灯笼补光。

【道具 · 主图设定】
共同身份：窄直黑钢剑，银护手，剑鞘末端有三道斜磨痕。
主图外观：护手磨旧，深棕绳缠握柄，剑身有细微水珠。

【变体】引用确切主图修订，另写本次外观差异；不重新定义共同身份。`


const shotTemplate = `Action / visible state change:
At the inn doorway, the swordsman begins still with his right hand on the sheathed sword. He steps across the wet threshold and turns toward the counter. Rainwater falls from his robe hem and forms a small pool. He ends motionless in a guarded three-quarter stance, his face and wardrobe unchanged.

Camera:
The camera tracks backward at walking speed from a medium shot, then settles into a static medium close-up as he stops.

Dialogue / delivery:
沈砚（in a low, restrained voice）：谁在等我？
沈砚（voiceover; softly, with controlled breath）：我又回到了这里。

Positive continuity:
Preserve the same face, high topknot, weathered blue-gray robe, sword in the right hand, screen direction, and final stance.

Overall soundscape:
Rain strikes the tiled roof; wet footsteps cross the timber floor; the sword guard gives one short metallic click synchronized to his stop.

Non-diegetic music:
Sparse guqin harmonics at 72 BPM; a slow two-note pulse swells once, then fades before the cut.`


const guide = computed(() => promptWritingGuideContract(props.profile))
const showProject = computed(() => props.scope === 'project')
const showBible = computed(() => props.scope === 'bible')
const showShot = computed(() => props.scope === 'shot')

const copyText = async (key: string, value: string) => {
  try {
    await navigator.clipboard.writeText(value)
  } catch {
    const input = document.createElement('textarea')
    input.value = value; input.style.position = 'fixed'; input.style.opacity = '0'
    document.body.appendChild(input); input.select(); document.execCommand('copy'); input.remove()
  }
  copied.value = key
  window.setTimeout(() => { if (copied.value === key) copied.value = '' }, 1600)
}
</script>

<template>
  <details class="prompt-writing-guide">
    <summary><span><Sparkles :size="14"/><strong>字段写作模板与配置化装配指南</strong></span><ChevronDown :size="14"/></summary>
    <div class="guide-body">
      <article v-if="guide.profile" class="active-profile-guide">
        <header><div><span>当前策略 / CURRENT PROMPT PROFILE</span><strong>{{ guide.profile.name }} · v{{ guide.profile.version }}</strong></div></header>
        <dl class="profile-guide-facts"><div><dt>来源投影器</dt><dd>{{ guide.adapterLabel }}</dd></div><div><dt>配置键</dt><dd><code>{{ guide.adapterKey }}</code></dd></div></dl>
        <p>{{ guide.adapterDescription }}</p>
        <ol v-if="guide.fieldRules.length" class="configured-field-rules">
          <li v-for="rule in guide.fieldRules" :key="`${rule.position}:${rule.fieldKey}`"><b>{{ rule.position }}. {{ rule.fieldLabel || rule.fieldKey }}</b><code>{{ rule.fieldKey }}</code><small>{{ rule.sourceSelector }} → {{ rule.channel }} · {{ rule.labelStyle }} · {{ rule.separator }} · ≤ {{ rule.maxChars }} 字符<template v-if="rule.generationMode"> · {{ rule.generationMode }} / {{ rule.generationOutputSemantics }}</template></small></li>
        </ol>
        <p v-else>当前 Profile 尚未配置字段规则；请先在系统设置完成字段来源、顺序、标签、分隔符、通道和长度上限，再以服务端预览结果为准。</p>
        <nav v-if="guide.profile.officialReference" class="official-links" aria-label="当前 PromptProfile 官方资料"><span>管理员登记的官方资料</span><a :href="guide.profile.officialReference" target="_blank" rel="noreferrer">打开当前 Profile 官方说明</a></nav>
      </article>
      <article v-if="showProject">
        <header><div><span>项目母稿 / PROJECT SCRIPT</span><strong>全剧母稿可识别模板</strong></div><button type="button" @click="copyText('project',projectTemplate)"><component :is="copied==='project'?Check:Clipboard" :size="12"/>{{ copied==='project'?'已复制':'复制模板' }}</button></header>
        <pre>{{ projectTemplate }}</pre>
        <p>这是填写示例，不是运行默认值。导入或粘贴的母稿保留原文；结构化结果需要校验并明确采用。整集中的镜头起止时间用于确定位置与时长，例如 45–50 秒代表一个 5 秒镜头，不会原样传给单镜视频工作流。动作和对白时间使用镜头内时间。无效、越界或相互矛盾的时间会要求修正，不会静默换成默认值。人物、场景和道具名称应与 Bible 一致。</p>
      </article>
      <article v-if="showBible">
        <header><div><span>一致性设定集 / CONSISTENCY BIBLE</span><strong>人物 / 场景 / 道具锚点模板</strong></div><button type="button" @click="copyText('bible',bibleTemplate)"><component :is="copied==='bible'?Check:Clipboard" :size="12"/>{{ copied==='bible'?'已复制':'复制模板' }}</button></header>
        <pre>{{ bibleTemplate }}</pre>
        <p>可先仅登记实体名称，无需先有图片或锚点。共同身份和外观在“主图 / 变体设定”中维护，修改后另存修订；普通实体说明不会覆盖设定。原文可使用中文，保存不会自动调用模型。需要翻译时，在当前主图修订中明确选择执行实例生成候选，采用后核对并点击“确认译文”；也可以手工填写、调整译文，中文原文不会被覆盖。目标语言以所选策略的字段要求为准。这里只写身份和外观事实；动作、情绪和运镜留在具体分镜中。</p>
      </article>
      <article v-if="showShot">
        <header><div><span>单镜输入模板 / SHOT INPUT TEMPLATE</span><strong>单镜高质量英文填写模板</strong></div><button type="button" @click="copyText('shot',shotTemplate)"><component :is="copied==='shot'?Check:Clipboard" :size="12"/>{{ copied==='shot'?'已复制':'复制模板' }}</button></header>
        <pre>{{ shotTemplate }}</pre>
        <p>按“起始状态 → 主动作 → 环境响应 → 结束状态”描述可见变化，动作数量应适合本镜时长。这是写作示例，不强制使用固定六段或固定语言；哪些字段需要英文、采用什么标签、分隔符和通道，以精确策略版本为准。对白正文保留原文，不把整集时间偏移当作单镜模型输入。</p>
        <p>参考视频会被模型读取完整内容；purpose 仅表达希望侧重的动作、运镜或节奏意图，不提供模型级内容隔离，也不能替代 Bible 锚点。实际素材、配套音频与接线由本次输入映射和冻结合同确定，不根据示例文字自动添加。</p>
      </article>
      <article class="translation-rule">
        <header><div><span>模型生成与人工复核 / MODEL REVIEW</span><strong>中文源文 → 英文候选 → 人工确认</strong></div></header>
        <ul><li>独立翻译使用明确选择的 <code>prompt.normalize</code> 执行实例；策略字段的生成或翻译使用该实例配置的辅助执行实例，不自动寻找本地模型或旧绑定。输出先作为候选，采用和审核不会覆盖中文原文；过期或源文已变化的译文不能冒充当前已确认内容。</li><li>确认前重点核对专名、人物性别、左/右手、道具部位、数值与比例、动作先后、镜头运动、光线方向和最终状态；可直接编辑英文译文再确认。</li><li>在连续剧工作室按“中文术语 = Canonical English”配置术语表，例如 <code>玄纹剑 = Xuanwen Sword</code>。未配置的专名请明确填写并复核；素材标签以本次冻结映射和策略规则为准，不从模型名称推断。</li><li>对白、歌词和真实屏显文字应保留原文；其他字段按本次选择的目标语言与策略规则处理。</li><li>哪些业务内容进入提示词，由策略字段来源及执行实例输入映射决定；生成前查看准备详情与最终提示词预览。</li></ul>
      </article>
    </div>
  </details>
</template>

<style scoped>
.prompt-writing-guide{border:1px solid rgba(159,140,255,.16);border-radius:8px;background:rgba(159,140,255,.025);overflow:hidden}.prompt-writing-guide summary{min-height:44px;padding:8px 12px;display:flex;align-items:center;justify-content:space-between;color:#8d84bd;cursor:pointer;list-style:none}.prompt-writing-guide summary::-webkit-details-marker{display:none}.prompt-writing-guide summary>span{display:flex;align-items:center;gap:8px}.prompt-writing-guide summary strong{font-size:12px;color:#aaa4c9}.prompt-writing-guide[open] summary>svg{transform:rotate(180deg)}.guide-body{padding:0 10px 10px;display:grid;gap:9px}.guide-body article{min-width:0;padding:11px;border:1px solid rgba(255,255,255,.055);border-radius:6px;background:#0a0d11}.guide-body article>header{display:flex;align-items:center;justify-content:space-between;gap:10px}.guide-body article>header>div{display:flex;flex-direction:column;gap:3px}.guide-body article>header span{font:10px Consolas,monospace;letter-spacing:.1em;color:#6f6796}.guide-body article>header strong{font-size:12px;color:#aaaeb4}.guide-body button{min-height:30px;padding:5px 9px;display:flex;align-items:center;gap:5px;border:1px solid var(--border);border-radius:4px;background:#12161b;color:#8d939a;font-size:10px;cursor:pointer}.guide-body button:hover{color:var(--accent);border-color:rgba(230,167,90,.25)}.guide-body pre{max-height:240px;margin:9px 0 7px;padding:10px;overflow:auto;border-radius:4px;background:#07090c;color:#9aa69e;font:10px/1.65 Consolas,monospace;white-space:pre-wrap;word-break:break-word}.guide-body p,.guide-body li{font-size:11px;line-height:1.6;color:#777e86}.guide-body ul{margin:8px 0 0;padding-left:18px}.guide-body code{font:10px Consolas,monospace;color:#a9a1d9}.profile-guide-facts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin:9px 0}.profile-guide-facts div{padding:8px;border:1px solid rgba(255,255,255,.05);border-radius:4px}.profile-guide-facts dt{font-size:9px;color:#666f78}.profile-guide-facts dd{margin:3px 0 0;font-size:10px;color:#9ca3aa}.configured-field-rules{display:grid;gap:6px;margin:9px 0 0;padding:0;list-style:none}.configured-field-rules li{display:grid;grid-template-columns:minmax(120px,.8fr) minmax(100px,.7fr) minmax(240px,2fr);gap:8px;align-items:center;padding:7px 8px;border:1px solid rgba(255,255,255,.05);border-radius:4px}.configured-field-rules b{font-size:10px;color:#aeb2b7}.configured-field-rules small{font-size:9px;color:#707881}.translation-rule{border-color:rgba(94,164,218,.14)!important}.translation-rule header span{color:#5689ad!important}.official-links{display:flex;align-items:center;flex-wrap:wrap;gap:7px;margin-top:10px;padding-top:9px;border-top:1px solid rgba(255,255,255,.05)}.official-links span{font:9px Consolas,monospace;letter-spacing:.09em;color:#5d8d7e}.official-links a{padding:4px 7px;border:1px solid rgba(104,197,167,.15);border-radius:4px;color:#7db49f;font-size:10px;text-decoration:none}.official-links a:hover{color:#9fd6c0;border-color:rgba(104,197,167,.32)}@media(min-width:1180px){.prompt-writing-guide[open] .guide-body{grid-template-columns:repeat(2,minmax(0,1fr))}.prompt-writing-guide[open] .active-profile-guide,.prompt-writing-guide[open] .translation-rule{grid-column:1/-1}}@media(max-width:720px){.profile-guide-facts{grid-template-columns:1fr}.configured-field-rules{display:block}.configured-field-rules li{display:grid;margin-top:6px;grid-template-columns:1fr}}
</style>
