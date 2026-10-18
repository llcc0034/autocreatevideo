<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { BibleItem, BibleImageEntityType, SeriesProject } from '@/types'

const props = defineProps<{ project: SeriesProject }>()
const emit = defineEmits<{ manage: [item: BibleItem, kind: BibleImageEntityType] }>()
const search = ref(''), page = ref(1), pageSize = 12
const groups = [
  { key: 'characters', kind: 'character', label: '人物' },
  { key: 'scenes', kind: 'scene', label: '场景' },
  { key: 'props', kind: 'prop', label: '道具' },
] as const
const rows = computed(() => groups.flatMap(group => props.project.bible[group.key].map(item => ({ item, kind: group.kind, label: group.label }))))
const filtered = computed(() => rows.value.filter(row => `${row.item.name} ${row.label}`.toLocaleLowerCase().includes(search.value.trim().toLocaleLowerCase())))
const pages = computed(() => Math.max(1, Math.ceil(filtered.value.length / pageSize)))
const visible = computed(() => filtered.value.slice((page.value - 1) * pageSize, page.value * pageSize))
watch([search, () => props.project.id], () => { page.value = 1 })
watch(pages, value => { page.value = Math.min(page.value, value) })
</script>

<template>
  <section class="original-names" aria-label="原名称与设定">
    <header><div><h3>原名称与设定</h3><p>直接保留人物、场景和道具的原名称，无需补英文名。素材按实体、设定版本与实际输入位置关联，不靠译名识别。</p></div>
      <label class="field"><span>查找原名称</span><input v-model="search" type="search" placeholder="名称或类型"></label>
    </header>
    <p class="source-note">这里是项目当前输入（含未保存编辑），不是本轮已冻结内容。修改不会改写历史请求；参考名称与实际素材请在对应设定中查看。</p>
    <ul v-if="visible.length"><li v-for="row in visible" :key="`${row.kind}:${row.item.id}`"><div><strong>{{ row.item.name || '未填写名称' }}</strong><small>{{ row.label }}</small></div><button type="button" class="button ghost small" :aria-label="`查看${row.label} ${row.item.name}的主图与变体设定`" @click="emit('manage', row.item, row.kind)">主图／变体设定</button></li></ul>
    <p v-else>{{ rows.length ? '没有匹配的名称。' : '尚未登记实体，可在 Bible 与参考素材中添加；不是缺少翻译。' }}</p>
    <footer><span>共 {{ filtered.length }} 项 · 第 {{ page }} / {{ pages }} 页</span><div v-if="pages > 1"><button type="button" class="button ghost small" :disabled="page <= 1" @click="page--">上一页名称</button><button type="button" class="button ghost small" :disabled="page >= pages" @click="page++">下一页名称</button></div></footer>
  </section>
</template>

<style scoped>
.original-names{border:1px solid var(--border);border-radius:12px;padding:18px;margin:18px 0;min-width:0}header,footer,li{display:flex;align-items:center;justify-content:space-between;gap:16px}header>div{flex:1;min-width:0}h3{font-size:16px;margin:0}p,small,footer{font-size:14px;color:var(--text-muted);line-height:1.65}p{margin:8px 0}.source-note{padding:10px 0;border-bottom:1px solid var(--border)}header label{width:230px;max-width:100%}ul{list-style:none;padding:0;margin:0;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 24px}li{border-bottom:1px solid var(--border);padding:12px 0;min-width:0}li>div{min-width:0}strong,small{display:block;overflow-wrap:anywhere}strong{font-size:14px}li button{flex-shrink:0}footer{margin-top:12px;flex-wrap:wrap}footer>div{display:flex;gap:8px}
@media(max-width:720px){header{align-items:flex-start;flex-direction:column}ul{grid-template-columns:minmax(0,1fr)}.original-names{padding:12px}li{flex-wrap:wrap}}
</style>
