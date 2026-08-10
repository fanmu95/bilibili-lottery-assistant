<template>
  <div class="page">
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span><el-icon><Document /></el-icon> 运行日志</span>
          <div class="filters">
            <el-select v-model="filter.level" placeholder="级别" clearable size="small" style="width: 110px" @change="search">
              <el-option label="全部" value="" />
              <el-option label="成功" value="success" />
              <el-option label="信息" value="info" />
              <el-option label="警告" value="warning" />
              <el-option label="错误" value="error" />
            </el-select>
            <el-select v-model="filter.module" placeholder="模块" clearable size="small" style="width: 130px" @change="search">
              <el-option v-for="m in modules" :key="m" :label="m" :value="m" />
            </el-select>
            <el-input v-model="filter.keyword" placeholder="搜索日志内容" clearable size="small" style="width: 190px" @keyup.enter="search" @clear="search" />
            <el-button size="small" :icon="Search" @click="search">搜索</el-button>
            <el-divider direction="vertical" />
            <el-switch v-model="autoRefresh" active-text="自动刷新" inline-prompt size="small" />
            <el-button size="small" :icon="Refresh" @click="load">刷新</el-button>
            <el-button size="small" type="danger" plain :icon="Delete" @click="clearLogs">清空日志</el-button>
          </div>
        </div>
      </template>

      <el-alert v-if="error" :title="error" type="error" :closable="false" class="mb8" />

      <el-timeline v-loading="loading" class="log-timeline">
        <el-timeline-item v-for="log in logs" :key="log.id" :timestamp="log.created_at"
          :type="typeMap[log.level]" placement="top" size="large">
          <div class="log-item">
            <el-tag :type="typeMap[log.level]" size="small" effect="dark">{{ levelLabel[log.level] }}</el-tag>
            <el-tag size="small" type="info" effect="plain">{{ log.module }}</el-tag>
            <span class="log-msg">{{ log.message }}</span>
          </div>
        </el-timeline-item>
        <el-empty v-if="!loading && logs.length === 0" description="暂无日志" />
      </el-timeline>

      <el-pagination v-model:current-page="page" :page-size="pageSize" :total="total"
        layout="total, prev, pager, next" class="mt" @current-change="load" />
    </el-card>
  </div>
</template>

<script setup>
import { onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Document, Search, Refresh, Delete } from '@element-plus/icons-vue'
import { logApi } from '../api'

const logs = ref([])
const modules = ref([])
const loading = ref(false)
const total = ref(0)
const page = ref(1)
const pageSize = ref(30)
const error = ref('')
const autoRefresh = ref(true)

const filter = reactive({ level: '', module: '', keyword: '' })

const typeMap = { success: 'success', info: 'primary', warning: 'warning', error: 'danger' }
const levelLabel = { success: '成功', info: '信息', warning: '警告', error: '错误' }

async function load() {
  loading.value = true
  error.value = ''
  try {
    const res = await logApi.list({
      page: page.value,
      page_size: pageSize.value,
      level: filter.level || '',
      module: filter.module || '',
      keyword: filter.keyword || '',
    })
    logs.value = res.items || []
    total.value = res.total || 0
  } catch (e) {
    error.value = '日志加载失败，请检查后端服务'
  } finally {
    loading.value = false
  }
}

async function loadModules() {
  try { modules.value = await logApi.modules() || [] } catch (e) { /* ignore */ }
}

function search() { page.value = 1; load() }

async function clearLogs() {
  await ElMessageBox.confirm('确定清空全部日志？此操作不可恢复', '提示', { type: 'warning' })
  await logApi.clear()
  ElMessage.success('日志已清空')
  load()
}

let timer = null
onMounted(() => {
  load(); loadModules()
  timer = setInterval(() => { if (autoRefresh.value) load() }, 5000)
})
onUnmounted(() => clearInterval(timer))
</script>

<style scoped>
.card-header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; }
.filters { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.log-timeline { padding: 8px 4px; max-height: calc(100vh - 300px); overflow-y: auto; }
.log-item { display: flex; align-items: center; gap: 8px; }
.log-msg { font-size: 13px; }
.mb8 { margin-bottom: 8px; }
.mt { margin-top: 16px; justify-content: flex-end; }
</style>
