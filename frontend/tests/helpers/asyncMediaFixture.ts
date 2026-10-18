export const asyncMediaFixture = {
  schema_version:1,maximum_string_length:100000,description:'仅支持标准标量；Smoke 样例不是生产默认值。',
  operations:[{key:'image.text_to_image',output_kind:'image'},{key:'video.text_to_video',output_kind:'video'},{key:'audio.text_to_audio',output_kind:'audio'}],
  fields:[
    ['model','模型',['string']],['prompt','正向提示词',['string']],['negative_prompt','负向提示词',['string']],
    ['width','宽度',['integer']],['height','高度',['integer']],['duration_seconds','时长',['integer','number']],
    ['fps','帧率',['integer','number']],['seed','种子',['integer']],['audio','音频开关',['boolean']],
  ].map(([key,label,types])=>({key,label,types,source_description:`来源说明 ${key}`})),
}
