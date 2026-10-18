// Isolated catalog only; no runtime business-field defaults.
export const scriptContractFixture={schema_version:3,description:'模型仅维护传输设置；业务字段归所选输出版本',policies:[
  ['series_concept','script.generate','剧本生成'],['script_structure','script.structure','剧本结构化']
].map(([key,operation,label])=>({key,operation,label,number_fields:[
  {key:'timeout_seconds',label:'模型请求等待时限',unit:'秒',integer:false,minimum:0,maximum:600,exclusive_minimum:true}
]}))}
