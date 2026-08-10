<template>
  <div class="page">
    <!-- 监控用户扫描（全量扫描进度） -->
    <el-card shadow="never">
      <div class="scan-head">
        <div class="scan-title">
          <el-icon color="#fb7299"><VideoPlay /></el-icon>
          <span>监控用户扫描</span>
        </div>
        <div class="scan-note" v-if="scanState.running">
          <el-tag size="small" type="warning" effect="plain" class="mr4">扫描中</el-tag>
          <span class="dim">{{ scanState.message }}</span>
        </div>
      </div>
      <!-- 步骤条 -->
      <el-steps :active="scanStep" finish-status="success" class="scan-steps">
        <el-step title="启动扫描">
          <template #description>
            {{ scanState.running ? `共 ${scanState.total} 个监控用户` : (scanState.finished_at ? `上次结束 ${scanState.finished_at}` : '等待任务启动') }}
          </template>
        </el-step>
        <el-step title="扫描监控用户">
          <template #description>
            {{ scanState.current_user ? `正在扫描 ${scanState.current_user}（${scanState.done}/${scanState.total}）` : '扫描用户转发/发布动态' }}
          </template>
        </el-step>
        <el-step title="关键词初筛">
          <template #description>
            <template v-if="scanState.llm_enabled && scanState.llm_total">已发现 {{ scanState.found }} 个候选</template>
            <template v-else>已发现 {{ scanState.found }} 个抽奖活动</template>
          </template>
        </el-step>
        <el-step title="LLM 深度解析">
          <template #description>
            <div v-if="!scanState.llm_enabled" class="dim">未启用（在设置中开启 LLM）</div>
            <div v-else-if="!scanState.llm_total" class="dim">等待解析任务</div>
            <div v-else class="llm-desc">
              <div class="llm-desc-tags">
                <el-tag size="small" type="primary" effect="plain">解析 {{ scanState.llm_done }}/{{ scanState.llm_total }}</el-tag>
                <el-tag v-if="scanState.llm_success" size="small" type="success" effect="plain">抽奖 {{ scanState.llm_success }}</el-tag>
                <el-tag v-if="scanState.llm_fail" size="small" type="info" effect="plain">非抽奖 {{ scanState.llm_fail }}</el-tag>
                <el-tag v-if="scanState.llm_image" size="small" type="warning" effect="plain">图片 {{ scanState.llm_image }}</el-tag>
              </div>
              <div v-if="scanState.llm_current" class="llm-desc-current dim">
                当前：
                <el-tooltip :content="scanState.llm_current" placement="top">
                  <span class="cur-title">{{ shortTitle(scanState.llm_current) }}</span>
                </el-tooltip>
              </div>
            </div>
          </template>
        </el-step>
        <el-step title="扫描完成">
          <template #description>
            <span v-if="scanState.message" class="finish-msg">{{ shortTitle(scanState.message, 40) }}</span>
            <span v-else class="dim">等待扫描</span>
          </template>
        </el-step>
      </el-steps>
      <!-- LLM 进度条 -->
      <div v-if="llmActive" class="llm-progress">
        <el-progress :percentage="llmPercent" :stroke-width="8" striped striped-flow />
        <div class="llm-detail dim">
          <span>解析 {{ scanState.llm_done }}/{{ scanState.llm_total }} 条</span>
          <span v-if="scanState.llm_success">· 抽奖 {{ scanState.llm_success }}</span>
          <span v-if="scanState.llm_fail">· 非抽奖 {{ scanState.llm_fail }}</span>
          <span v-if="scanState.llm_current">· 当前: {{ scanState.llm_current }}</span>
        </div>
      </div>
      <!-- 按钮行 -->
      <div class="scan-actions">
        <el-button type="primary" :icon="VideoPlay" :loading="scanState.running" :disabled="scanState.running" @click="startScan">
          {{ scanState.running ? '扫描中...' : '开始扫描' }}
        </el-button>
        <el-button v-if="scanState.running" :icon="VideoPause" @click="stopScan">停止</el-button>
      </div>
    </el-card>

    <!-- 添加活动来源 -->
    <el-card shadow="never" class="mt">
      <template #header>
        <div class="card-header">
          <span><el-icon><Search /></el-icon> 添加活动来源</span>
          <el-tag type="info" effect="plain">共 {{ total }} 个监控用户</el-tag>
        </div>
      </template>
      <el-form inline>
        <el-form-item label="监控类型">
          <el-radio-group v-model="addForm.monitor_type">
            <el-radio-button value="repost">
              <el-icon><RefreshLeft /></el-icon> 监控用户转发的抽奖活动
            </el-radio-button>
            <el-radio-button value="publish">
              <el-icon><Promotion /></el-icon> 监控用户发布的抽奖活动
            </el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="用户 MID">
          <el-input v-model="addForm.uid" placeholder="B站空间页数字ID，如 38808431" clearable style="width: 260px">
            <template #prepend>UID</template>
          </el-input>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :icon="Plus" :loading="adding" @click="addUser">添加</el-button>
          <el-button :icon="Upload" :loading="batchLoading" @click="batchVisible = true">批量导入</el-button>
        </el-form-item>
      </el-form>
      <el-alert type="info" :closable="false" show-icon
        title="监控用户转发的抽奖活动：扫描该用户转发的动态，识别含抽奖关键词的内容；监控用户发布的抽奖活动：扫描该用户自己发布的抽奖/福利内容。添加后可在下方扫描指定用户。" />
    </el-card>

    <!-- 监控用户列表 -->
    <el-card shadow="never" class="mt">
      <template #header>
        <div class="card-header">
          <div class="header-left">
            <span>监控用户列表（被添加的用户信息）</span>
            <template v-if="selectedUsers.length">
              <el-button size="small" type="primary" plain :icon="VideoPlay" :loading="batchScanning"
                @click="batchScan">批量扫描 ({{ selectedUsers.length }})</el-button>
              <el-popconfirm title="确定批量移除选中的监控用户？" @confirm="batchRemove">
                <template #reference>
                  <el-button size="small" type="danger" plain :icon="Delete">批量删除 ({{ selectedUsers.length }})</el-button>
                </template>
              </el-popconfirm>
            </template>
          </div>
          <div>
            <el-select v-model="filterType" placeholder="类型筛选" clearable size="small" style="width: 160px" @change="load">
              <el-option label="转发监控" value="repost" />
              <el-option label="发布监控" value="publish" />
            </el-select>
            <el-input v-model="filterKeyword" placeholder="搜索昵称/UID" clearable size="small" style="width: 180px; margin-left: 8px" @keyup.enter="load" @clear="load" />
            <el-button size="small" :icon="Search" @click="load">搜索</el-button>
          </div>
        </div>
      </template>
      <el-table :data="users" v-loading="loading" stripe @selection-change="onSelectionChange">
        <el-table-column type="selection" width="45" />
        <el-table-column label="用户" min-width="240">
          <template #default="{ row }">
            <div class="user-cell">
              <el-avatar :size="40" :src="row.avatar"><el-icon><User /></el-icon></el-avatar>
              <div>
                <div class="uname">{{ row.username }}</div>
                <div class="uid">UID: {{ row.uid }} · {{ row.monitor_type === 'repost' ? '转发监控' : '发布监控' }}</div>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="last_scanned_at" label="扫描状态" width="210">
          <template #default="{ row }">
            <template v-if="isScanningUser(row)">
              <el-tag size="small" type="warning" effect="dark" class="mr4">
                <el-icon class="is-loading" style="margin-right:4px"><Loading /></el-icon>扫描中
              </el-tag>
              <div class="dim scan-step">{{ scanState.message || '抓取动态中...' }}</div>
            </template>
            <template v-else>
              <span v-if="row.last_scanned_at">{{ row.last_scanned_at }}</span>
              <el-tag v-else size="small" type="info">未扫描</el-tag>
            </template>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="240" fixed="right">
          <template #default="{ row }">
            <div class="ops">
              <el-button size="small" type="primary" plain :icon="VideoPlay" :loading="scanningId === row.id" @click="scanUser(row)">扫描</el-button>
              <el-button size="small" :icon="Link" @click="openSpace(row)">空间</el-button>
              <el-popconfirm title="确定移除该监控用户？" @confirm="removeUser(row)">
                <template #reference>
                  <el-button size="small" type="danger" :icon="Delete">移除</el-button>
                </template>
              </el-popconfirm>
            </div>
          </template>
        </el-table-column>
        <template #empty><el-empty description="暂无监控用户，请在上方添加" /></template>
      </el-table>
    </el-card>

    <!-- 批量导入 -->
    <el-dialog v-model="batchVisible" title="批量导入监控用户" width="520">
      <el-form label-width="90px">
        <el-form-item label="监控类型">
          <el-radio-group v-model="batchForm.monitor_type">
            <el-radio value="repost">转发监控</el-radio>
            <el-radio value="publish">发布监控</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="用户 MID">
          <el-input v-model="batchForm.uidsText" type="textarea" :rows="6"
            placeholder="每行一个 MID，或使用逗号/空格分隔&#10;示例：&#10;38808431&#10;86609988, 21056345" />
        </el-form-item>
      </el-form>
      <el-alert v-if="batchResult" :title="batchResultText" type="success" :closable="false" show-icon class="mb8" />
      <template #footer>
        <el-button @click="batchVisible = false">取消</el-button>
        <el-button type="primary" :loading="batchLoading" @click="batchImport">开始导入</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Plus, Upload, Search, User, Delete, Link, VideoPlay, VideoPause, RefreshLeft, Promotion, Loading } from '@element-plus/icons-vue'
import { watchApi, scanApi } from '../api'

const users = ref([])
const loading = ref(false)
const total = computed(() => users.value.length)
const adding = ref(false)
const scanningId = ref(null)
const selectedUsers = ref([])
const batchScanning = ref(false)

// 单用户扫描进度（复用扫描管理器状态）
const scanState = ref({
  running: false, total: 0, done: 0, current_user: '', found: 0, message: '',
  finished_at: '', llm_enabled: false, llm_done: 0, llm_total: 0,
  llm_success: 0, llm_fail: 0, llm_image: 0, llm_current: '',
})
let scanTimer = null
const refreshTick = ref(0)

// 步骤条：0 未启动 / 1 扫描用户 / 2 初筛 / 3 LLM / 4 完成
const scanStep = computed(() => {
  const s = scanState.value
  if (!s.running && (s.message === '扫描完成' || s.message === '扫描已停止')) return 4
  if (!s.running) return 0
  if (!s.total) return 0
  if (s.llm_enabled && s.llm_total > 0) return 3
  if (s.found > 0 || s.done > 0) return 2
  return 1
})
const llmActive = computed(() => {
  const s = scanState.value
  return s.llm_enabled && s.llm_total > 0 && s.running
})
const llmPercent = computed(() => {
  const s = scanState.value
  if (!s.llm_total) return 0
  return Math.round((s.llm_done / s.llm_total) * 100)
})
// 压缩空白并截断长文本（步骤条/当前项展示用）
function shortTitle(text, maxLen = 16) {
  if (!text) return ''
  const clean = String(text).replace(/\s+/g, ' ').trim()
  return clean.length > maxLen ? clean.slice(0, maxLen) + '…' : clean
}

function isScanningUser(row) {
  return scanState.value.running && scanState.value.current_user.includes(row.username)
}

async function startScan() {
  const res = await scanApi.start(null)
  ElMessage.success(res.message)
  pollScanProgress()
}
async function stopScan() {
  await scanApi.stop()
  ElMessage.info('已请求停止')
}

async function pollScanProgress() {
  stopScanPolling()
  scanTimer = setInterval(async () => {
    try {
      scanState.value = await scanApi.progress()
      if (scanState.value.running) {
        refreshTick.value++
        if (refreshTick.value % 4 === 0) load()
      } else {
        stopScanPolling()
        scanningId.value = null
        load()
      }
    } catch (e) { /* ignore */ }
  }, 1200)
}
function stopScanPolling() { if (scanTimer) { clearInterval(scanTimer); scanTimer = null } }

const addForm = ref({ monitor_type: 'repost', uid: '' })
const filterType = ref('')
const filterKeyword = ref('')

const batchVisible = ref(false)
const batchLoading = ref(false)
const batchForm = ref({ monitor_type: 'repost', uidsText: '' })
const batchResult = ref(null)
const batchResultText = computed(() => batchResult.value
  ? `导入完成：新增 ${batchResult.value.added.length} 个，跳过 ${batchResult.value.skipped.length} 个，失败 ${batchResult.value.failed.length} 个`
  : '')

async function load() {
  loading.value = true
  try {
    const res = await watchApi.list({
      monitor_type: filterType.value || '',
      keyword: filterKeyword.value || '',
    })
    users.value = res.items || []
  } finally { loading.value = false }
}

async function addUser() {
  if (!addForm.value.uid.trim()) return ElMessage.warning('请输入用户 MID')
  adding.value = true
  try {
    const u = await watchApi.add({
      uid: addForm.value.uid.trim(),
      monitor_type: addForm.value.monitor_type,
    })
    ElMessage.success(`已添加 ${u.username}`)
    addForm.value.uid = ''
    load()
  } finally { adding.value = false }
}

function openSpace(row) {
  window.open(`https://space.bilibili.com/${row.uid}`, '_blank')
}

async function scanUser(row) {
  scanningId.value = row.id
  try {
    const res = await watchApi.scan(row.id)
    if (res.started) {
      ElMessage.info(`开始扫描 ${row.username}，进度将在列表中展示`)
      pollScanProgress()
    } else {
      // 已在扫描中
      scanningId.value = null
      ElMessage.warning(res.message || '扫描已在进行中')
      pollScanProgress()
    }
  } catch (e) {
    scanningId.value = null
  }
}

async function removeUser(row) {
  await watchApi.remove(row.id)
  ElMessage.success('已移除')
  load()
}

function onSelectionChange(rows) {
  selectedUsers.value = rows
}

async function batchScan() {
  const ids = selectedUsers.value.map(u => u.id)
  if (!ids.length) return ElMessage.warning('请先勾选要扫描的用户')
  batchScanning.value = true
  try {
    const res = await scanApi.start(ids)
    ElMessage.success(res.message || '批量扫描已启动，可在下方查看扫描进度')
  } finally { batchScanning.value = false }
}

async function batchRemove() {
  const ids = selectedUsers.value.map(u => u.id)
  if (!ids.length) return
  const res = await watchApi.batchDelete(ids)
  ElMessage.success(`已批量移除 ${res.count} 个监控用户`)
  load()
}

async function batchImport() {
  const uids = batchForm.value.uidsText
    .split(/[\s,，;；]+/).map(s => s.trim()).filter(Boolean)
  if (!uids.length) return ElMessage.warning('请输入至少一个 MID')
  batchLoading.value = true
  try {
    batchResult.value = await watchApi.batchAdd({
      uids,
      monitor_type: batchForm.value.monitor_type,
    })
    load()
  } finally { batchLoading.value = false }
}

onMounted(() => {
  load()
  // 若已有扫描在进行，恢复进度展示
  scanApi.progress().then(s => {
    scanState.value = s
    if (s.running) { scanningId.value = null; pollScanProgress() }
  }).catch(() => {})
})
onUnmounted(stopScanPolling)
</script>

<style scoped>
.mt { margin-top: 16px; }
.scan-step { font-size: 12px; line-height: 1.4; margin-top: 2px; max-width: 190px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.card-header { display: flex; justify-content: space-between; align-items: center; }
.header-left { display: flex; align-items: center; gap: 10px; }
.user-cell { display: flex; align-items: center; gap: 10px; }
.uname { font-weight: 600; }
.uid { font-size: 12px; color: var(--el-text-color-secondary); }
.mb8 { margin-bottom: 8px; }
.ops { display: flex; align-items: center; gap: 4px; white-space: nowrap; }
/* 监控用户扫描 */
.scan-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 18px; }
.scan-title { display: flex; align-items: center; gap: 6px; font-weight: 600; font-size: 15px; }
.scan-note { font-size: 12px; line-height: 1.6; }
.scan-steps { width: 100%; }
.llm-desc { display: flex; flex-direction: column; gap: 4px; }
.llm-desc-tags { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; }
.llm-desc-current { font-size: 12px; line-height: 1.5; }
.cur-title { color: var(--el-text-color-secondary); cursor: default; }
.finish-msg { font-size: 12px; }
.llm-progress { margin-top: 14px; }
.llm-detail { margin-top: 6px; font-size: 12px; }
.scan-actions { display: flex; gap: 8px; margin-top: 18px; }
</style>
