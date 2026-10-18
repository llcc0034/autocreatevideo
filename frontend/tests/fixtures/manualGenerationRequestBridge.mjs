// Test-only stdin/stdout bridge. No server, network calls or production writes.
import {createServer} from 'vite'
import {fileURLToPath} from 'node:url'
let content=''
for await (const chunk of process.stdin) content+=chunk
const value=JSON.parse(content)
const vite=await createServer({root:fileURLToPath(new URL('../..',import.meta.url)),logLevel:'silent',appType:'custom',server:{middlewareMode:true,hmr:false}})
try {
  const form=await vite.ssrLoadModule('/src/services/executionRequestForm.ts')
  const manual=await vite.ssrLoadModule('/src/services/productionManual.ts')
  const reviews=await vite.ssrLoadModule('/src/services/productionManualStoryboardReview.ts')
  const {adaptProductionRun}=await vite.ssrLoadModule('/src/services/platformAdapters.ts')
  const {adaptExecutionContract}=await vite.ssrLoadModule('/src/services/executionRegistry.ts')
  const run=adaptProductionRun(value.run),contract=form.executionRequestFormContract(adaptExecutionContract(value.contract))
  const sources=reviews.manualGenerationShotSources(reviews.readManualStoryboardReferences(value.review,run),value.target.scope.id)
  const results=[]
  for (const variant of value.variants) {
    const draft=form.emptyExecutionRequestDraft(contract)
    draft.parameters=value.parameters;draft.channels=value.channels
    for(const [key,rows] of Object.entries(variant.inputs)) draft.media[key]=rows.map(row=>{
      const source=sources.find(source=>source.referenceKey===row.reference)
      return {itemKey:row.key,assetId:source?.assetId??'',purpose:row.purpose??null,pairedTo:row.pairedTo??null,subjectId:null,
        ...(source?{reviewedShot:source}:{}),...(row.videoAudio?{videoAudio:true}:{})}
    })
    results.push(manual.buildManualGeneration({projectId:run.project,options:manual.adaptManualActionOptions(value.options),stageKey:'media_generation',
      target:{...value.target,unitKey:value.target.unit_key},contract,draft:await form.prepareExecutionSelectedSources(draft),idempotencyKey:variant.key}))
  }
  process.stdout.write(JSON.stringify(results))
} finally {await vite.close()}
