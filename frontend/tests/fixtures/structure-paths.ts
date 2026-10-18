// Actual editor with declared fixture schemas only; no API, saved values or models.
import {createApp,h,ref} from 'vue'
import RecipeStructureMappingEditor from '../../src/components/RecipeStructureMappingEditor.vue'
import {mappingErrors} from '../../src/services/recipeStructureMapping'
import type {JsonObject} from '../../src/services/platformTypes'
import '../../src/styles.css'
import '../../src/platform.css'
const schema:JsonObject={type:'object',properties:{'a.b':{type:'array',items:{type:'object',properties:{'嵌套':{type:'array',items:{type:'object',properties:{'情绪':{type:'string',title:'自定义情绪'}}}}}}}}}
const fields=[{selector_key:'fixture',label:'隔离结构化来源',kind:'json',scope_kind:'project',revision_kind:'fixture',description:'显式测试合同，不读取任何真实产物。',value_schema:schema}]
const initial=():JsonObject=>({kind:'each',source:{kind:'context_field',selector_key:'fixture',scope:{kind:'project'},path:['a.b']},item:{kind:'each',source:{kind:'item_field',path:['嵌套']},item:{kind:'item_field',path:['情绪']}}})
const target:JsonObject={type:'array',items:{type:'array',items:{type:'string'}}}
const nodes=[['object','按字段组成对象'],['array','固定列表'],['each','逐条映射列表'],['literal','固定值'],['context_field','受控来源'],['selected_output','已采用输出'],['item_field','当前条目字段']].map(([value,label])=>({value:value!,label:label!}))
createApp({setup(){
  const value=ref(initial()),disabled=ref(false)
  return()=>h('main',{style:{maxWidth:'940px',margin:'16px auto',padding:'12px',color:'#e6edf3',background:'#101820'}},[
    h('h1','深层路径 · 隔离验证'),h('p','字段含点号和中文名称，两层列表映射；不保存、不读取业务API、不生成。'),
    h('button',{class:'button ghost',onClick:()=>disabled.value=!disabled.value},disabled.value?'切换可编辑':'切换只读'),
    h('button',{class:'button ghost',onClick:()=>value.value={kind:'context_field',selector_key:'fixture',scope:{kind:'project'},path:['a.b',0,'missing']}},'展示失效路径'),
    h('button',{class:'button ghost',onClick:()=>value.value=initial()},'恢复嵌套样例'),
    h(RecipeStructureMappingEditor,{modelValue:value.value,schema:target,label:'嵌套输出',nodes,fields,outputs:[],scopes:['project'],priorStages:[],disabled:disabled.value,'onUpdate:modelValue':(v:JsonObject)=>value.value=v}),
    h('p',{'aria-label':'校验结果'},mappingErrors(value.value,target,'映射',[],{fields,outputs:[]}).join('；')||'配置校验通过；不代表实际执行'),
    h('output',{'aria-label':'原始映射',style:{display:'block',overflowWrap:'anywhere'}},JSON.stringify(value.value)),
  ])
}}).mount('#app')
