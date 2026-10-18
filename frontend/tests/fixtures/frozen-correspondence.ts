/** Presentation data only: not a saved business task or model result. */
import type {TaskReferenceAssetSummary} from '../../src/types/index'
export const frozenCorrespondenceRows = (): TaskReferenceAssetSummary[] => [0,1,2,3,4].map(n=>({
  itemKey:`isolated-use-${n}`,inputKey:n<2?'pictures':n===2?'video':n===3?'audio':'context',assetId:n<2?'same-asset':`asset-${n}`,
  displayName:n<2?'素材文件 · 同一张图':n===2?'视频参考':n===3?'配对音轨':'上下文素材',mediaKind:n===2?'video':n===3?'audio':'image',
  inputLabel:n<2?`<Picture ${2-n}>`:n===2?'<Video 1>':n===3?'<Audio 1>':undefined,
  purpose:n===2?'motion':undefined,pairedTo:n===3?'isolated-use-2':undefined,previewUrl:'',checksum:'a'.repeat(64),sourceSha256:'b'.repeat(64),
  ports:n===4?[]:[{targetKey:n<2?'picture_ports':n===2?'video_ports':'audio_ports',port:{kind:'semantic'}}],stagingStatus:n===4?'not_applicable':'not_staged',
  referenceBinding:{position:n<2?1-n:0,original_name:n===0?'旅人':n===1?'旅人':n===2?'Ａlice':n===3?'Alice':'<script>仅原文</script>',
    reference_name:n===4?null:`${n<2?'旅人':n===2?'Ａlice':'Alice'} [isolated-use-${n}]`,name_disambiguated:n===4?null:true,input_slot:n===4?null:n<2?2-n:1,
    shot_reference:n<3?{use_id:`selection-${n}`,use_key:`雨中参考-${n}`,position:n,purpose:n===0?'同一人物的外观参考\n不自动变成首帧':'',source_mode:'existing',
      source:n<2?{kind:'reference_version',reference_version_id:`version-${n+1}`,series_id:'same-series',version:n+1,acquisition_source:'existing'}:{kind:'asset',asset_id:'asset-2'},
      setting:n<2?{appearance_id:'appearance',setting_revision_id:`revision-${n+1}`,revision:n+1,content_sha256:'c'.repeat(64),entity_kind:'character',entity_id:'character',appearance_role:'primary',reference_name:'旅人设定'}:null}:null}
}))
