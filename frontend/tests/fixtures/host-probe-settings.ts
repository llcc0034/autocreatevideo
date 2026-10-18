// UI-only fixture: no business API, configuration saves, or probes.
import {createApp,h,ref} from 'vue'
import ConfigValueEditor from '../../src/components/ConfigValueEditor.vue'
import type {ConfigDefinition} from '../../src/services/platformTypes'
import '../../src/styles.css'
import '../../src/platform.css'

const mode=ref('"automatic"'),seconds=ref('2592000'),days=ref('36500')
const definition=(key:string,description:string,valueType:string,unit:string,valueSchema:object)=>({key,description,valueType,unit,valueSchema} as ConfigDefinition)
const fields=[
  [mode,definition('runtime.host_probe_mode','主机探测模式','string','mode',{enum:['automatic','manual'],enum_labels:{automatic:'自动探测',manual:'手动探测'}})],
  [seconds,definition('runtime.capability_ttl_seconds','自动探测有效期（秒）','integer','seconds',{minimum:30,maximum:31536000})],
  [days,definition('runtime.host_probe_manual_validity_days','手动探测长期有效期（天）','integer','days',{minimum:1,maximum:36500})],
] as const
createApp({render:()=>h('main',{style:{maxWidth:'780px',margin:'16px auto',padding:'16px',display:'grid',gap:'16px'}},[
  h('h1',{style:{fontSize:'22px'}},'主机探测设置 · 隔离验证'),
  h('p','仅验证正式配置编辑控件，不连接业务API、不保存系统配置、不执行探测。'),
  ...fields.map(([value,def])=>h('label',{style:{display:'grid',gap:'8px'}},[
    h('strong',def.description),h(ConfigValueEditor,{definition:def,modelValue:value.value,'onUpdate:modelValue':(next:string)=>{value.value=next}}),
  ])),
  h('p',{role:'status'},mode.value==='"manual"'?`手动探测 · 新结果有效期 ${days.value} 天，不自动刷新。`:`自动探测 · 新结果有效期 ${Number(seconds.value)/86400} 天，按配置提前刷新。`),
  h('p','所有模块仍按原 expires_at 检查；设置改变不改写已有记录。再次探测后应用新期限。'),
])}).mount('#app')
