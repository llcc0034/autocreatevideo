/** Isolated presentation fixture, never a business task or Smoke result. */
export const mediaTraceFixture = () => ({
  state:'recorded',workflow_version_id:'isolated-workflow',request_sha256:'a'.repeat(64),root_id:'isolated-root',
  message:'隔离组件样例：只解释冻结接线与暂存回执，不代表模型执行成功。',media_profile:null,
  scalar_wiring:{state:'matched',contract_sha256:'d'.repeat(64),message:'精确版本接线，不是执行回执',rows:[
    {semantic_key:'prompt.positive',value_type:'string',node_id:'encoder',input_name:'text',class_type:'TextEncoder',state:'matched',message:'原声明端口一致 <script>not executable</script>'}]},
  items:[{item_key:'second',input_key:'references',kind:'image',display_name:'<script>not executable</script>',asset_id:'asset-1',artifact_id:null,
    reference_binding:{position:0,original_name:'隔离原名',reference_name:null,name_disambiguated:null,input_slot:null,shot_reference:null},
    origin:{kind:'asset',asset_id:'asset-1'},purpose:null,paired_to:null,source_facts_sha256:'b'.repeat(64),source_bytes_sha256:'c'.repeat(64),input_label:'<Picture 1>',
    mode:'workflow_input',ports:[{target_key:'images',port:{kind:'node_input',node_id:'merge',input_name:'image1'}}],
    graph_inputs:[{node_id:'cr_images_0',class_type:'LoadImage',input_path:['image']}],
    port_routes:[{target_key:'images',destination:{node_id:'merge',input_name:'image1'},state:'connected',entry_node_id:'cr_images_0',edge_indexes:[0],message:'一条最短连通路径，不是运行顺序或送达证明。'}],
    processing_summary:{state:'declared',label:'使用原始字节',description:'未声明平台上传前转换。',configuration_source:'原执行组合的素材接线',shared_with:null,
      declared:[],observed:[],byte_identity:'unavailable',message:'只有处理声明，尚无本次暂存回执；不证明实际执行。'},
    processing:null,adapter:'source_bytes/v1',receipt_state:'missing',receipt:null,processing_metadata:null}],
  nodes:[{node_id:'cr_images_0',class_type:'LoadImage',origin:'added',removed_inputs:[],inputs:[{input_name:'image',change:'added',kind:'media',value:null}]},
    {node_id:'merge',class_type:'ImageBatch',origin:'original',removed_inputs:['image2'],inputs:[
      {input_name:'image1',change:'changed',kind:'connection',value:['cr_images_0',0]},
      {input_name:'width',change:'unchanged',kind:'literal',value:0},
      {input_name:'enabled',change:'changed',kind:'literal',value:false},
      {input_name:'filename',change:'unchanged',kind:'redacted',value:null}]}],
  edges:[{from_node:'cr_images_0',output_index:0,to_node:'merge',input_name:'image1'}],
})
