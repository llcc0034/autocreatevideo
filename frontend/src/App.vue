<script setup lang="ts">
import { clearRouteModuleFailure, routeModuleFailure } from '@/services/routeModuleRecovery'

const reloadPage = () => {
  clearRouteModuleFailure()
  window.location.reload()
}
</script>

<template>
  <RouterView />
  <aside v-if="routeModuleFailure" class="route-module-failure" role="alert" aria-live="assertive">
    <div>
      <strong>页面模块加载失败</strong>
      <p>前端服务可能刚完成重启，或浏览器仍保留旧模块。请重新加载页面后再试。</p>
    </div>
    <button type="button" @click="reloadPage">重新加载页面</button>
  </aside>
</template>

<style scoped>
.route-module-failure {
  position: fixed;
  z-index: 10000;
  right: 24px;
  bottom: 24px;
  display: flex;
  max-width: min(560px, calc(100vw - 32px));
  align-items: center;
  gap: 20px;
  padding: 16px 18px;
  border: 1px solid rgba(239, 90, 90, 0.55);
  border-radius: 12px;
  background: #181316;
  color: #f6e8e8;
  box-shadow: 0 18px 48px rgba(0, 0, 0, 0.38);
}

.route-module-failure strong {
  display: block;
  margin-bottom: 4px;
}

.route-module-failure p {
  margin: 0;
  color: #cbbfc2;
  line-height: 1.5;
}

.route-module-failure button {
  flex: none;
  padding: 10px 14px;
  border: 1px solid rgba(229, 155, 61, 0.7);
  border-radius: 8px;
  background: #d98f32;
  color: #160f08;
  cursor: pointer;
  font: inherit;
  font-weight: 700;
}

@media (max-width: 640px) {
  .route-module-failure {
    right: 16px;
    bottom: 16px;
    left: 16px;
    flex-direction: column;
    align-items: stretch;
  }
}
</style>
