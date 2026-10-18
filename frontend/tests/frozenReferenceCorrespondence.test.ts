import assert from 'node:assert/strict'
import test from 'node:test'
import {createServer} from 'vite'
import {fileURLToPath} from 'node:url'
import {createSSRApp} from 'vue'
import {renderToString} from 'vue/server-renderer'
import {adaptFrozenReferenceBinding} from '../src/services/frozenReferenceBinding.ts'
import {frozenCorrespondenceRows} from './fixtures/frozen-correspondence.ts'
const root=fileURLToPath(new URL('..',import.meta.url))
test('server names, order and exact versions survive unchanged; missing or malformed correspondence fails',()=>{
  for(const row of frozenCorrespondenceRows())assert.deepEqual(adaptFrozenReferenceBinding(row.referenceBinding),row.referenceBinding)
  for(const key of ['position','original_name','reference_name','name_disambiguated','input_slot','shot_reference']){
    const data:any=structuredClone(frozenCorrespondenceRows()[0]!.referenceBinding);delete data[key]
    assert.throws(()=>adaptFrozenReferenceBinding(data),key)
  }
  for(const changes of [{position:-1},{input_slot:0},{name_disambiguated:'false'},{reference_name:null}]){
    assert.throws(()=>adaptFrozenReferenceBinding({...frozenCorrespondenceRows()[0]!.referenceBinding,...changes}))
  }
})
test('formal component shows same-asset distinct versions, creative note, Unicode aliases and real labels; never edits or fetches',async()=>{
  const vite=await createServer({root,logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
  try {
    const C=(await vite.ssrLoadModule('/src/components/FrozenTaskReferences.vue')).default
    const rows=frozenCorrespondenceRows(), task={id:'isolated',inputContractStatus:'available',referenceAssets:rows.slice(0,4),contextMediaUses:rows.slice(4)},before=JSON.stringify(task)
    const html=await renderToString(createSSRApp(C,{task}))
    for(const text of ['旅人 [isolated-use-0]','旅人 [isolated-use-1]','Ａlice [isolated-use-2]','Alice [isolated-use-3]',
      '系列版本 v1','系列版本 v2','旅人设定 · r1','旅人设定 · r2','version-1','version-2','同一人物的外观参考',
      '直接素材 · 未选择系列版本','未绑定设定修订','未记录分镜选材版本','未记录策略引用名','已按使用 ID 消歧',
      '&lt;Picture 2&gt;','&lt;Picture 1&gt;','&lt;Video 1&gt;','&lt;Audio 1&gt;','配对来源','仅提示词上下文','&lt;script&gt;仅原文&lt;/script&gt;'])assert.ok(html.includes(text),text)
    assert.doesNotMatch(html,/<script>|<img|<video|<audio|<select|type="checkbox"/)
    assert.equal(JSON.stringify(task),before)
  }finally{await vite.close()}
})
