// Content comparison only; never resolves defaults, versions or execution rules.
export function sameJson(a:unknown,b:unknown):boolean {
  if(a===b)return true
  if(a===null||b===null||typeof a!=='object'||typeof b!=='object')return false
  if(Array.isArray(a)||Array.isArray(b))return Array.isArray(a)&&Array.isArray(b)&&a.length===b.length&&a.every((value,index)=>sameJson(value,b[index]))
  const left=a as Record<string,unknown>,right=b as Record<string,unknown>,keys=Object.keys(left)
  return keys.length===Object.keys(right).length&&keys.every(key=>Object.prototype.hasOwnProperty.call(right,key)&&sameJson(left[key],right[key]))
}
export function sameStructureValue(key:string,a:unknown,b:unknown):boolean {
  // Node order is explicitly carried by position/parent; the serializer may
  // enumerate nodes differently. Arrays inside defaults/constraints stay ordered.
  if(key==='nodes'&&Array.isArray(a)&&Array.isArray(b)&&[...a,...b].every(node=>node&&typeof node.node_key==='string')){
    const sort=(items:any[])=>[...items].sort((x,y)=>x.node_key<y.node_key?-1:x.node_key>y.node_key?1:0)
    return sameJson(sort(a),sort(b))
  }
  return sameJson(a,b)
}
