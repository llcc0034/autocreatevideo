import type { ComfySavedWorkflowItem, ComfySavedWorkflowPreview, ComfySavedWorkflowBatchItemInput } from '../../src/services/platformTypes'
import { savedWorkflowImportFingerprint } from '../../src/services/savedWorkflowRecovery.ts'

export const copy = <T>(value:T):T => JSON.parse(JSON.stringify(value))
export const profile:any={id:'fixture-model',profileKey:'fixture.reference-video',version:2,status:'published',modality:'video',provider:'fixture-provider',specification:{capabilities:[]},description:'隔离能力目录'}
export const template:any={id:'fixture-template',name:'隔离参考视频工作流',slug:'fixture-video',outputKind:'video'}
export const preview:ComfySavedWorkflowPreview={
  schemaVersion:1,sourcePath:'workflows/fixture-video.json',sourceSha256:'1'.repeat(64),sizeBytes:1200,sourceWorkflowId:'fixture-source',
  primaryOutputNodeIds:['81'],primaryApiNodeIds:['81'],outputCandidates:[{canvasNodeId:'81',nodeType:'SaveVideo',suggestedOutputKind:'video'}],
  modelDependencies:[],assetDependencies:[{nodeId:'7',nodeType:'LoadImage',inputName:'image',mediaKind:'image',valueType:'asset_image',suggestedSemanticKey:'asset.image.reference',inputSlot:0,assetCategory:'image',isRequired:true,minItems:1,maxItems:1}],
  conversionReport:{},objectInfoSchemaSha256:'2'.repeat(64),convertedGraphSha256:'3'.repeat(64),parameterSchemaSha256:'4'.repeat(64),structuralContractSha256:'5'.repeat(64),
  configurableParameters:[{semanticKey:'video.frames',sourceValue:39,recommendedSafeValue:39,defaultValue:22,min:5,max:260,schemaMin:5,schemaMax:260,multipleOf:null,alignmentModulus:17,alignmentRemainder:5,options:[],type:'integer',selectionGroup:'',target:{nodeId:'9',nodeType:'VideoSampler',inputName:'frames'}}],
  suggestedInputContract:{schema_version:1,parameters:{'video.frames':{type:'integer',min:5,max:260,default:22,alignment_modulus:17,alignment_remainder:5}},asset_slots:[{semantic_key:'asset.image.reference',media_kind:'image',input_slot:0,min_items:1,max_items:1}]},suggestedSemanticBindings:[],suggestedModelRequirements:[],blockers:[],liveValidateRequirements:[],admissionTier:'generic_fixed',actionability:'ready_for_executable_import',recoveryActions:[],contractDraftId:'fixture-contract',contractDraftRevision:1,
}
export function previewReceipt(deploymentId:string,input:any,candidateSourceId='fixture-candidate',result=preview):ComfySavedWorkflowPreview {
  return {...copy(result),sourcePath:input.sourcePath,sourceSha256:input.expectedSha256,
    primaryOutputNodeIds:[...input.primaryOutputNodeIds],requestReceipt:{requestKey:input.requestKey,candidateSourceId,
      request:{deploymentId,sourcePath:input.sourcePath,expectedSha256:input.expectedSha256,
        expectedSourceScopeSchemaVersion:input.expectedSourceScopeSchemaVersion,expectedSourceScopeSha256:input.expectedSourceScopeSha256,
        primaryOutputNodeIds:[...input.primaryOutputNodeIds],modelProfileVersion:input.modelProfileVersion??null}}}
}
export function source(overrides:Partial<ComfySavedWorkflowItem>={}):ComfySavedWorkflowItem{return{
  sourceScopeSchemaVersion:1,sourceScopeSha256:'6'.repeat(64),sourceKey:'fixture-key',sourceDeploymentId:'fixture-deployment',sourceProviderId:'fixture-provider',sourceRuntimeHostId:'fixture-host',sourcePath:preview.sourcePath,sourceSha256:preview.sourceSha256,sizeBytes:1200,sourceWorkflowId:'fixture-source',
  suggestedImportKind:'template_version',candidateManaged:true,candidateSourceId:'fixture-candidate',imported:false,sourceId:'',workflowVersionId:'',templateId:template.id,contractUpgradeRequired:false,errorCode:'',errorMessage:'',analysisStatus:'complete',analysisCode:'',analysisMessage:'',admissionTier:'generic_fixed',actionability:'ready_for_executable_import',recoveryActions:[],matchStatus:'matched',matchReasonCodes:[],matchActions:[],outputCandidates:copy(preview.outputCandidates),
  modelProfileCandidates:[{modelProfileVersionId:profile.id,profileKey:profile.profileKey,version:2,status:'published',modality:'video',targetDeploymentId:'fixture-deployment',eligible:true,liveValidateReady:true,eligibleOutputNodeIds:['81'],rejectionCodes:[],liveValidateRejectionCodes:[]}],matchedModelProfileVersionId:profile.id,matchedTargetDeploymentId:'fixture-deployment',matchedPrimaryOutputNodeId:'81',matchedOutputKind:'video',suggestedTemplate:{useExisting:true,templateId:template.id,name:template.name,slug:template.slug,outputKind:'video'},preview:null,...overrides,
}}
export function scan(items:ComfySavedWorkflowItem[]){return{schemaVersion:1,status:'complete',sourceCount:1,succeededSourceCount:1,failedSourceCount:0,matchedCount:items.length,reviewCount:0,effectiveScanMaxItems:10,effectiveScanMaxSeconds:30,effectiveRequestMaxBytes:100000,effectiveBatchMaxItems:1,effectiveBatchMaxSeconds:30,savedWorkflowDirectory:'workflows',sources:[{sourceScopeSchemaVersion:1,sourceScopeSha256:'6'.repeat(64),sourceDeploymentId:'fixture-deployment',providerId:'fixture-provider',runtimeHostId:'fixture-host',sourceLabel:'隔离工作流目录',deploymentCount:1,status:'succeeded',itemCount:items.length,errorCode:'',errorMessage:''}],items:copy(items)}}
export function receipt(deploymentId:string,items:ComfySavedWorkflowBatchItemInput[]){return{schemaVersion:1,deploymentId,status:'complete',createdCount:items.length,reusedCount:0,failedCount:0,items:items.map(row=>({sourceDeploymentId:deploymentId,sourcePath:row.sourcePath,sourceSha256:row.expectedSha256,sourceId:'fixture-candidate',importKind:row.importKind,result:'created',code:'',detail:'',workflowVersionId:row.importKind==='template_version'?'fixture-saved-version':'',structuralContractSha256:row.expectedStructuralContractSha256||''}))}}

export function recoveredSource(deploymentId:string,input:ComfySavedWorkflowBatchItemInput){return{
  id:'fixture-promoted-source',deploymentId,scopeVersion:input.expectedSourceScopeSchemaVersion,scopeSha256:input.expectedSourceScopeSha256,
  path:input.sourcePath,sha256:input.expectedSha256,kind:input.importKind,versionId:input.importKind==='template_version'?'fixture-saved-version':'',
  importContractSha256:'',report:{promotion_source:'comfy_workflow_contract_draft',contract_draft_projection_sha256:'a'.repeat(64),contract_draft_id:input.contractDraft,successor_of:input.successorOf||undefined,
    structural_contract_sha256:input.expectedStructuralContractSha256,source_converted_graph_sha256:input.expectedConvertedGraphSha256,
    object_info_schema_sha256:input.expectedObjectInfoSchemaSha256,primary_output_node_id:input.primaryOutputNodeIds?.[0]},
}}
export function recoveredVersion(input:ComfySavedWorkflowBatchItemInput):any{return{
  id:'fixture-saved-version',template:input.template||'fixture-new-template',templateName:input.newTemplate?.name||template.name,
  templateSlug:input.newTemplate?.slug||template.slug,outputKind:input.newTemplate?.outputKind||template.outputKind,modelProfileVersion:input.modelProfileVersion,
}}
export async function recoveredProof(deploymentId:string,input:ComfySavedWorkflowBatchItemInput){const row=recoveredSource(deploymentId,input)
  row.importContractSha256=await savedWorkflowImportFingerprint(input,recoveredVersion(input).template,'a'.repeat(64));return row
}

// Memory-only catalog shared by node tests and the browser fixture. Unlisted
// APIs throw; no fallback to live endpoints, generation or host operations.
export function installReads(api:any){
  for(const key of Object.keys(api))api[key]=async()=>{throw Error(`未授权夹具 API: ${key}`)}
  for(const key of ['listProviders','listDeployments','listWorkflowProjectors','listWorkflowProjectorVersions','listRuntimeHostProbeAttempts','listWorkflowVersions'])api[key]=async()=>[]
  api.listModelProfiles=async()=>[copy(profile)];api.listLegacyWorkflowTemplates=async()=>[copy(template)]
  api.getModelProfileRegistryOptions=async()=>({});api.getWorkflowProjectorOptions=async()=>({engines:[],authoringSeeds:[],definitionEditor:{sections:[]}})
  api.resolveConfiguration=async()=>({values:{'workflow.comfyui_saved_workflow_directory':'workflows','security.comfy.import.scan_max_seconds':30,'security.comfy.import.scan_max_items':10,'security.comfy.import.request_max_bytes':100000},sources:{}})
}
