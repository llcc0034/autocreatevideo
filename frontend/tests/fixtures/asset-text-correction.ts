import {createApp,h,ref} from 'vue'
import ExecutionPreparationWorkbench from '../../src/components/ExecutionPreparationWorkbench.vue'
import {adaptExecutionPreparationDetail} from '../../src/services/executionPreparation'
import {executionPreparationApi} from '../../src/services/executionPreparationApi'
import {api} from '../../src/services/api'
import {assetTextCorrectionData} from './asset-text-correction-data'
import '../../src/styles.css'
import '../../src/platform.css'

const mode=ref('success'), saved=ref(0), requests=ref(0), generation=ref(0)
let current=assetTextCorrectionData()
api.defaults.adapter=async()=>{throw Error('隔离夹具禁止业务网络')}
executionPreparationApi.continue=async()=>{generation.value++;throw Error('禁止模型调用')}
executionPreparationApi.get=async()=>{
  if(mode.value==='read-error') throw Error('隔离读取失败：原文文件不可读取')
  return adaptExecutionPreparationDetail(structuredClone(current))
}
executionPreparationApi.completeTextResponse=async(id,revision,review,sha,text)=>{
  requests.value++
  if(id!==current.id||revision!==current.revision||review!==current.text_response_review.id||sha!==current.text_response_review.original_sha256)throw Error('隔离身份不匹配')
  if(mode.value==='write-error')throw Error('隔离保存失败，修订未写入；编辑内容保留')
  current.revision++
  Object.assign(current.text_response_review,{editable:false,confirmed_at:'2026-10-02T01:02:00Z',confirmed_by:'fixture-human',corrected_text:text,corrected_sha256:'d'.repeat(64),readonly_reason:'已保存人工修订'})
  current.outcome={message:'隔离样例：人工修订结果已保存，未采用。',artifact_count:1,selected_count:0}
  saved.value++
  return adaptExecutionPreparationDetail(structuredClone(current))
}
createApp({setup(){return()=>h('main',{style:{maxWidth:'920px',margin:'16px auto',padding:'12px'}},[
  h('h1','独立素材正文补正 · 隔离样例'), h('p',`模拟提交 ${requests.value}；模拟保存 ${saved.value}；模型调用 ${generation.value}；零业务网络`),
  h('nav',{style:{display:'flex',gap:'8px',flexWrap:'wrap'}},[
    ['success','模拟保存成功'],['write-error','模拟保存失败'],['readonly','模拟已有重试'],['read-error','模拟读取失败'],
  ].map(([key,label])=>h('button',{class:'button',onClick:()=>{mode.value=key!;current=assetTextCorrectionData();if(key==='readonly')Object.assign(current.text_response_review,{editable:false,readonly_reason:'原调用已有重试，请查看后继任务，不能同时补正。'})}},label))),
  h(ExecutionPreparationWorkbench,{key:mode.value,preparationId:current.id,versionId:'fixture-version',purpose:'production'}),
])}}).mount('#app')
