export const integerConstraintsFixture = () => ({
  value_fields: [
    {value:'minimum',label:'最小值'}, {value:'maximum',label:'最大值'},
    {value:'default',label:'默认值（可选）'}, {value:'const',label:'固定值（可选）'},
  ],
  seed_policies: [{value:'fixed',label:'固定／人工提供'},{value:'generate_once',label:'首次准备时生成一次'}],
})
