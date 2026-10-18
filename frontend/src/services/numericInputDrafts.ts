import {computed,reactive,watch} from 'vue'

/** Local form state only: unfinished native number input must not delete a contract value. */
export function numericInputDrafts(fields:()=>{key:string;label:string;validate?:(raw:string)=>string|undefined}[],blocked:()=>boolean|undefined) {
  const drafts=reactive<Record<string,{raw:string;reason:string}>>({})
  const active=(key:string)=>fields().some(field=>field.key===key)
  const invalid=(key:string)=>active(key)&&key in drafts
  const errors=computed(()=>fields().filter(field=>invalid(field.key)).map(field=>`${field.label}：${drafts[field.key]?.reason}；请补全，或撤销未完成输入。原配置未改。`))
  watch(()=>JSON.stringify(fields().map(field=>field.key)),()=>{
    for(const key of Object.keys(drafts))if(!active(key))delete drafts[key]
  })
  function capture(key:string,event:Event):boolean {
    if(blocked()||!active(key))return false
    const input=event.target as HTMLInputElement
    const reason=input.validity?.badInput||(input.value!==''&&!Number.isFinite(Number(input.value)))
      ? '数字尚未完成或不是有限数值' : fields().find(field=>field.key===key)?.validate?.(input.value)
    if(reason){
      drafts[key]={raw:input.value,reason}
      return false
    }
    delete drafts[key]
    return true
  }
  function discard(key:string){if(!blocked()&&active(key))delete drafts[key]}
  // Structural list edits keep surviving drafts with their original row, not its old index.
  function remapKeys(rekey:(key:string)=>string|undefined){
    if(blocked())return
    const next:Record<string,{raw:string;reason:string}>={}
    for(const [key,raw] of Object.entries(drafts)){const target=rekey(key);if(target!==undefined)next[target]=raw}
    for(const key of Object.keys(drafts))delete drafts[key]
    Object.assign(drafts,next)
  }
  const value=(key:string,current:unknown)=>invalid(key)?drafts[key]?.raw:current??''
  // Only for an explicit whole-contract replacement, not a user undo action.
  function reset(){for(const key of Object.keys(drafts))delete drafts[key]}
  return {capture,discard,invalid,value,errors,remapKeys,reset}
}
