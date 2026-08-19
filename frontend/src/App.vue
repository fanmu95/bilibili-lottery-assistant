<template>
  <router-view />
</template>

<script setup>
// 全局后台任务提醒：职业抽奖号发现完成时，无论当前在哪个页面都弹出通知
import { onMounted, onUnmounted } from 'vue'
import { ElMessage, ElNotification } from 'element-plus'
import { activityApi, updateApi } from './api'

let proTimer = null
let lastNotifiedMsg = ''   // 防止同一结果重复通知
let lastUpdateShown = ''   // 防止同一版本重复提醒

// 每次打开页面：弱提醒检查新版本（不弹窗打断，仅 info 提示）
async function checkUpdate() {
  try {
    const u = await updateApi.check()
    if (u?.has_update && u.latest !== lastUpdateShown) {
      lastUpdateShown = u.latest
      ElMessage({
        type: 'info',
        message: `发现新版本 ${u.latest}（当前 ${u.current}），可在「设置」页更新`,
        duration: 6000,
        showClose: true,
      })
    }
  } catch (e) { /* 静默 */ }
}

async function checkProDiscovery() {
  try {
    const p = await activityApi.discoverProProgress()
    if (p.running) {
      // 任务进行中：保持轮询
      if (!proTimer) proTimer = setInterval(checkProDiscovery, 10000)
      return
    }
    // 任务结束（空闲）
    if (proTimer) { clearInterval(proTimer); proTimer = null }
    const result = p.result
    if (result && result.message && result.message !== lastNotifiedMsg) {
      lastNotifiedMsg = result.message
      const found = result.found || []
      const added = result.added || []
      if (added.length) {
        ElNotification({
          title: '🎯 发现职业抽奖号',
          message: `新增 ${added.length} 个监控：${added.slice(0, 3).map(a => a.uname).join('、')}${added.length > 3 ? ' 等' : ''}`,
          type: 'success',
          duration: 8000,
          position: 'bottom-right',
        })
      } else if (found.length) {
        ElNotification({
          title: '职业号发现完成',
          message: result.message,
          type: 'info',
          duration: 6000,
          position: 'bottom-right',
        })
      }
    }
  } catch (e) { /* 后端未就绪时静默 */ }
}

onMounted(() => {
  checkProDiscovery()
  checkUpdate()
})
onUnmounted(() => { if (proTimer) { clearInterval(proTimer); proTimer = null } })
</script>

<style>
* { box-sizing: border-box; }
html, body, #app { height: 100%; margin: 0; }
body {
  font-family: 'Helvetica Neue', Helvetica, 'PingFang SC', 'Microsoft YaHei', Arial, sans-serif;
  background: var(--el-bg-color-page);
  color: var(--el-text-color-primary);
}
/* B 站官方表情（评论/私信 v-html 渲染的图片） */
.bili-emote {
  width: 20px;
  height: 20px;
  vertical-align: middle;
  margin: 0 1px;
  object-fit: contain;
}
</style>
