<template>
  <div class="page">
    <!-- 统计 -->
    <div class="stat-row">
      <el-card shadow="never" class="stat-card"><el-statistic title="活动总数" :value="stats.total" /></el-card>
      <el-card shadow="never" class="stat-card"><el-statistic title="待参与" :value="stats.pending"><template #suffix><el-tag size="small" type="warning" effect="plain">可参与</el-tag></template></el-statistic></el-card>
      <el-card shadow="never" class="stat-card"><el-statistic title="已参与" :value="stats.participated" /></el-card>
      <el-card shadow="never" class="stat-card"><el-statistic title="已结束" :value="stats.ended"><template #suffix><el-tag size="small" type="info" effect="plain">已截止</el-tag></template></el-statistic></el-card>
      <el-card shadow="never" class="stat-card"><el-statistic title="待复核" :value="stats.unreviewed" /></el-card>
    </div>

    <!-- 全自动模式（扫描+参与循环） -->
    <el-card shadow="never" class="mt">
      <el-divider content-position="left"><el-icon><Cpu /></el-icon> 全自动模式</el-divider>
      <div class="auto-row">
        <div class="auto-info">
          <el-tag :type="autoState.running ? 'success' : 'info'" size="small" effect="dark" class="mr4">
            {{ autoState.running ? '运行中' : '未启动' }}
          </el-tag>
          <span class="dim auto-msg">{{ autoState.message || '待参与活动不足 10 个时自动扫描下一位监控用户，入库后自动参与' }}</span>
          <el-tag v-if="autoState.scheduled && !autoState.running" size="small" type="warning" effect="plain" class="mr4">
            定时 {{ autoState.schedule_time || '10:00' }} 自动启动
          </el-tag>
          <span v-if="autoState.running" class="dim auto-msg">
            （第 {{ autoState.round }} 轮 · 已参与 {{ autoState.participated }} 个 · 待参与 {{ autoState.pending_count }} 个<template v-if="autoState.scanned_user"> · 最近扫描 {{ autoState.scanned_user }}</template><template v-if="nextRoundText"> · 下一轮 {{ autoState.next_round_at }} 开始（剩余 {{ nextRoundText }}）</template>）
          </span>
        </div>
        <div class="auto-actions">
          <el-button type="success" :icon="Cpu" :loading="autoState.running" :disabled="autoState.running" @click="startAuto">
            启动全自动
          </el-button>
          <el-button v-if="autoState.running" type="danger" :icon="VideoPause" @click="stopAuto">停止</el-button>
          <el-button :icon="Refresh" @click="refreshAll">刷新状态</el-button>
        </div>
      </div>
      <!-- 全自动详细动作展示（当前动作 + 动作日志） -->
      <div v-if="autoState.running" class="auto-detail">
        <div class="auto-current" v-if="autoState.current_action">
          <span class="auto-current-icon">{{ logIcon(currentActionType) }}</span>
          <span class="auto-current-text" :class="'cur-' + currentActionType">{{ autoState.current_action }}</span>
        </div>
        <div v-if="autoState.current_activity" class="auto-activity dim">
          当前活动：{{ autoState.current_activity }}
          <template v-if="autoState.current_account"> · 账号：{{ autoState.current_account }}</template>
        </div>
        <!-- 职业号发现状态（自动模式冷却期运行、轮次开始暂停） -->
        <div v-if="proState.running" class="auto-pro">
          <el-tag size="small" type="warning" effect="dark">
            <el-icon class="is-loading" style="margin-right:4px"><Loading /></el-icon>职业号发现中
          </el-tag>
          <span class="dim">活动 {{ proState.activity_id }} · {{ proState.message || '分析评论区用户中...' }}</span>
        </div>
        <div v-else-if="proState.paused_by_auto && autoState.running" class="auto-pro">
          <el-tag size="small" type="info" effect="plain">职业号发现：已暂停</el-tag>
          <span class="dim">轮次进行中，冷却期恢复</span>
        </div>
        <div v-else-if="proState.result" class="auto-pro">
          <el-tag size="small" type="success" effect="plain">职业号发现完成</el-tag>
          <span class="dim">{{ proState.message || '' }}</span>
        </div>
        <el-scrollbar ref="autoLogScrollRef" v-if="autoState.action_log && autoState.action_log.length" max-height="170px" class="auto-log-scroll">
          <div class="auto-log">
            <div v-for="(item, i) in autoState.action_log" :key="i" class="auto-log-item" :class="'log-' + (item.type || 'info')">
              <span class="auto-log-icon">{{ logIcon(item.type) }}</span>
              <span class="auto-log-ts dim">{{ item.ts }}</span>
              <span class="auto-log-text">{{ item.text }}</span>
            </div>
          </div>
        </el-scrollbar>
      </div>
    </el-card>

    <!-- 快速筛选 -->
    <el-card shadow="never" class="mt">
      <div class="quick-filter">
        <el-segmented v-model="filter.status" :options="statusOptions" @change="search" />
        <el-input v-model="filter.keyword" placeholder="搜索标题/奖品/UP主" clearable size="default"
          style="width: 200px" @keyup.enter="search" @clear="search">
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>
        <el-button :icon="RefreshLeft" @click="reset">重置</el-button>
      </div>
    </el-card>

    <!-- 活动列表 -->
    <el-card shadow="never" class="mt">
      <template #header>
        <div class="card-header">
          <div class="header-left">
            <span>活动列表</span>
            <template v-if="selectedRows.length">
              <el-button size="small" type="primary" plain :icon="Select" :loading="batchParticipating"
                @click="batchParticipate">批量参与 ({{ selectedRows.length }})</el-button>
              <el-popconfirm title="确定批量删除选中的活动？" @confirm="batchRemove">
                <template #reference>
                  <el-button size="small" type="danger" plain :icon="Delete">批量删除 ({{ selectedRows.length }})</el-button>
                </template>
              </el-popconfirm>
            </template>
          </div>
        </div>
      </template>
      <el-table :data="activities" v-loading="loading" stripe @selection-change="onSelectionChange">
        <el-table-column type="selection" width="45" />
        <el-table-column label="活动" min-width="300">
          <template #default="{ row }">
            <div class="act-cell">
              <div class="act-title">
                <a :href="row.link" target="_blank" class="act-link">{{ row.title }}</a>
              </div>
              <div class="act-sub">
                <el-tag size="small" type="info" effect="plain">{{ sourceLabel(row.source_type) }}</el-tag>
                <span>@{{ row.author_name || row.source_name }}</span>
                <span v-if="row.source_name && row.source_name !== row.author_name">来源: {{ row.source_name }}</span>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="奖品" min-width="180">
          <template #default="{ row }">
            <el-tooltip v-if="row.prize_info" :content="row.prize_info" placement="top" :show-after="300">
              <span class="prize-multi" :class="reviewClass(row)">{{ row.prize_info }}</span>
            </el-tooltip>
            <span v-else class="dim">—</span>
          </template>
        </el-table-column>
        <el-table-column label="结束时间" width="145">
          <template #default="{ row }">
            <span v-if="row.end_time" :class="[reviewClass(row), { expired: isExpired(row) }]">{{ fmtEndTime(row.end_time) }}</span>
            <el-tag v-else size="small" type="info">未定</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="参与文案" min-width="120">
          <template #default="{ row }">
            <el-tooltip v-if="row.comment_text" :content="row.comment_text" placement="top" :show-after="300">
              <span class="comment-cell">{{ row.comment_text }}</span>
            </el-tooltip>
            <el-tooltip v-else content="未生成参与文案（设置中开启 LLM 生成/借用评论后，参与或扫描时自动生成）" placement="top">
              <span class="dim">—</span>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="未参与账号" width="170">
          <template #default="{ row }">
            <!-- 只显示未参与过的账号（点击即参与） -->
            <template v-if="accounts.length">
              <div class="acc-avatars" v-if="unparticipatedAccounts(row).length">
                <el-tooltip v-for="acc in unparticipatedAccounts(row)" :key="acc.id"
                  :content="`${acc.username}：点击参与`" placement="top">
                  <el-avatar :size="30" :src="acc.avatar" class="acc-avatar"
                    @click="participateWith(row, acc)">
                    <el-icon><User /></el-icon>
                  </el-avatar>
                </el-tooltip>
              </div>
              <el-tag v-else size="small" type="success" effect="plain">全部已参与</el-tag>
            </template>
            <el-tooltip v-else content="请先在账号管理添加并登录账号" placement="top">
              <span class="dim">无账号</span>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="statusMap[row.status]?.type || 'info'" size="small">{{ statusMap[row.status]?.label || row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="360" fixed="right">
          <template #default="{ row }">
            <div class="ops">
              <el-button size="small" type="primary" plain :icon="Link" @click="openLink(row)">跳转</el-button>
              <el-button v-if="row.status !== 'ended' && !isExpired(row)" size="small" :icon="Select" @click="participateDefault(row)">参与</el-button>
              <el-button size="small" :icon="Search" :loading="proDiscoveringId === row.id"
                @click="discoverPro(row)">发现职业号</el-button>
              <el-popconfirm title="确定删除该活动？" @confirm="removeActivity(row)">
                <template #reference>
                  <el-button size="small" type="danger" :icon="Delete">删除</el-button>
                </template>
              </el-popconfirm>
            </div>
          </template>
        </el-table-column>
        <template #empty><el-empty description="暂无该状态下的活动，可点击「开始扫描」发现" /></template>
      </el-table>
      <el-pagination v-model:current-page="page" v-model:page-size="pageSize" :total="total"
        layout="total, prev, pager, next, sizes" :page-sizes="[10, 25, 50, 100]" background
        class="mt" @current-change="load" @size-change="onSizeChange" />
    </el-card>

    <!-- 参与进度悬浮窗（不阻塞操作，支持队列/停止） -->
    <transition name="el-fade-in">
      <div v-if="partVisible" class="part-float">
        <div class="part-float-head" @click="partCollapsed = !partCollapsed">
          <div class="part-float-title">
            <el-icon class="is-loading" v-if="partProgress.running"><Loading /></el-icon>
            <span>{{ partProgress.running ? '参与进行中' : '参与队列' }}</span>
            <el-tag v-if="partQueueLen > 1" size="small" type="warning" effect="plain" class="ml4">
              队列 {{ partQueueLen }} 个
            </el-tag>
          </div>
          <div class="part-float-ops">
            <el-button size="small" text :icon="partCollapsed ? ArrowUp : ArrowDown" @click.stop="partCollapsed = !partCollapsed" />
            <el-button size="small" text :icon="Close" @click="hidePartFloat" />
          </div>
        </div>
        <div v-show="!partCollapsed" class="part-float-body">
          <template v-if="partProgress.running">
            <el-steps :active="partProgress.step_index" align-center finish-status="success" class="part-steps" size="small">
              <el-step title="点赞" />
              <el-step v-if="partSteps.includes('follow')" title="关注" />
              <el-step title="转发" />
              <el-step title="评论" />
            </el-steps>
            <div class="part-msg">
              <span>{{ partProgress.message || '准备中...' }}</span>
            </div>
            <div class="part-float-actions">
              <el-button size="small" type="danger" plain :icon="VideoPause" @click="cancelPart">
                停止
              </el-button>
            </div>
          </template>
          <template v-else-if="partProgress.done">
            <el-alert :type="partProgress.errors.length ? 'error' : 'success'" :closable="false" show-icon
              :title="partProgress.result_text || (partProgress.errors.length ? '参与失败' : '参与完成')" />
            <div v-if="partProgress.comment_text" class="part-comment dim">
              参与文案：{{ partProgress.comment_text }}
            </div>
            <el-descriptions v-if="partProgress.results.length" :column="1" size="small" border class="mt">
              <el-descriptions-item v-for="r in partProgress.results" :key="r.action">
                <template #label>{{ { like: '点赞', follow: '关注', repost: '转发', comment: '评论' }[r.action] || r.action }}</template>
                <span :class="r.ok ? 'ok-text' : 'err-text'">{{ r.message }}</span>
              </el-descriptions-item>
            </el-descriptions>
            <div v-if="partProgress.errors.length" class="err-text dim">{{ partProgress.errors.join('；') }}</div>
          </template>
          <template v-else>
            <div class="part-msg dim">等待开始...</div>
          </template>
          <!-- 队列列表 -->
          <div v-if="partQueuedItems.length" class="part-queue mt">
            <div class="part-queue-title dim">等待队列：</div>
            <div v-for="q in partQueuedItems" :key="q.activity_id" class="part-queue-item dim">
              #{{ q.queue_pos }} {{ q.title }}
            </div>
          </div>
        </div>
      </div>
    </transition>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh, RefreshLeft, Search, Delete, Select, VideoPause, User, Link, Cpu, Loading, Close, ArrowUp, ArrowDown } from '@element-plus/icons-vue'
import { activityApi, accountApi, autoApi } from '../api'

const statusMap = {
  pending: { label: '待参与', type: 'warning' },
  participated: { label: '已参与', type: 'primary' },
  skipped: { label: '已跳过', type: 'info' },
  failed: { label: '参与失败', type: 'danger' },
  ended: { label: '已结束', type: 'info' },
}
const statusOptions = [
  { label: '全部', value: '' },
  { label: '待参与', value: 'pending' },
  { label: '已参与', value: 'participated' },
  { label: '已结束', value: 'ended' },
  { label: '已跳过', value: 'skipped' },
]
const sourceLabel = (t) => t === 'repost' ? '转发' : t === 'publish' ? '发布' : '手动'

const stats = ref({ total: 0, pending: 0, participated: 0, skipped: 0, ended: 0, unreviewed: 0 })
const activities = ref([])
const accounts = ref([])
const loading = ref(false)
const total = ref(0)
const page = ref(1)
const pageSize = ref(25)

// 默认展示"待参与"的活动，默认按结束时间排序（后端固定）
const filter = reactive({ status: 'pending', keyword: '' })

// ---------- 全自动模式 ----------
const autoState = ref({
  running: false, message: '', round: 0, participated: 0,
  scanned_user: '', pending_count: 0, started_at: null, phase: 0,
  current_action: '', current_activity: '', current_account: '', action_log: [],
  scheduled: false, schedule_time: '',
  next_round_at: '', next_round_in: null,
  pro_discovery: { running: false, message: '', activity_id: null, paused_by_auto: false, result: null },
})
// 职业号发现状态（自动模式冷却期运行、轮次开始暂停）
const proState = computed(() => autoState.value.pro_discovery || {})
let autoTimer = null
// 下一轮倒计时（本地每秒递减，轮询 3s 时校正）
const nextRoundText = ref('')
let countdownTimer = null
function fmtCountdown(sec) {
  if (sec === null || sec === undefined || sec <= 0) return ''
  const m = Math.floor(sec / 60), s = sec % 60
  return `${m}分${String(s).padStart(2, '0')}秒`
}
function stopCountdown() { if (countdownTimer) { clearInterval(countdownTimer); countdownTimer = null } }
function syncCountdown(remainSec) {
  stopCountdown()
  if (remainSec === null || remainSec === undefined || remainSec <= 0) { nextRoundText.value = ''; return }
  nextRoundText.value = fmtCountdown(remainSec)
  countdownTimer = setInterval(() => {
    remainSec -= 1
    if (remainSec <= 0) { nextRoundText.value = ''; stopCountdown(); return }
    nextRoundText.value = fmtCountdown(remainSec)
  }, 1000)
}
// 动作日志类型 -> 图标（格式化美观展示）
function logIcon(type) {
  return { scan: '🔍', llm: '🤖', action: '🔄', success: '✅', error: '❌', start: '🚀', info: '•' }[type] || '•'
}
// 当前动作类型（取日志最后一条的 type，用于高亮）
const currentActionType = computed(() => {
  const log = autoState.value.action_log || []
  return log.length ? (log[log.length - 1].type || 'info') : 'info'
})
// 动作日志滚动条：新日志出现时自动滚到最底部（显示最新）
const autoLogScrollRef = ref(null)
watch(() => autoState.value.action_log?.length, async () => {
  await nextTick()
  const sb = autoLogScrollRef.value
  if (sb && sb.wrapRef) sb.wrapRef.scrollTop = sb.wrapRef.scrollHeight
})
// 全自动步骤高亮：0 检查 / 1 扫描中 / 2 参与中
async function startAuto() {
  const res = await autoApi.start()
  if (res.ok) ElMessage.success(res.message || '全自动模式已启动')
  else ElMessage.warning(res.message || '启动失败')
  pollAuto()
}
async function stopAuto() {
  const res = await autoApi.stop()
  ElMessage.info(res.message || '已请求停止')
}
async function pollAuto() {
  stopAutoPolling()
  autoTimer = setInterval(async () => {
    try {
      autoState.value = await autoApi.progress()
      syncCountdown(autoState.value.next_round_in)
      // 运行中实时刷新活动列表/统计（参与/扫描结果立刻可见）
      load(); loadStats()
      if (!autoState.value.running) {
        stopAutoPolling()
      }
    } catch (e) { /* ignore */ }
  }, 3000)
}
function stopAutoPolling() { if (autoTimer) { clearInterval(autoTimer); autoTimer = null } }

// 兜底定时刷新活动列表（全自动未运行时也自动刷新，默认 20s）
let listTimer = null
function startListPolling() {
  stopListPolling()
  listTimer = setInterval(() => { load(); loadStats() }, 20000)
}
function stopListPolling() { if (listTimer) { clearInterval(listTimer); listTimer = null } }

const selectedRows = ref([])
const batchParticipating = ref(false)

function isParticipated(row, acc) {
  return (row.participated_accounts || []).includes(acc.id)
}
// 该活动未参与过的账号（可点击参与）
function unparticipatedAccounts(row) {
  return accounts.value.filter(acc => !isParticipated(row, acc))
}
function isExpired(row) {
  return !!row.end_time && new Date(row.end_time.replace(/-/g, '/')) < new Date()
}
// 复核状态着色：已复核绿色、未复核红色（后端 reviewed_at 非空=已复核）
function reviewClass(row) {
  return row.reviewed_at ? 'rev-done' : 'rev-pending'
}
// 结束时间显示：去掉秒（B 站官方 lottery_time 精确到秒，展示到分钟即可，避免"假精确"）
function fmtEndTime(t) {
  if (!t) return ''
  const s = String(t).trim()
  return s.length > 16 ? s.slice(0, 16) : s
}

async function loadStats() { stats.value = await activityApi.stats() }

async function load() {
  loading.value = true
  try {
    const params = {
      page: page.value,
      page_size: pageSize.value,
      status: filter.status || '',
      keyword: filter.keyword || '',
    }
    const res = await activityApi.list(params)
    activities.value = res.items || []
    total.value = res.total || 0
  } finally { loading.value = false }
}

function search() { page.value = 1; load() }
function onSizeChange() { page.value = 1; load() }
function reset() {
  Object.assign(filter, { status: 'pending', keyword: '' })
  page.value = 1
  load()
}

function openLink(row) {
  window.open(row.link, '_blank')
}

function onSelectionChange(rows) {
  selectedRows.value = rows
}

async function batchParticipate() {
  const ids = selectedRows.value.map(r => r.id)
  if (!ids.length) return ElMessage.warning('请先勾选要参与的活动')
  batchParticipating.value = true
  try {
    const res = await activityApi.batchParticipate(ids, null)
    if (res.ok) ElMessage.success(res.message || `批量参与完成，共参与 ${res.count} 个`)
    else ElMessage.warning(res.message || '批量参与失败')
    load(); loadStats()
  } finally { batchParticipating.value = false }
}

async function batchRemove() {
  const ids = selectedRows.value.map(r => r.id)
  if (!ids.length) return
  const res = await activityApi.batchDelete(ids)
  ElMessage.success(`已批量删除 ${res.count} 个活动`)
  load(); loadStats()
}

// ---------- 参与 ----------
async function refreshAll() {
  const res = await activityApi.refreshStatus()
  ElMessage.success(`刷新完成，${res.changed} 个活动状态更新`)
  load(); loadStats()
}

async function participateDefault(row) {
  await doParticipate(row, null)
}

async function participateWith(row, acc) {
  await doParticipate(row, acc.id)
}

// ---------- 参与（悬浮队列 + 进度展示 + 可取消） ----------
const partVisible = ref(false)      // 悬浮窗显示
const partCollapsed = ref(false)    // 折叠
const partProgress = ref({ running: false, queued: false, done: false, phase: 'idle', step_index: 0,
  total: 0, action: '', message: '', results: [], errors: [], result_text: '', comment_text: '', queue_pos: 0 })
const partSteps = ref(['like', 'repost', 'comment'])
const partQueuedItems = ref([])     // 排队中的活动列表
const partQueueLen = computed(() => partQueuedItems.value.length + (partProgress.value.queued ? 1 : 0))
let partTimer = null
let partActivityId = null
let partReloaded = false   // 完成时已刷新过表格（防止重复 load 导致闪烁）

function stopPartPolling() { if (partTimer) { clearInterval(partTimer); partTimer = null } }

async function doParticipate(row, accountId) {
  const res = await activityApi.participate(row.id, accountId)
  if (res.ok && res.started) {
    partActivityId = row.id
    partReloaded = false
    partVisible.value = true
    partCollapsed.value = false
    // 立即拉一次进度（running 或 queued）
    await refreshPartProgress()
    pollPartStatus()
    ElMessage.success(res.message || '已加入参与队列')
  } else if (res.ok) {
    // 已参与过
    ElMessage.success(res.message || '参与成功')
    load(); loadStats()
  } else {
    ElMessage.warning(res.message || '参与失败')
    load(); loadStats()
  }
}

async function refreshPartProgress() {
  if (!partActivityId) return
  try {
    const p = await activityApi.participateProgress(partActivityId)
    partProgress.value = p
    const finished = p.done || (!p.running && !p.queued)
    if (finished) {
      // 完成：停止轮询（否则每 0.8s 都 load 导致表格闪），收起悬浮窗
      stopPartPolling()
      if (p.done) {
        setTimeout(() => { if (!partQueuedItems.value.length) partVisible.value = false }, 4000)
      }
      // 完成只刷新一次表格（用标志防止重复 load）
      if (!partReloaded) {
        partReloaded = true
        load(); loadStats()
      }
    }
  } catch (e) { /* ignore */ }
}

async function refreshPartQueue() {
  try {
    const s = await activityApi.participateStatus()
    // 队列项（当前 + 排队），补标题
    const ids = []
    if (s.running) ids.push(s.running.activity_id)
    ;(s.queued || []).forEach(q => ids.push(q.activity_id))
    partQueuedItems.value = (s.queued || []).map(q => ({ ...q, title: '' }))
    if (ids.length) {
      const acts = activities.value.filter(a => ids.includes(a.id))
      partQueuedItems.value = (s.queued || []).map(q => ({
        ...q, title: (acts.find(a => a.id === q.activity_id) || {}).title || ''
      }))
    }
  } catch (e) { /* ignore */ }
}

async function pollPartStatus() {
  stopPartPolling()
  partTimer = setInterval(async () => {
    await refreshPartProgress()
    await refreshPartQueue()
  }, 800)
}

async function cancelPart() {
  if (!partActivityId) return
  const res = await activityApi.participateCancel(partActivityId)
  if (res.ok) {
    ElMessage.info('已请求停止参与')
    await refreshPartProgress()
  } else {
    ElMessage.warning(res.message || '取消失败')
  }
}

function hidePartFloat() {
  stopPartPolling()
  partVisible.value = false
}

async function removeActivity(row) {
  await activityApi.remove(row.id)
  ElMessage.success('已删除')
  load(); loadStats()
}

// ---------- 发现职业抽奖号（异步 + 轮询） ----------
const proDiscoveringId = ref(null)
let proPollTimer = null
async function discoverPro(row) {
  proDiscoveringId.value = row.id
  try {
    const res = await activityApi.discoverPro(row.id)
    if (!res.ok || !res.started) {
      ElMessage.warning(res.message || '启动失败')
      proDiscoveringId.value = null
      return
    }
    ElMessage.info('已启动职业抽奖号发现，后台分析中...')
    // 轮询结果
    stopProPoll()
    proPollTimer = setInterval(async () => {
      try {
        const p = await activityApi.discoverProProgress()
        if (p.running) return
        stopProPoll()
        proDiscoveringId.value = null
        showProResult(p.result)
      } catch (e) { /* ignore */ }
    }, 5000)
  } catch (e) {
    proDiscoveringId.value = null
  }
}
function stopProPoll() { if (proPollTimer) { clearInterval(proPollTimer); proPollTimer = null } }
function showProResult(result) {
  const found = result?.found || []
  const added = result?.added || []
  if (found.length) {
    ElMessageBox.alert(
      `<div style="max-height:300px;overflow:auto">
        <p>${result?.message || ''}</p>
        ${found.map(f => `<div style="padding:4px 0">👤 ${f.uname} (${f.uid}) · 转发${f.total}条 · 抽奖${f.lottery}条 · ${Math.round(f.ratio * 100)}%${added.some(a => a.uid === f.uid) ? ' <span style="color:#67c23a">已加入监控</span>' : ''}</div>`).join('')}
      </div>`,
      '职业抽奖号发现结果', { dangerouslyUseHTMLString: true, customStyle: { maxWidth: '520px' } })
  } else {
    ElMessage.info(result?.message || '未发现职业抽奖账号')
  }
}

onMounted(() => {
  load(); loadStats()
  accountApi.list().then(list => { accounts.value = list || [] }).catch(() => {})
  autoApi.progress().then(s => {
    autoState.value = s
    if (s.running) pollAuto()
  }).catch(() => {})
  startListPolling()
})
onUnmounted(() => { stopAutoPolling(); stopCountdown(); stopPartPolling(); stopProPoll(); stopListPolling() })
</script>

<style scoped>
.mt { margin-top: 16px; }
.stat-row { display: flex; gap: 16px; margin-bottom: 16px; }
.stat-card { flex: 1; min-width: 0; }
.auto-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.auto-info { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.auto-msg { font-size: 12px; }
.auto-actions { display: flex; gap: 8px; }
.auto-detail { margin-top: 14px; padding: 10px 12px; background: var(--el-fill-color-light); border-radius: 8px; }
.auto-current { display: flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 600; }
.auto-current-icon { flex-shrink: 0; }
.auto-current-text { color: var(--el-color-primary); }
.cur-scan { color: #409eff; }
.cur-llm { color: #8b5cf6; }
.cur-action { color: #0ea5e9; }
.cur-success { color: #67c23a; }
.cur-error { color: #f56c6c; }
.cur-start { color: #e6a23c; }
.auto-activity { font-size: 12px; margin-top: 4px; }
.auto-pro { display: flex; align-items: center; gap: 8px; font-size: 12px; margin-top: 6px; flex-wrap: wrap; }
.auto-log-scroll { margin-top: 8px; }
.auto-log-item { display: flex; align-items: center; gap: 8px; font-size: 12px; padding: 3px 6px; line-height: 1.5; border-radius: 4px; }
.auto-log-item:hover { background: var(--el-fill-color-light); }
.auto-log-icon { flex-shrink: 0; width: 18px; text-align: center; }
.auto-log-ts { flex-shrink: 0; font-family: monospace; font-size: 11px; color: var(--el-text-color-placeholder); }
.auto-log-text { word-break: break-all; }
/* 按类型着色 */
.log-scan .auto-log-icon, .log-scan .auto-log-text { color: #409eff; }
.log-llm .auto-log-icon, .log-llm .auto-log-text { color: #8b5cf6; }
.log-action .auto-log-icon, .log-action .auto-log-text { color: #0ea5e9; }
.log-success .auto-log-icon, .log-success .auto-log-text { color: #67c23a; }
.log-error .auto-log-icon, .log-error .auto-log-text { color: #f56c6c; }
.log-start .auto-log-icon, .log-start .auto-log-text { color: #e6a23c; }
.log-info .auto-log-icon, .log-info .auto-log-text { color: var(--el-text-color-regular); }
.part-steps { margin-bottom: 12px; }
.part-msg { display: flex; align-items: center; gap: 8px; color: var(--el-text-color-regular); font-size: 13px; }
.part-comment { margin-top: 10px; font-size: 12px; }
.ok-text { color: var(--el-color-success); }
.err-text { color: var(--el-color-danger); }
.part-float {
  position: fixed; right: 20px; bottom: 20px; width: 360px; z-index: 2000;
  background: var(--el-bg-color); border: 1px solid var(--el-border-color-lighter);
  border-radius: 10px; box-shadow: 0 6px 24px rgba(0, 0, 0, .18); overflow: hidden;
}
.part-float-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 10px 12px; cursor: pointer; background: var(--el-fill-color-light);
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.part-float-title { display: flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 600; }
.part-float-ops { display: flex; align-items: center; }
.part-float-body { padding: 12px; max-height: 60vh; overflow-y: auto; }
.part-float-actions { margin-top: 10px; }
.part-queue-item { font-size: 12px; padding: 2px 0; }
.part-queue-title { font-size: 12px; margin-bottom: 2px; }
.ml4 { margin-left: 4px; }
.quick-filter { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.card-header { display: flex; justify-content: space-between; align-items: center; }
.header-left { display: flex; align-items: center; gap: 10px; }
.act-title { font-weight: 600; }
.act-link { color: var(--el-color-primary); text-decoration: none; }
.act-link:hover { text-decoration: underline; }
.act-sub { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--el-text-color-secondary); margin-top: 4px; }
.dim { color: var(--el-text-color-placeholder); }
.prize-multi {
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
  word-break: break-all;
  color: var(--el-color-danger);
  font-size: 13px;
  line-height: 1.5;
}
.comment-cell {
  display: inline-block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: bottom;
  font-size: 13px;
  color: var(--el-text-color-regular);
}
.expired { color: var(--el-color-danger); }
/* 复核状态着色：已复核亮白强调、未复核暗灰（标题/结束时间/奖品） */
.rev-done { color: #ffffff !important; font-weight: 600; }
.rev-pending { color: #909399 !important; }
.detail-desc { max-height: 120px; overflow-y: auto; white-space: pre-wrap; font-size: 13px; }
.mr4 { margin-right: 4px; }
.ops { display: flex; align-items: center; gap: 4px; white-space: nowrap; }
.acc-avatars { display: flex; align-items: center; gap: 6px; }
.acc-avatar { cursor: pointer; opacity: 0.35; border: 2px solid transparent; transition: all .2s; }
.acc-avatar:hover { opacity: 0.9; }
.acc-avatar.done { opacity: 1; border-color: var(--el-color-success); box-shadow: 0 0 0 1px var(--el-color-success); }
</style>
