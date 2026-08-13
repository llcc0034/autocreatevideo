<script setup lang="ts">
import { computed, ref } from 'vue'
import { Check, Clipboard, ChevronDown, Sparkles } from 'lucide-vue-next'

const props = withDefaults(defineProps<{ scope?: 'project' | 'bible' | 'shot' }>(), { scope: 'project' })
const copied = ref('')

const projectTemplate = `第1集：雨夜来客
镜头1：雨夜客栈外，沈砚推门进入，右手始终握住玄纹剑。
沈砚（in a low, restrained voice）：谁在等我？
镜头2：掌柜退到灯下，视线落在玄纹剑上。
掌柜（voiceover; softly, with controlled breath）：你终于来了。`

const bibleTemplate = `【人物 Character visualPrompt】Lean young Chinese swordsman, narrow angular face, a shallow scar at the tail of his right eyebrow, long black hair tied in a high topknot; permanently wears a weathered blue-gray cross-collar robe and dark leather wrist guards; preserve facial geometry, wardrobe colors, and material texture in every shot.

【场景 Scene visualPrompt】Rain-soaked traditional Chinese inn with fixed dark timber columns, the entrance on frame right, a long counter against the rear wall, and a continuous second-floor gallery; cold cyan moonlight enters through torn paper windows, with dim warm amber lantern fill; preserve the architecture and light directions.

【道具 Prop visualPrompt】Narrow straight black-steel sword, worn silver guard, dark-brown cord-wrapped grip, and three diagonal abrasion marks at the end of the scabbard; preserve scale, silhouette, materials, colors, and mark positions.`

const directorTemplate = `Visual style: cinematic wuxia realism, restrained contrast, natural skin texture.
Scene: define the fixed environment, layout, time of day, lighting direction, and palette.
Subjects: define immutable identity, face, hair, build, wardrobe color, and material.
Props: define shape, scale, material, color, and persistent marks.
Observable action sequence: Starting state -> primary action -> environment response -> final state. Use present tense and only visible, physically executable changes.
Camera: state shot size, angle, lens behavior, motivated movement, speed, and final framing.
On-screen dialogue: The on-screen character (S1) says in a low, restrained voice: <d>[Chinese]台词原文</d>.
Voiceover: The referenced subject (S1) says in an off-screen voiceover, softly: <d>[Chinese]台词原文</d>. The visible character keeps their lips closed.
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

const baseStructure = `integrated_multimodal_description:
[Shot 1] ...
[Shot 2] At 00:05.167, ...
overall_soundscape: ...
non_diegetic_music: ...`

const refStructure = `subject_definitions:
summary:
retention_analysis:
detailed_description:
overall_soundscape:
non_diegetic_music:`

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
    <summary><span><Sparkles :size="14"/><strong>字段写作模板与 H3 导演描述规则</strong></span><ChevronDown :size="14"/></summary>
    <div class="guide-body">
      <article v-if="showProject">
        <header><div><span>PROJECT SCRIPT</span><strong>全剧母稿可识别模板</strong></div><button type="button" @click="copyText('project',projectTemplate)"><component :is="copied==='project'?Check:Clipboard" :size="12"/>{{ copied==='project'?'已复制':'复制模板' }}</button></header>
        <pre>{{ projectTemplate }}</pre>
        <p>人物、场景和道具名称应与 Bible 完全一致；人物名后的括号会保存为对白演绎指令，请使用英文。括号含独立 <code>voiceover</code> token 时会生成画外旁白并要求画面人物闭唇；对白正文保持原文。</p>
      </article>
      <article v-if="showBible">
        <header><div><span>CONSISTENCY BIBLE</span><strong>人物 / 场景 / 道具锚点模板</strong></div><button type="button" @click="copyText('bible',bibleTemplate)"><component :is="copied==='bible'?Check:Clipboard" :size="12"/>{{ copied==='bible'?'已复制':'复制模板' }}</button></header>
        <pre>{{ bibleTemplate }}</pre>
        <p>实体名称和 visualPrompt 都可以使用中文。新建 Bible 时，中文 visualPrompt 会自动生成独立的英文候选；必须人工核对并点击“确认译文”后才能进入正式 H3 提示词，中文原文不会被覆盖。若直接填写英文则无需翻译。这里只写不可变化的视觉事实；动作、情绪、运镜和临时光效应留在具体分镜中。</p>
      </article>
      <article v-if="showShot">
        <header><div><span>SHOT INPUT TEMPLATE</span><strong>单镜高质量英文填写模板</strong></div><button type="button" @click="copyText('shot',shotTemplate)"><component :is="copied==='shot'?Check:Clipboard" :size="12"/>{{ copied==='shot'?'已复制':'复制模板' }}</button></header>
        <pre>{{ shotTemplate }}</pre>
        <p>5 秒优先一条主动作链和一种主要运镜；按“起始状态 → 主动作 → 环境响应 → 结束状态”填写。对白正文可以中文；括号内 delivery、人物动作、构图、运镜、声景和配乐会进入导演描述，必须用英文。当前只支持 delivery 与 voiceover，不支持 scenetrans / cutoff。</p>
      </article>
      <article v-if="showShot" class="mode-contracts">
        <header><div><span>MODE CONTRACTS</span><strong>系统自动装配的官方字段顺序</strong></div></header>
        <div class="contract-grid"><div><b>T2VA / I2VA / FL2VA / L2VA</b><pre>{{ baseStructure }}</pre></div><div><b>Ref2VA</b><pre>{{ refStructure }}</pre></div></div>
        <ul><li>首帧、尾帧和 17k+5 有效帧时长的对齐句由系统自动生成，不要手写 Picture 编号。</li><li>Ref 图片只负责对应人物、场景或道具身份；参考视频只负责声明的动作、运镜或节奏；音频只负责声明的音色、声音或音乐。</li><li>输入标签按实际连接顺序自动生成；没有素材时不会创建空 Picture / Video / Audio 连接。</li></ul>
      </article>
      <article class="director-rule">
        <header><div><span>OFFICIAL ENGLISH STRUCTURE</span><strong>H3 英文导演描述规则</strong></div><button type="button" @click="copyText('director',directorTemplate)"><component :is="copied==='director'?Check:Clipboard" :size="12"/>{{ copied==='director'?'已复制':'复制规则' }}</button></header>
        <pre>{{ directorTemplate }}</pre>
        <ul><li>使用现在时，只描述可见、可听、可执行的事实。</li><li>第一镜不写时间戳；后续镜头由系统按 <code>[Shot N] At MM:SS.mmm</code> 自动写入严格递增时间。</li><li>参考输入必须使用工作流给出的精确标签，如 &lt;Picture 1&gt;、&lt;Video 1&gt;、&lt;Audio 1&gt;。</li><li>对白、歌词和真实屏显文字保留原语言；人物动作、构图、运镜、声景和配乐使用英文。</li><li>负面约束与正向导演描述分开，避免同一缺陷同时出现在正负提示词。</li></ul>
        <nav class="official-links" aria-label="MiniMax H3 官方资料"><span>官方资料</span><a href="https://github.com/MiniMax-AI/MiniMax-H3/blob/main/skills/h3-prompt-writing/references/base-en.txt" target="_blank" rel="noreferrer">Base 提示词指南</a><a href="https://github.com/MiniMax-AI/MiniMax-H3/blob/main/skills/h3-prompt-writing/references/ref-en.txt" target="_blank" rel="noreferrer">Ref2VA 提示词指南</a><a href="https://docs.comfy.org/tutorials/video/minimax/minimax-h3" target="_blank" rel="noreferrer">ComfyUI H3 工作流</a></nav>
      </article>
      <article class="translation-rule">
        <header><div><span>LOCAL QWEN REVIEW</span><strong>中文源文 → 英文候选 → 人工确认</strong></div></header>
        <ul><li>本地 ModelScope <code>Qwen/Qwen3-4B-GGUF</code> 只生成英文候选译文，不覆盖中文原文；它不是 DashScope 的 Qwen-MT API。本地 4B 候选不可直接生产，未确认、失败或源文已变更的译文绝不会进入正式 prompt。</li><li>确认前重点核对专名、人物性别、左/右手、道具部位、数值与比例、动作先后、镜头运动、光线方向和最终状态；可直接编辑英文译文再确认。</li><li>在连续剧工作室按“中文术语 = Canonical English”配置术语表，例如 <code>玄纹剑 = Xuanwen Sword</code>。未配置的实体名称会使用 S1、scene:slug、prop:slug 等稳定技术标识，系统不会自由猜译专名。</li><li>对白、歌词和真实屏显文字保持原语言；只翻译人物动作、构图、运镜、delivery、连续性、声景和配乐等导演字段。</li><li>项目名、镜头标题和实体名称主要用于标识；母稿与基础描述用于拆解或创作管理。以字段旁的“进入提示词 / 连续性参考”徽标判断是否需要翻译审核。</li></ul>
      </article>
    </div>
  </details>
</template>

<style scoped>
.prompt-writing-guide{border:1px solid rgba(159,140,255,.16);border-radius:8px;background:rgba(159,140,255,.025);overflow:hidden}.prompt-writing-guide summary{min-height:44px;padding:8px 12px;display:flex;align-items:center;justify-content:space-between;color:#8d84bd;cursor:pointer;list-style:none}.prompt-writing-guide summary::-webkit-details-marker{display:none}.prompt-writing-guide summary>span{display:flex;align-items:center;gap:8px}.prompt-writing-guide summary strong{font-size:12px;color:#aaa4c9}.prompt-writing-guide[open] summary>svg{transform:rotate(180deg)}.guide-body{padding:0 10px 10px;display:grid;gap:9px}.guide-body article{min-width:0;padding:11px;border:1px solid rgba(255,255,255,.055);border-radius:6px;background:#0a0d11}.guide-body article>header{display:flex;align-items:center;justify-content:space-between;gap:10px}.guide-body article>header>div{display:flex;flex-direction:column;gap:3px}.guide-body article>header span{font:10px Consolas,monospace;letter-spacing:.1em;color:#6f6796}.guide-body article>header strong{font-size:12px;color:#aaaeb4}.guide-body button{min-height:30px;padding:5px 9px;display:flex;align-items:center;gap:5px;border:1px solid var(--border);border-radius:4px;background:#12161b;color:#8d939a;font-size:10px;cursor:pointer}.guide-body button:hover{color:var(--accent);border-color:rgba(230,167,90,.25)}.guide-body pre{max-height:240px;margin:9px 0 7px;padding:10px;overflow:auto;border-radius:4px;background:#07090c;color:#9aa69e;font:10px/1.65 Consolas,monospace;white-space:pre-wrap;word-break:break-word}.guide-body p,.guide-body li{font-size:11px;line-height:1.6;color:#777e86}.guide-body ul{margin:8px 0 0;padding-left:18px}.guide-body code{font:10px Consolas,monospace;color:#a9a1d9}.contract-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:9px}.contract-grid b{font-size:10px;color:#8d949b}.contract-grid pre{margin-top:5px}.director-rule{border-color:rgba(104,197,167,.12)!important}.director-rule header span{color:#5d8d7e!important}.translation-rule{border-color:rgba(94,164,218,.14)!important}.translation-rule header span{color:#5689ad!important}.official-links{display:flex;align-items:center;flex-wrap:wrap;gap:7px;margin-top:10px;padding-top:9px;border-top:1px solid rgba(255,255,255,.05)}.official-links span{font:9px Consolas,monospace;letter-spacing:.09em;color:#5d8d7e}.official-links a{padding:4px 7px;border:1px solid rgba(104,197,167,.15);border-radius:4px;color:#7db49f;font-size:10px;text-decoration:none}.official-links a:hover{color:#9fd6c0;border-color:rgba(104,197,167,.32)}@media(min-width:1180px){.prompt-writing-guide[open] .guide-body{grid-template-columns:repeat(2,minmax(0,1fr))}.prompt-writing-guide[open] .director-rule,.prompt-writing-guide[open] .translation-rule{grid-column:1/-1}}@media(max-width:720px){.contract-grid{grid-template-columns:1fr}}
</style>
