<template>
  <el-container class="layout">
    <el-aside width="220px" class="aside">
      <div class="logo">
        <el-icon :size="22" color="#fb7299"><VideoPlay /></el-icon>
        <span>B站抽奖助手</span>
      </div>
      <el-menu :default-active="activeMenu" router class="menu" background-color="transparent">
        <el-menu-item v-for="item in menus" :key="item.path" :index="item.path">
          <el-icon><component :is="item.icon" /></el-icon>
          <span>{{ item.title }}</span>
        </el-menu-item>
      </el-menu>
      <div class="aside-footer">v1.0.0 · 本地控制台</div>
    </el-aside>

    <el-container>
      <el-header class="header">
        <div class="header-title">
          <el-icon><component :is="currentMeta.icon" /></el-icon>
          <span>{{ currentMeta.title }}</span>
        </div>
        <div class="header-right">
          <el-tag :type="backendOk ? 'success' : 'danger'" size="small" effect="dark">
            {{ backendOk ? '后端在线' : '后端离线' }}
          </el-tag>
          <el-tooltip :content="isDark ? '切换到亮色' : '切换到暗色'" placement="bottom">
            <el-switch v-model="isDark" @change="toggleTheme" inline-prompt
              :active-icon="Moon" :inactive-icon="Sunny" />
          </el-tooltip>
        </div>
      </el-header>
      <el-main class="main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { Moon, Sunny } from '@element-plus/icons-vue'
import { summaryApi } from '../api'

const route = useRoute()
const isDark = ref(document.documentElement.classList.contains('dark'))
const backendOk = ref(false)

const menus = [
  { path: '/accounts', title: '账号管理', icon: 'User' },
  { path: '/discovery', title: '活动发现', icon: 'Search' },
  { path: '/activities', title: '活动列表', icon: 'Trophy' },
  { path: '/logs', title: '日志', icon: 'Document' },
  { path: '/settings', title: '设置', icon: 'Setting' },
]

const activeMenu = computed(() => route.path)
const currentMeta = computed(() => route.meta)

function toggleTheme(v) {
  document.documentElement.classList.toggle('dark', v)
  localStorage.setItem('bili-theme', v ? 'dark' : 'light')
}

let timer = null
async function checkHealth() {
  try {
    await summaryApi.get()
    backendOk.value = true
  } catch (e) {
    backendOk.value = false
  }
}

onMounted(() => {
  checkHealth()
  timer = setInterval(checkHealth, 15000)
})
onUnmounted(() => clearInterval(timer))
</script>

<style scoped>
.layout { height: 100vh; }
.aside {
  background: var(--el-bg-color);
  border-right: 1px solid var(--el-border-color-light);
  display: flex;
  flex-direction: column;
}
.logo {
  height: 60px; display: flex; align-items: center; gap: 8px;
  padding: 0 20px; font-weight: 700; font-size: 16px;
  color: var(--el-text-color-primary);
}
.menu { flex: 1; border-right: none; }
.aside-footer {
  padding: 14px 20px; font-size: 12px; color: var(--el-text-color-secondary);
  border-top: 1px solid var(--el-border-color-lighter);
}
.header {
  display: flex; align-items: center; justify-content: space-between;
  background: var(--el-bg-color);
  border-bottom: 1px solid var(--el-border-color-light);
}
.header-title { display: flex; align-items: center; gap: 8px; font-weight: 600; }
.header-right { display: flex; align-items: center; gap: 14px; }
.main { background: var(--el-bg-color-page); }
</style>
