// A test catalog; production controls always use the server options response.
export const visualRuleReviewFixture = () => ({schema_version:1,
  output:{id:'output-version',output_key:'output-key',version:2,name:'Frozen output',sha256:'f'.repeat(64)},
  fields:visualRulesOutputRaw.outputs.map(row=>({key:row.field,label:row.label,value_type:'text',allow_blank:row.field==='negative_prompt',
    source_selector:row.source_selector,response_path:row.response_field,response_format:row.response_field,transformation:row.transformation}))})
export const outputResponseKeyPolicy={pattern:'^[a-z][a-z0-9_]{0,63}(\\.([a-z][a-z0-9_]{0,63}|0|[1-9][0-9]{0,63})){0,3}$',max_length:259,reserved:['schema_version','constructor','prototype'],description:'最多4层对象／数组，数组下标从0连续声明，不接受脚本或别名。'}
export const visualRulesOutputRaw={schema_version:1,operation:'visual_rules.generate',response_schema_version:2,
  response_example:{schema_version:2,color_design:{base_colors:['teal'],accent_color:'none',palette_relation:'cinematic',
    lighting:'soft_diffuse',contrast:'restrained',saturation:'muted',exposure:'balanced',progression:'stable',preserve_natural_skin_exposure:true},
    negative_constraints:[],continuity_summary:'Keep stable visual continuity.'},
  number_fields:[
    ['base_colors_min_items','基础色建议最少数量','项',0,21],['base_colors_max_items','基础色建议最多数量','项',0,21],
    ['negative_constraints_min_items','负面约束最少数量','条',0,1048576],['negative_constraints_max_items','负面约束最多数量','条',0,1048576],
    ['negative_constraint_max_chars','单条建议最长','字符',1,1048576],['continuity_summary_max_chars','连续性说明建议最长','字符',1,1048576],
    ['negative_prompt_max_chars','合并后负面提示词建议最长','字符',1,1048576],['color_script_max_chars','色彩脚本建议最长','字符',1,1048576],
  ].map(([key,label,unit,minimum,maximum])=>({key,label,unit,minimum,maximum})),
  number_pairs:[['base_colors_min_items','base_colors_max_items'],['negative_constraints_min_items','negative_constraints_max_items']],
  clauses_max_bytes:1048576,color_choices:{base_colors:['black','teal'],preserve_natural_skin_exposure:[true,false]},
  outputs:[{source_selector:'reviewed_visual_rules.continuity_summary',field:'continuity_summary',label:'已审核连续性说明',response_field:'continuity_summary',transformation:'校验完整正文后保留。'},
    {source_selector:'reviewed_visual_rules.color_script',field:'color_script',label:'已审核色彩脚本',response_field:'color_design',transformation:'按枚举渲染英文脚本。'},
    {source_selector:'reviewed_visual_rules.negative_prompt',field:'negative_prompt',label:'已审核负面约束',response_field:'negative_constraints',transformation:'合并建议，审核时可移除。'}]}
export const outputFieldSourceOptions=visualRulesOutputRaw.outputs.map(row=>({field:row.field,label:row.label,choices:[{
  response_field:row.response_field,value_type:row.field==='color_script'?'object':row.field==='negative_prompt'?'array':'text',
  transformation:row.transformation,example:(visualRulesOutputRaw.response_example as any)[row.response_field]},
  ...(row.field==='color_script'?[{response_field:'color_script',value_type:'text',transformation:'保留模型英文说明',example:'Original color description.'}]:
    row.field==='continuity_summary'?[{response_field:'continuity_points',value_type:'array',transformation:'按顺序连接要点',example:['Keep wardrobe consistent.','Preserve lighting direction.']}]:
    [{response_field:'negative_prompt',value_type:'text',transformation:'仅按英文逗号拆分再合并建议',example:'identity drift, inconsistent lighting'}])]}))
