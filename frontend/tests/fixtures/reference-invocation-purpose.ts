// Isolated local form only. No API, saved business facts or model calls.
import {createApp, h, ref} from 'vue'
import RecipeReferenceMappingEditor from '../../src/components/RecipeReferenceMappingEditor.vue'
import '../../src/styles.css'
import '../../src/platform.css'
const options:any={referenceSourceOptions:[{value:'images',label:'图片素材',kind:'image',paired:false,stageKey:'storyboard_review',scopeKind:'shot'},
  {value:'videos',label:'视频素材',kind:'video',paired:false,stageKey:'storyboard_review',scopeKind:'shot'}]}
createApp({setup(){
  const locked=ref(false)
  const inputs=['image','video'].map(kind=>({key:kind,kind,semantic_contract:{paired_with_input_key:null,allowed_scope_kinds:['shot'],allowed_subject_kinds:[],
    allowed_purposes:kind==='video'?['motion_camera_reference','composition_reference']:[]}}))
  const sources=ref(inputs.map(input=>({kind:'reviewed_references',stage_key:'storyboard_review',scope:{kind:'shot'},
    groups:[{source_key:input.kind==='image'?'images':'videos',purposes:null,subject_kinds:null}]})))
  return()=>h('main',{style:'max-width:960px;margin:auto;padding:20px'},[
    h('h1','本次调用用途 · 隔离表单'),h('p','使用正式组件，仅本地表单状态，不保存配置、不调用模型。图片无需额外用途；视频必须按目标合同明确选择。'),
    h('label',[h('input',{type:'checkbox',checked:locked.value,onChange:()=>locked.value=!locked.value}),'只读']),
    ...inputs.map((input,index)=>h('section',{style:'margin:20px 0'},[h('h2',input.kind==='image'?'图片槽位':'视频槽位'),
      h(RecipeReferenceMappingEditor,{modelValue:sources.value[index],input,options,priorStages:[{key:'storyboard_review'}],disabled:locked.value,
        'onUpdate:modelValue':(value:any)=>{sources.value[index]=value}})])),
    h('pre',{style:'white-space:pre-wrap;overflow-wrap:anywhere'},JSON.stringify(sources.value,null,2)),
  ])
}}).mount('#app')
