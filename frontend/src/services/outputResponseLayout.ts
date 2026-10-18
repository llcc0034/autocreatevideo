import type {JsonObject} from './platformTypes'

const object=(value:unknown):value is JsonObject=>!!value&&typeof value==='object'&&!Array.isArray(value)
const owns=(value:object,key:string)=>Object.prototype.hasOwnProperty.call(value,key)
const indexPart=(part:string)=>/^(0|[1-9][0-9]{0,63})$/.test(part)

export function responseValueAt(payload:unknown,path:string):unknown {
  let value=payload
  for(const part of path.split('.')) {
    if(indexPart(part)){
      const index=Number(part)
      if(!Array.isArray(value)||!Number.isSafeInteger(index)||index>=value.length||!owns(value,String(index)))throw new Error(`响应位置 ${path} 缺失或父级不是数组`)
      value=value[index]
    }else{
      if(!part||!object(value)||!owns(value,part))throw new Error(`响应位置 ${path} 缺失或父级不是对象`)
      value=value[part]
    }
  }
  return value
}

export function packResponseExample(schemaVersion:unknown,values:{path:string;value:unknown}[]):JsonObject {
  if(new Set(values.map(row=>row.path)).size!==values.length)throw new Error('响应位置重复')
  for(const {path} of values){
    const parts=path.split('.')
    if(!/^[a-z][a-z0-9_]{0,63}$/.test(parts[0]||'')||parts.length>4
      ||parts.some(part=>!(/^[a-z][a-z0-9_]{0,63}$/.test(part)||indexPart(part))||['schema_version','constructor','prototype','__proto__'].includes(part))
      ||values.some(other=>other.path.startsWith(path+'.')))throw new Error('响应位置重复、互为父子或包含保留字')
  }
  const build=(entries:{parts:string[];value:unknown}[]):unknown=>{
    if(entries.some(row=>!row.parts.length)){
      if(entries.length!==1||entries[0]!.parts.length)throw new Error('响应位置不得互为父子')
      return entries[0]!.value
    }
    const groups=new Map<string,{parts:string[];value:unknown}[]>()
    for(const {parts,value} of entries){const key=parts[0]!;groups.set(key,[...(groups.get(key)||[]),{parts:parts.slice(1),value}])}
    const keys=[...groups.keys()]
    if(keys.some(indexPart)){
      // Validate before allocating: large indices must not create sparse arrays.
      if(!keys.every(indexPart)||keys.some(key=>!Number.isSafeInteger(Number(key))||Number(key)>=keys.length))throw new Error('响应数组下标必须从0连续完整声明，不能留空或混用对象字段')
      return Array.from({length:keys.length},(_,index)=>build(groups.get(String(index))!))
    }
    const result:JsonObject=Object.create(null)
    for(const [key,children] of groups)result[key]=build(children)
    return result
  }
  return JSON.parse(JSON.stringify({schema_version:schemaVersion,...build(values.map(row=>({parts:row.path.split('.'),value:row.value}))) as JsonObject})) as JsonObject
}

export function validateResponseExample(payload:JsonObject,paths:string[]):void {
  const shape=packResponseExample(null,paths.map(path=>({path,value:null})))
  const check=(value:unknown,expected:JsonObject|unknown[])=>{
    if(Array.isArray(expected)){
      if(!Array.isArray(value)||value.length!==expected.length||Object.keys(value).length!==expected.length)throw new Error('响应示例数组长度不符或不是数组')
      for(let index=0;index<expected.length;index++)if(!owns(value,String(index)))throw new Error('响应示例数组不能留空')
    }else if(!object(value)||Object.keys(value).length!==Object.keys(expected).length
      ||Object.keys(expected).some(key=>!owns(value,key)))throw new Error('响应示例含缺失或未声明的对象字段')
    for(const [key,child] of Object.entries(expected))if(object(child)||Array.isArray(child))check((value as JsonObject)[key],child)
  }
  check(payload,shape)
}
