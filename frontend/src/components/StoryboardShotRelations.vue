<script setup lang="ts">
import {computed} from 'vue'
import type {ReviewShotDisplay} from '@/services/storyboardReviewDisplay'
const props = defineProps<{shot: ReviewShotDisplay}>()
const groups = computed(() => [
  {label: '场景', empty: '未关联场景', items: props.shot.scene ? [{key: props.shot.scene.id, entity: props.shot.scene, prefix: ''}] : []},
  ...(['visible', 'speaker'] as const).map(role => ({label: role === 'visible' ? '出镜角色' : '发声角色',
    empty: role === 'visible' ? '未声明出镜角色' : '未声明发声角色',
    items: props.shot.characters.filter(row => row.role === role).map(row => ({key: `${role}:${row.position}`,
      entity: row.entity, prefix: role === 'speaker' ? `对白第${row.position + 1}条：` : ''}))})),
  {label: '道具', empty: '未关联道具', items: props.shot.props.map(entity => ({key: entity.id, entity, prefix: ''}))},
])
</script>
<template>
  <section class="shot-relations" aria-label="原分镜关系">
    <p>关系来自所见原分镜；名称为同项目 Bible 当前名称，仅辅助阅读，不改变原关系或审核摘要。发声不代表出镜。</p>
    <dl><div v-for="group in groups" :key="group.label"><dt>{{group.label}}</dt><dd>
      <span v-if="!group.items.length">{{group.empty}}</span>
      <ul v-else><li v-for="item in group.items" :key="item.key">
        {{item.prefix}}<span v-if="item.entity.status === 'available'">{{item.entity.name}}</span>
        <span v-else class="platform-alert warning" role="status">原关联名称不可用，未按名字找替代对象</span>
      </li></ul>
    </dd></div></dl>
    <details><summary>原关系技术身份</summary><pre>{{JSON.stringify({scene:shot.scene,characters:shot.characters,props:shot.props},null,2)}}</pre></details>
  </section>
</template>
<style scoped>
.shot-relations{display:grid;gap:8px;min-width:0}.shot-relations p,.shot-relations dl,.shot-relations dd,.shot-relations ul{margin:0}
.shot-relations dl{display:grid;gap:8px}.shot-relations dl>div{display:flex;gap:12px;flex-wrap:wrap}.shot-relations dt{font-weight:600;min-width:5em}
.shot-relations dd{min-width:0;overflow-wrap:anywhere}.shot-relations ul{padding-left:18px}.shot-relations p,.shot-relations pre{white-space:pre-wrap;overflow-wrap:anywhere}
.shot-relations summary{cursor:pointer;min-height:36px}
</style>
