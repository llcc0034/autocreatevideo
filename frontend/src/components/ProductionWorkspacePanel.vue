<template>
  <section class="panel production-workspace-panel"><slot/></section>
</template>
<style scoped>
.production-workspace-panel{padding:20px;min-width:0;display:grid;gap:18px;font-size:14px;line-height:1.7}
@media(max-width:640px){.production-workspace-panel{padding:14px}}
</style>
