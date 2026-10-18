import type {JsonObject} from './platformTypes'
import type {ScriptSemanticOptions} from './scriptSemantics'

const object=(value:unknown):JsonObject=>value&&typeof value==='object'&&!Array.isArray(value)?value as JsonObject:{}
const EACH=Symbol('each')
type Token=string|number|typeof EACH
function pathText(tokens:Token[]){return tokens.map(token=>typeof token==='number'?`[${token}]`:token===EACH?'[*]':/^[A-Za-z_][\w]*$/.test(token)?`.${token}`:`[${JSON.stringify(token)}]`).join('')}

// Describe declarations only. No sample evaluation, field-name guessing or validation.
function sourceDescription(value:unknown,remaining:Token[],options:ScriptSemanticOptions,item?:string):string {
  const node=object(value),kind=node.kind
  if(kind==='path'){
    if(!Array.isArray(node.path)||!node.path.every(token=>typeof token==='string'||typeof token==='number'))return '路径尚未填写完整'
    const base=node.scope==='root'?'响应':node.scope==='item'?item:undefined
    return base?base+pathText([...node.path as Token[],...remaining]):'当前条目来源尚未确定；查看完整映射'
  }
  if(kind==='object'){
    if(!remaining.length)return '按字段组成对象；展开查看各字段'
    return sourceDescription(object(node.fields)[String(remaining[0])],remaining.slice(1),options,item)
  }
  if(kind==='each'){
    const source=sourceDescription(node.source,[],options,item)
    if(remaining[0]===EACH)return sourceDescription(node.item,remaining.slice(1),options,`（${source}）[*]`)
    return `${source} → 逐项映射`
  }
  if(kind==='literal')return '配置固定值（不是模型返回）；查看完整映射'
  if(kind==='lookup')return '按稳定 key 查找；实际来源条目由返回值决定，查看完整映射'
  if(kind==='array')return '按顺序组成列表；各项来源分别配置，查看完整映射'
  if(!kind)return '未映射（不会自动补值）'
  const label=options.mapping.nodes.find(row=>row.value===kind)?.label
  return label?`${label}；查看完整映射`:'目录未声明此取值方式；查看完整映射'
}

export function scriptSemanticGuideGroups(options:ScriptSemanticOptions,mapping:JsonObject){
  return Object.entries(object(options.json_schema.properties)).map(([key,definition])=>{
    const rows:{path:string;label:string;source:string}[]=[]
    function visit(value:unknown,tokens:Token[]){
      const schema=object(value)
      if(schema.type==='object'){
        for(const [name,child] of Object.entries(object(schema.properties)))visit(child,[...tokens,name])
      }else if(schema.type==='array'&&['object','array'].includes(String(object(schema.items).type))){
        visit(schema.items,[...tokens,EACH])
      }else rows.push({path:pathText(tokens).replace(/^\./,''),label:typeof schema.title==='string'?schema.title:pathText(tokens),source:sourceDescription(mapping.node,tokens,options)})
    }
    visit(definition,[key])
    return {key,label:typeof object(definition).title==='string'?String(object(definition).title):key,rows}
  })
}
