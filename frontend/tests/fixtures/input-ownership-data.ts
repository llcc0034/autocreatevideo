import {recipeDefaultContract,recipeDefaultOptions,recipeDefaultRoute} from './recipe-default-data'

// Isolated declarations, never claims about a configured model or live defaults.
export function ownershipContract(){
  const value:any=recipeDefaultContract()
  const rules={
    width:{type:'integer',required:true,minimum:0,maximum:1024,default:0},
    sound:{type:'boolean',required:false,default:false},
    fixed:{type:'integer',required:true,const:1},
    seed:{type:'integer',required:false,minimum:0,maximum:100,seed_policy:'generate_once'},
    missing:{type:'string',required:true},
  }
  value.definition.interface.parameters=Object.fromEntries(Object.entries(rules).map(([key,rule])=>[key,{
    rule,value_source:Object.hasOwn(rule,'default')||Object.hasOwn(rule,'const')?'workflow.fixture.parameters':null,
    layers:[{source:'workflow.fixture.parameters',rule}],
  }]))
  return value
}
export const ownershipOptions={...recipeDefaultOptions,parameterPolicyOptions:[
  {value:'default',label:'本步骤默认值'},{value:'locked_value',label:'本步骤固定值'},{value:'locked_source',label:'本步骤固定来源'},
],parameterSourceOptions:[{selectorKey:'project.width',label:'项目宽度',scopeKind:'project',type:'integer',unit:'px',sourceKind:'context_scalar',parameterKey:null,stageKey:null,description:'项目中明确填写的宽度'}]}
export const ownershipRoute=()=>recipeDefaultRoute()
