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

const bibleTemplate = `【人物 Character visualPrompt】Lean young Chinese swordsman, narrow angular face, a shallow scar at the tail of his right eyebrow, long black hair tied in a high topknot; permanently wears a weathered blue-gray cross-collar robe and dark leather wrist guards; preserve facial geometry, wardrobe colors, and material texture in every shot.

【场景 Scene visualPrompt】Rain-soaked traditional Chinese inn with fixed dark timber columns, the entrance on frame right, a long counter against the rear wall, and a continuous second-floor gallery; cold cyan moonlight enters through torn paper windows, with dim warm amber lantern fill; preserve the architecture and light directions.

【道具 Prop visualPrompt】Narrow straight black-steel sword, worn silver guard, dark-brown cord-wrapped grip, and three diagonal abrasion marks at the end of the scabbard; preserve scale, silhouette, materials, colors, and mark positions.`

const classifiedReferenceDirectorTemplate = `Visual style: cinematic wuxia realism, restrained contrast, natural skin texture.
Scene: define the fixed environment, layout, time of day, lighting direction, and palette.
Subjects: define immutable identity, face, hair, build, wardrobe color, and material.
Props: define shape, scale, material, color, and persistent marks.
Observable action sequence: Starting state -> primary action -> environment response -> final state. Use present tense and only visible, physically executable changes.
Camera: state shot size, angle, lens behavior, motivated movement, speed, and final framing.
On-screen dialogue: The on-screen character (S1) says in a low, restrained voice: <d>[Chinese]台词原文</d>.
Voiceover: Off-screen voiceover narration by the referenced subject (S1), softly: <d>[Chinese]台词原文</d>. The visible character remains silent with lips closed and does not speak on camera.
Do not place defects or forbidden changes in the positive director description; keep them in the negative prompt.`

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

const classifiedReferenceStructure = `subject_definitions:
summary:
retention_analysis:
detailed_description:
overall_soundscape:
non_diegetic_music:`

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
        <p><code>时长</code>、动作/对白时间段和首尾帧连续要求都会确定性写入分镜。时间可写秒、毫秒或 <code>MM:SS.mmm</code>；缺少时间段时使用整镜范围，缺少时长或连续要求时使用页面所示的服务端配置默认值。无效或越界指令只会提示并回退默认，不会中止其他镜头。人物、场景和道具名称应与 Bible 完全一致；对白括号中的演绎指令请使用英文，对白正文保持原文。</p>
      </article>
      <article v-if="showBible">
        <header><div><span>一致性设定集 / CONSISTENCY BIBLE</span><strong>人物 / 场景 / 道具锚点模板</strong></div><button type="button" @click="copyText('bible',bibleTemplate)"><component :is="copied==='bible'?Check:Clipboard" :size="12"/>{{ copied==='bible'?'已复制':'复制模板' }}</button></header>
        <pre>{{ bibleTemplate }}</pre>
        <p>实体名称和 visualPrompt 都可以使用中文。新建 Bible 时，中文 visualPrompt 会自动生成独立的英文候选；必须人工核对并点击“确认译文”后才能进入当前已发布 PromptProfile 的正式装配，中文原文不会被覆盖。若直接填写英文则无需翻译。这里只写不可变化的视觉事实；动作、情绪、运镜和临时光效应留在具体分镜中。</p>
      </article>
      <article v-if="showShot">
        <header><div><span>单镜输入模板 / SHOT INPUT TEMPLATE</span><strong>单镜高质量英文填写模板</strong></div><button type="button" @click="copyText('shot',shotTemplate)"><component :is="copied==='shot'?Check:Clipboard" :size="12"/>{{ copied==='shot'?'已复制':'复制模板' }}</button></header>
        <pre>{{ shotTemplate }}</pre>
        <p>5 秒优先一条主动作链和一种主要运镜；按“起始状态 → 主动作 → 环境响应 → 结束状态”填写。对白正文可以中文；括号内 delivery、人物动作、构图、运镜、声景和配乐会进入导演描述，必须用英文。当前只支持 delivery 与 voiceover，不支持 scenetrans / cutoff。</p>
      </article>
      <article v-if="showShot && guide.showClassifiedReferenceExample" class="mode-contracts">
        <header><div><span>受控适配器案例 / CONTROLLED ADAPTER EXAMPLE</span><strong>H3 分类参考上下文六段案例</strong></div></header>
        <div class="contract-grid"><div><b>仅当当前 Profile 选择 classified_reference_context_ir_v1 时适用</b><pre>{{ classifiedReferenceStructure }}</pre></div></div>
        <ul><li>首帧、尾帧和 17k+5 有效帧时长的对齐句由系统自动生成，不要手写 Picture 编号。</li><li>Ref 图片用于对应人物、场景或道具身份；参考视频会被模型读取完整内容，purpose 仅表达希望侧重的动作、运镜或节奏意图，不提供模型级内容隔离，也不能替代 Bible 锚点；音频用于声明的音色、声音或音乐。</li><li>输入标签按实际连接顺序自动生成；没有素材时不会创建空 Picture / Video / Audio 连接。</li></ul>
      </article>
      <article v-if="guide.showClassifiedReferenceExample" class="director-rule">
        <header><div><span>适配器英文案例 / ADAPTER ENGLISH EXAMPLE</span><strong>H3 英文导演描述案例</strong></div><button type="button" @click="copyText('director',classifiedReferenceDirectorTemplate)"><component :is="copied==='director'?Check:Clipboard" :size="12"/>{{ copied==='director'?'已复制':'复制案例' }}</button></header>
        <pre>{{ classifiedReferenceDirectorTemplate }}</pre>
        <ul><li>使用现在时，只描述可见、可听、可执行的事实。</li><li>第一镜不写时间戳；后续镜头由系统按 <code>[Shot N] At MM:SS.mmm</code> 自动写入严格递增时间。</li><li>参考输入必须使用工作流给出的精确标签，如 &lt;Picture 1&gt;、&lt;Video 1&gt;、&lt;Audio 1&gt;。</li><li>对白、歌词和真实屏显文字保留原语言；人物动作、构图、运镜、声景和配乐使用英文。</li><li>负面约束与正向导演描述分开，避免同一缺陷同时出现在正负提示词。</li></ul>
        <nav class="official-links" aria-label="MiniMax H3 官方资料"><span>官方资料</span><a href="https://github.com/MiniMax-AI/MiniMax-H3/blob/main/skills/h3-prompt-writing/references/base-en.txt" target="_blank" rel="noreferrer">基础提示词（Base Prompt）指南</a><a href="https://github.com/MiniMax-AI/MiniMax-H3/blob/main/skills/h3-prompt-writing/references/ref-en.txt" target="_blank" rel="noreferrer">参考生视频（Ref2VA）提示词指南</a><a href="https://docs.comfy.org/tutorials/video/minimax/minimax-h3" target="_blank" rel="noreferrer">ComfyUI H3 工作流指南</a></nav>
      </article>
      <article class="translation-rule">
        <header><div><span>模型生成与人工复核 / MODEL REVIEW</span><strong>中文源文 → 英文候选 → 人工确认</strong></div></header>
        <ul><li>系统只使用项目明确选择、已通过真实烟测的本地 <code>prompt.normalize</code> 模型部署生成英文候选，不会把未知模型映射为内置模型，也不会覆盖中文原文。未确认、失败或源文已变化的译文绝不会进入正式 prompt。</li><li>确认前重点核对专名、人物性别、左/右手、道具部位、数值与比例、动作先后、镜头运动、光线方向和最终状态；可直接编辑英文译文再确认。</li><li>在连续剧工作室按“中文术语 = Canonical English”配置术语表，例如 <code>玄纹剑 = Xuanwen Sword</code>。未配置的实体名称会使用 S1、scene:slug、prop:slug 等稳定技术标识，系统不会自由猜译专名。</li><li>对白、歌词和真实屏显文字保持原语言；只翻译人物动作、构图、运镜、delivery、连续性、声景和配乐等导演字段。</li><li>项目名、镜头标题和实体名称主要用于标识；母稿与基础描述用于拆解或创作管理。以字段旁的“进入提示词 / 连续性参考”徽标判断是否需要翻译审核。</li></ul>
      </article>
    </div>
  </details>
</template>

<style scoped>
.prompt-writing-guide{border:1px solid rgba(159,140,255,.16);border-radius:8px;background:rgba(159,140,255,.025);overflow:hidden}.prompt-writing-guide summary{min-height:44px;padding:8px 12px;display:flex;align-items:center;justify-content:space-between;color:#8d84bd;cursor:pointer;list-style:none}.prompt-writing-guide summary::-webkit-details-marker{display:none}.prompt-writing-guide summary>span{display:flex;align-items:center;gap:8px}.prompt-writing-guide summary strong{font-size:12px;color:#aaa4c9}.prompt-writing-guide[open] summary>svg{transform:rotate(180deg)}.guide-body{padding:0 10px 10px;display:grid;gap:9px}.guide-body article{min-width:0;padding:11px;border:1px solid rgba(255,255,255,.055);border-radius:6px;background:#0a0d11}.guide-body article>header{display:flex;align-items:center;justify-content:space-between;gap:10px}.guide-body article>header>div{display:flex;flex-direction:column;gap:3px}.guide-body article>header span{font:10px Consolas,monospace;letter-spacing:.1em;color:#6f6796}.guide-body article>header strong{font-size:12px;color:#aaaeb4}.guide-body button{min-height:30px;padding:5px 9px;display:flex;align-items:center;gap:5px;border:1px solid var(--border);border-radius:4px;background:#12161b;color:#8d939a;font-size:10px;cursor:pointer}.guide-body button:hover{color:var(--accent);border-color:rgba(230,167,90,.25)}.guide-body pre{max-height:240px;margin:9px 0 7px;padding:10px;overflow:auto;border-radius:4px;background:#07090c;color:#9aa69e;font:10px/1.65 Consolas,monospace;white-space:pre-wrap;word-break:break-word}.guide-body p,.guide-body li{font-size:11px;line-height:1.6;color:#777e86}.guide-body ul{margin:8px 0 0;padding-left:18px}.guide-body code{font:10px Consolas,monospace;color:#a9a1d9}.profile-guide-facts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin:9px 0}.profile-guide-facts div{padding:8px;border:1px solid rgba(255,255,255,.05);border-radius:4px}.profile-guide-facts dt{font-size:9px;color:#666f78}.profile-guide-facts dd{margin:3px 0 0;font-size:10px;color:#9ca3aa}.configured-field-rules{display:grid;gap:6px;margin:9px 0 0;padding:0;list-style:none}.configured-field-rules li{display:grid;grid-template-columns:minmax(120px,.8fr) minmax(100px,.7fr) minmax(240px,2fr);gap:8px;align-items:center;padding:7px 8px;border:1px solid rgba(255,255,255,.05);border-radius:4px}.configured-field-rules b{font-size:10px;color:#aeb2b7}.configured-field-rules small{font-size:9px;color:#707881}.contract-grid{display:grid;grid-template-columns:minmax(0,1fr);gap:8px;margin-top:9px}.contract-grid b{font-size:10px;color:#8d949b}.contract-grid pre{margin-top:5px}.director-rule{border-color:rgba(104,197,167,.12)!important}.director-rule header span{color:#5d8d7e!important}.translation-rule{border-color:rgba(94,164,218,.14)!important}.translation-rule header span{color:#5689ad!important}.official-links{display:flex;align-items:center;flex-wrap:wrap;gap:7px;margin-top:10px;padding-top:9px;border-top:1px solid rgba(255,255,255,.05)}.official-links span{font:9px Consolas,monospace;letter-spacing:.09em;color:#5d8d7e}.official-links a{padding:4px 7px;border:1px solid rgba(104,197,167,.15);border-radius:4px;color:#7db49f;font-size:10px;text-decoration:none}.official-links a:hover{color:#9fd6c0;border-color:rgba(104,197,167,.32)}@media(min-width:1180px){.prompt-writing-guide[open] .guide-body{grid-template-columns:repeat(2,minmax(0,1fr))}.prompt-writing-guide[open] .active-profile-guide,.prompt-writing-guide[open] .director-rule,.prompt-writing-guide[open] .translation-rule{grid-column:1/-1}}@media(max-width:720px){.profile-guide-facts{grid-template-columns:1fr}.configured-field-rules{display:block}.configured-field-rules li{display:grid;margin-top:6px;grid-template-columns:1fr}}
</style>
