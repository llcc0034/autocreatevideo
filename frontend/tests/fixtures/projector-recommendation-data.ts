import {definition} from './projector-authoring-data.ts'
import {copy,source} from './workflow-import-data.ts'
import type {WorkflowProjectorRecommendation} from '../../src/services/platformTypes'

// Synthetic, memory-only graph contract. Not evidence of a supported real graph.
export const recommendationSource=source({sourcePath:'workflows/fixture-image.json',
  outputCandidates:[{canvasNodeId:'81',nodeType:'SaveImage',suggestedOutputKind:'image'}],
  matchedOutputKind:'image',modelProfileCandidates:[],matchedModelProfileVersionId:'',
  suggestedTemplate:{useExisting:false,templateId:'',name:'隔离图片方案',slug:'fixture-image',outputKind:'image'},
})
export const complexDefinition={...copy(definition),priority:20,
  detector:{fallback:false,allow_other_nodes:false,node_counts:{FixtureSampler:1,SaveImage:1},optional_node_counts:{FixtureNote:{min:0,max:2}},input_equals:[]},
  node_roles:{sampler:{node_type:'FixtureSampler',cardinality:'one'},output:{node_type:'SaveImage',cardinality:'one'}},
  required_edges:[{from_role:'sampler',output_index:0,to_role:'output',input_name:'images'}],
  parameter_policies:[
    {semantic_key:'image.iterations',node_role:'sampler',input_name:'steps',policy:'runtime',value:null,constraints:{min:1,max:40,multiple_of:1,default:20}},
    {semantic_key:'image.quality_mode',node_role:'sampler',input_name:'quality',policy:'fixed',value:'balanced'},
  ],
  projection:{kind:'generic_fixed',config:{capability:'image.text_to_image'}},
}
export function recommendation(overrides:Partial<WorkflowProjectorRecommendation>={}):WorkflowProjectorRecommendation{return{
  schemaVersion:1,persistence:'unsaved_draft',requiresHumanConfirmation:true,
  engineKey:'generic.fixed',engineVersion:1,providerDriverType:'comfyui',operation:'workflow.import',outputKind:'image',
  definition:copy(complexDefinition),sourceEvidence:{candidateSourceId:recommendationSource.candidateSourceId,
    sourceScopeSchemaVersion:1,sourceScopeSha256:recommendationSource.sourceScopeSha256,sourceSha256:recommendationSource.sourceSha256,
    sourcePathSha256:'2'.repeat(64),objectInfoSchemaSha256:'3'.repeat(64),convertedGraphSha256:'4'.repeat(64),
    primaryCanvasNodeId:'81',primaryApiNodeId:'81',primaryNodeType:'SaveImage'},
  catalog:{nodeTypes:[{value:'FixtureSampler',count:1,primaryOutput:false},{value:'SaveImage',count:1,primaryOutput:true}],
    inputsByNodeType:{FixtureSampler:['steps','quality'],SaveImage:['images']},
    observedEdges:[{fromNodeType:'FixtureSampler',outputIndex:0,toNodeType:'SaveImage',inputName:'images',recommended:true}],
    parameterTargets:[{semanticKey:'image.iterations',nodeType:'FixtureSampler',inputName:'steps',valueType:'integer'}]},
  warnings:['隔离推荐仅供编辑验证，不代表真实工作流已编译或通过准入。'],recommendationSha256:'5'.repeat(64),...overrides,
}}
