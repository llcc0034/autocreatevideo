// Explicit, isolated DTO examples; never a production configuration or Smoke result.
export const workflowContractFixture = () => ({
  id: '00000000-0000-4000-8000-000000000001', version: 2, status: 'draft', isActive: true,
  templateName: '隔离参考视频工作流', outputKind: 'video', provider: '', modelProfileVersion: '',
  graphSha256: 'a'.repeat(64), executionContractSha256: 'b'.repeat(64),
  graph: {}, inputContract: {}, validationState: 'not_run', smokeTestState: 'not_run',
  validatedDeployment: '', semanticBindings: [], modelRequirements: [], semanticBindingCount: 0, modelRequirementCount: 0,
  workflowInputSchema: { available: true, reasonCode: '', parameters: [
    {semanticKey:'video.frames',type:'integer',required:true,min:5,max:3600,multipleOf:null,alignmentModulus:17,alignmentRemainder:5,options:[5,22,39],selectionGroup:'video-length',recommendedValue:22},
    {semanticKey:'seed',type:'integer',required:false,min:0,max:null,multipleOf:1,alignmentModulus:null,alignmentRemainder:null,options:[],selectionGroup:'',recommendedValue:0},
    {semanticKey:'custom.enabled',type:'boolean',required:false,min:null,max:null,multipleOf:null,alignmentModulus:null,alignmentRemainder:null,options:[false,true],selectionGroup:'',recommendedValue:false},
  ], assetSlots:[{semanticKey:'custom.reference',inputSlot:'ref_image_0',mediaKind:'image',required:true,minItems:1,maxItems:2}],assetGroups:[],dynamicReferenceModes:{},
    dynamicReferences:{mode:'Ref2VA',conditioningMode:'reference',pictures:{minItems:0,maxItems:3,categories:['character','scene']},videos:{minItems:0,maxItems:2,purposes:['motion_reference'],defaultPurposeKey:'',supportsEmbeddedAudio:true,supportsExternalAudio:true},audios:{minItems:0,maxItems:2},maximumPhysicalAssets:5},
  },
})
export const workflowPreviewFixture = () => ({
  configurableParameters:[{semanticKey:'video.frames',type:'integer',min:5,max:3600,multipleOf:null,alignmentModulus:17,alignmentRemainder:5,options:[5,22,39],selectionGroup:'video-length',sourceValue:39,defaultValue:22,recommendedSafeValue:5}],
  assetDependencies:[{suggestedSemanticKey:'custom.reference',mediaKind:'image',inputSlot:'image',isRequired:true,minItems:1,maxItems:2}],
  primaryOutputNodeIds:['chosen-video'],outputCandidates:[{canvasNodeId:'first-image',nodeType:'SaveImage',suggestedOutputKind:'image'},{canvasNodeId:'chosen-video',nodeType:'SaveVideo',suggestedOutputKind:'video'}],
})
