<template>
  <div class="page">
    <!-- 统计 -->
    <el-row :gutter="16">
      <el-col :span="6"><el-card shadow="never"><el-statistic title="账号总数" :value="accounts.length" /></el-card></el-col>
      <el-col :span="6"><el-card shadow="never"><el-statistic title="已登录" :value="activeCount" /></el-card></el-col>
      <el-col :span="6"><el-card shadow="never"><el-statistic title="异常/未登录" :value="expiredCount" /></el-card></el-col>
      <el-col :span="6">
        <el-card shadow="never" class="action-card">
          <el-button type="primary" :icon="Plus" @click="openLoginDialog()">添加账号（扫码登录）</el-button>
        </el-card>
      </el-col>
    </el-row>

    <!-- 账号列表 -->
    <el-card shadow="never" class="mt">
      <template #header>
        <div class="card-header">
          <span>账号列表</span>
          <div>
            <el-button :icon="Download" size="small" @click="openImportDialog">导入 Cookie</el-button>
            <el-button :icon="Refresh" size="small" @click="load">刷新</el-button>
          </div>
        </div>
      </template>
      <el-table :data="accounts" v-loading="loading" stripe>
        <el-table-column label="账号" min-width="150">
          <template #default="{ row }">
            <div class="user-cell">
              <!-- 自绘角标：数字完全在头像框内，不超出单元格（el-table 行 overflow:hidden 会裁剪） -->
              <div class="avatar-wrap">
                <el-avatar :size="40" :src="row.avatar">
                  <el-icon><User /></el-icon>
                </el-avatar>
                <!-- 右上角：@提及 未读数 -->
                <span v-if="row.unread_at > 0" class="avatar-badge" title="被@未读">{{ row.unread_at > 99 ? '99+' : row.unread_at }}</span>
                <!-- 右下角：私信 未读数 -->
                <span v-if="row.unread_dm > 0" class="avatar-badge avatar-badge-dm" title="私信未读">{{ row.unread_dm > 99 ? '99+' : row.unread_dm }}</span>
              </div>
              <div>
                <div class="uname">
                  {{ row.username || '未命名' }}
                  <el-tag v-if="row.vip_status === 1" size="small" type="danger" effect="plain">大会员</el-tag>
                </div>
                <div class="uid">UID: {{ row.uid }}</div>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="等级" width="80">
          <template #default="{ row }">
            <span>Lv{{ row.level }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="coins" label="B币" width="80" />
        <el-table-column label="今日参与" width="90" align="center">
          <template #default="{ row }">
            <span>{{ row.today_participated ?? 0 }}</span>
          </template>
        </el-table-column>
        <el-table-column label="累计参与" width="90" align="center">
          <template #default="{ row }">
            <span>{{ row.total_participated ?? 0 }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <span>{{ row.status === 'active' ? '已登录' : '未登录' }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="last_login_at" label="最近登录" width="170" />
        <el-table-column label="操作" width="560" fixed="right">
          <template #default="{ row }">
            <div class="ops">
              <el-button size="small" type="danger" plain :icon="ChatDotRound"
                :disabled="row.status !== 'active'" @click="openMessages(row)">私信</el-button>
              <el-button size="small" type="primary" plain :icon="Bell" :disabled="row.status !== 'active'"
                @click="openMentions(row)">艾特</el-button>
              <el-button size="small" :icon="Refresh" :disabled="row.status !== 'active'" @click="refreshAccount(row)">刷新</el-button>
              <el-button size="small" :icon="Delete" :disabled="row.status !== 'active'" @click="openCleanup(row)">清理</el-button>
              <el-button size="small" :icon="Download" :disabled="row.status !== 'active'" @click="exportCookies(row)">Cookie</el-button>
              <el-button size="small" :icon="SwitchButton" @click="openLoginDialog(row)">重登</el-button>
              <el-button size="small" :icon="Delete" type="danger" @click="removeAccount(row)">删除</el-button>
            </div>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无账号，点击右上角添加账号" />
        </template>
      </el-table>
    </el-card>

    <!-- 扫码登录弹窗 -->
    <el-dialog v-model="loginVisible" :title="loginMode === 'relogin' ? '重新登录' : '添加账号'" width="420" :close-on-click-modal="false">
      <div class="qr-wrap">
        <div v-if="!qrUrl" class="qr-placeholder">
          <el-icon :size="48"><Camera /></el-icon>
          <p>请点击下方按钮生成二维码</p>
          <el-button type="primary" :loading="qrLoading" @click="generateQr">生成二维码</el-button>
        </div>
        <template v-else>
          <el-image :src="qrUrl" fit="contain" style="width: 220px; height: 220px" />
          <el-tag :type="qrStatusType" effect="dark" class="qr-status">{{ qrMessage }}</el-tag>
          <div class="qr-tips">使用哔哩哔哩 App 扫描二维码登录</div>
          <el-button size="small" @click="generateQr">刷新二维码</el-button>
        </template>
      </div>
    </el-dialog>

    <!-- 导入 Cookie 弹窗（剪贴板导入账号） -->
    <el-dialog v-model="importVisible" title="导入账号 Cookie" width="560" :close-on-click-modal="false">
      <p class="dim" style="margin: 0 0 8px">
        粘贴本工具导出的 Cookie JSON（先点账号行「Cookie」按钮复制，再粘贴到此处）：
      </p>
      <el-input v-model="importText" type="textarea" :rows="8" placeholder='[{"uid":"...","username":"...","cookies":"{...}"}]' />
      <div style="display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px">
        <el-button @click="importVisible = false">取消</el-button>
        <el-button type="primary" :loading="importLoading" @click="doImportCookies">导入</el-button>
      </div>
    </el-dialog>

    <!-- 私信回复弹窗 -->
    <el-dialog v-model="msgVisible" :title="`私信回复 - ${msgAccount?.username || ''}`" width="760" top="6vh">
      <div style="display: flex; justify-content: flex-end; margin-bottom: 8px;">
        <el-button size="small" type="primary" plain :loading="ackSessionsLoading" @click="ackAllSessions">
          全部已读（私信会话）
        </el-button>
      </div>
      <div class="msg-body">
        <div class="msg-left">
          <el-input v-model="msgKeyword" placeholder="搜索会话..." clearable size="small" class="mb8" />
          <div v-loading="sessionsLoading" class="session-list">
            <div v-for="s in filteredSessions" :key="s.talker_id"
                 class="session-item" :class="{ active: currentTalker === s.talker_id }"
                 @click="openThread(s)">
              <el-avatar :size="36" :src="s.avatar"><el-icon><User /></el-icon></el-avatar>
              <div class="session-info">
                <div class="s-name">{{ s.name }}</div>
                <div class="s-preview" v-html="emoteHtml(s.last_message)"></div>
              </div>
              <el-badge v-if="s.unread > 0" :value="s.unread" type="danger" />
            </div>
            <el-empty v-if="!sessionsLoading && filteredSessions.length === 0" description="暂无私信" :image-size="60" />
          </div>
        </div>
        <div class="msg-right">
          <div class="thread" v-loading="threadLoading">
            <div v-for="(m, i) in thread" :key="i" class="bubble-row" :class="m.sender">
              <div class="bubble" :class="m.sender" v-html="emoteHtml(m.content)"></div>
              <div class="b-time">{{ m.time }} · {{ m.sender_name }}</div>
            </div>
            <el-empty v-if="!threadLoading && thread.length === 0" description="点击左侧会话查看消息" :image-size="60" />
          </div>
        </div>
      </div>
    </el-dialog>

    <!-- 评论/艾特弹窗（被@ + 评论回复） -->
    <el-dialog v-model="mentionVisible" :title="`评论/艾特 - ${mentionAccount?.username || ''}`" width="680" top="6vh">
      <div style="display: flex; justify-content: flex-end; margin-bottom: 8px;">
        <el-button size="small" type="primary" plain :disabled="mentionLoading" @click="ackAllMentions">
          全部已读（@提及 + 评论回复）
        </el-button>
      </div>
      <el-tabs v-model="mentionTab">
        <el-tab-pane :label="`被@提及 (${atItems.length})`" name="at">
          <div v-loading="mentionLoading" class="mention-list">
            <div v-for="(m, i) in atItems" :key="i" class="mention-item">
              <a v-if="m.from_uid" :href="`https://space.bilibili.com/${m.from_uid}`" target="_blank" rel="noopener"
                 class="mention-avatar-link" :title="`打开 ${m.from_user || m.from_uid} 的主页`">
                <el-avatar :size="34" :src="m.from_avatar" class="mention-avatar"><el-icon><User /></el-icon></el-avatar>
              </a>
              <el-avatar v-else :size="34" :src="m.from_avatar" class="mention-avatar"><el-icon><User /></el-icon></el-avatar>
              <div class="mention-info">
                <div class="mention-head">
                  <span class="mention-user">{{ m.from_user || m.from_uid }}</span>
                  <span class="mention-time dim">{{ m.time }}</span>
                </div>
                <div class="mention-origin" v-if="m.title || m.activity_title">
                  <el-tag v-if="m.activity_title" size="small" type="warning" effect="plain" class="mention-activity-tag">活动</el-tag>
                  <span class="mention-origin-text">{{ m.activity_title || m.title }}</span>
                </div>
                <div class="mention-content" v-html="m.content ? emoteHtml(m.content) : '（无文本内容）'"></div>
                <a v-if="m.link" :href="m.link" target="_blank" rel="noopener" class="mention-link">查看原动态 →</a>
              </div>
            </div>
            <el-empty v-if="!mentionLoading && atItems.length === 0" description="暂无@提及" :image-size="60" />
          </div>
        </el-tab-pane>
        <el-tab-pane :label="`评论回复 (${replyItems.length})`" name="reply">
          <div v-loading="mentionLoading" class="mention-list">
            <div v-for="(m, i) in replyItems" :key="i" class="mention-item">
              <a v-if="m.from_uid" :href="`https://space.bilibili.com/${m.from_uid}`" target="_blank" rel="noopener"
                 class="mention-avatar-link" :title="`打开 ${m.from_user || m.from_uid} 的主页`">
                <el-avatar :size="34" :src="m.from_avatar" class="mention-avatar"><el-icon><User /></el-icon></el-avatar>
              </a>
              <el-avatar v-else :size="34" :src="m.from_avatar" class="mention-avatar"><el-icon><User /></el-icon></el-avatar>
              <div class="mention-info">
                <div class="mention-head">
                  <span class="mention-user">{{ m.from_user || m.from_uid }}</span>
                  <span class="mention-time dim">{{ m.time }}</span>
                </div>
                <div class="mention-origin" v-if="m.title || m.activity_title">
                  <el-tag v-if="m.activity_title" size="small" type="warning" effect="plain" class="mention-activity-tag">活动</el-tag>
                  <span class="mention-origin-text">{{ m.activity_title || m.title }}</span>
                </div>
                <div class="mention-content" v-html="m.content ? emoteHtml(m.content) : '（无文本内容）'"></div>
                <a v-if="m.link" :href="m.link" target="_blank" rel="noopener" class="mention-link">查看原动态 →</a>
              </div>
            </div>
            <el-empty v-if="!mentionLoading && replyItems.length === 0" description="暂无评论回复" :image-size="60" />
          </div>
        </el-tab-pane>
      </el-tabs>
    </el-dialog>

    <!-- 清理动态弹窗（账号维度：规则勾选 AND 组合，先统计再删除） -->
    <el-dialog v-model="cleanupVisible" :title="`清理动态 - ${cleanupAcc ? cleanupAcc.username : ''}`"
      width="700" :close-on-click-modal="false">
      <el-form label-width="170px">
        <el-form-item label="清理规则（勾选生效）">
          <div style="width: 100%">
            <div class="rule-item">
              <el-checkbox v-model="cleanupForm.r1" />规则1：列表内活动结束日期超过
              <el-input-number v-model="cleanupForm.end_days" :min="0" :max="365" size="small" style="width: 90px" />天
              <span class="hint">（此期间自行检查中奖）</span>
            </div>
            <div class="rule-item">
              <el-checkbox v-model="cleanupForm.r2" />规则2：转发时间距今超过
              <el-input-number v-model="cleanupForm.forward_days" :min="0" :max="3650" size="small" style="width: 90px" />天
              直接删（不检查列表/结束日期）
            </div>
            <div class="rule-item">
              <el-checkbox v-model="cleanupForm.r3" />规则3：列表外动态用 LLM 解析结束时间再判定（需启用 LLM）
            </div>
            <div class="rule-item">
              <el-checkbox v-model="cleanupForm.r4" />规则4（互动抽奖）：官方互动抽奖已截止 → 专门检查并清除
            </div>
            <div class="dim" style="margin-top: 4px">勾选的规则需<el-tag size="small" type="warning" effect="plain">同时满足（AND）</el-tag>才删除；至少勾选 1 条</div>
          </div>
        </el-form-item>
        <el-form-item label="翻页间隔（秒）">
          <el-input-number v-model="cleanupForm.scan_gap" :min="0" :max="30" :step="0.5" />
          <span class="hint" style="margin-left: 8px">扫描动态时每页间隔，防风控（建议 1~2）</span>
        </el-form-item>
        <el-form-item label="中奖白名单">
          <div style="width: 100%">
            <el-input v-model="cleanupWhitelistText" type="textarea" :rows="3"
              placeholder="每行一个中奖动态 id，清理时跳过不删" />
            <el-button size="small" style="margin-top: 4px" @click="saveWhitelist">保存白名单（{{ whitelistCount }} 条）</el-button>
          </div>
        </el-form-item>
      </el-form>
      <div class="cleanup-result">
        <!-- 统计过程进度展示 -->
        <div v-if="cleanupRunning" class="dim" style="margin-bottom: 8px">
          <el-progress :percentage="cleanupPercent" :stroke-width="10" style="margin-bottom: 6px" />
          <div>{{ cleanupStage }}</div>
          <div v-if="cleanupProgress.forwards">已扫描转发 {{ cleanupProgress.forwards }} 条 · 候选 {{ cleanupProgress.candidates }} 条</div>
          <div v-if="cleanupProgress.stage?.includes('删除中')">
            <el-tag type="danger" size="large">已删除 {{ cleanupProgress.deleted }} / {{ cleanupProgress.candidates }} 条</el-tag>
          </div>
        </div>
        <!-- 统计结果展示 -->
        <div v-else-if="cleanupStat" class="dim" style="margin-bottom: 8px">
          可删除 {{ cleanupStat.to_delete }} | 白名单跳过 {{ cleanupStat.whitelisted }} |
          已截止(检查期内) {{ cleanupStat.ended_recent }} | 未截止 {{ cleanupStat.not_ended }} |
          未解析 {{ cleanupStat.no_llm }} | 失败 {{ cleanupStat.failed }}
          <template v-if="cleanupStat.dry_run === false">· 已删除 {{ cleanupStat.deleted }}</template>
        </div>
        <el-table v-if="cleanupStat && cleanupStat.items.length" :data="cleanupStat.items" size="small" max-height="280" stripe>
          <el-table-column label="源动态ID" width="190">
            <template #default="{ row }">
              <el-link type="primary" :href="`https://www.bilibili.com/opus/${row.orig_id}`" target="_blank" :underline="false">
                {{ row.orig_id }}
              </el-link>
            </template>
          </el-table-column>
          <el-table-column prop="end_time" label="结束时间" width="130" />
          <el-table-column prop="reason" label="原因" />
        </el-table>
        <!-- 已截止但未满检查期的动态（明确识别展示） -->
        <el-collapse v-if="cleanupStat && cleanupStat.recent_items?.length" style="margin-top: 6px">
          <el-collapse-item :title="`已截止但未满检查期（${cleanupStat.recent_items.length} 条，N 天内自行检查中奖，暂不删除）`">
            <el-table :data="cleanupStat.recent_items" size="small" max-height="200">
              <el-table-column label="源动态ID" width="190">
                <template #default="{ row }">
                  <el-link type="primary" :href="`https://www.bilibili.com/opus/${row.orig_id}`" target="_blank" :underline="false">
                    {{ row.orig_id }}
                  </el-link>
                </template>
              </el-table-column>
              <el-table-column prop="end_time" label="截止时间" width="130" />
              <el-table-column prop="reason" label="状态" />
            </el-table>
          </el-collapse-item>
        </el-collapse>
        <el-empty v-else-if="cleanupStat" description="没有符合条件的转发动态" :image-size="60" />
      </div>
      <template #footer>
        <el-button @click="cleanupVisible = false" :disabled="cleanupRunning">关闭</el-button>
        <el-button type="warning" :loading="cleanupLoading" :disabled="cleanupRunning" @click="cleanupAccountPreview">统计</el-button>
        <el-button type="danger" :disabled="cleanupRunning || !cleanupStat"
          @click="cleanupAccountRun">删除 {{ cleanupStat ? cleanupStat.to_delete : 0 }} 条</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox, ElNotification } from 'element-plus'
import { Plus, Refresh, Delete, User, ChatDotRound, Camera, SwitchButton, Bell, Download } from '@element-plus/icons-vue'
import { accountApi, cleanupApi, settingApi } from '../api'
import { emoteHtml, loadEmotes } from '../utils/emote'

const accounts = ref([])
const loading = ref(false)

// ---------- 清理动态（账号维度） ----------
const cleanupVisible = ref(false)
const cleanupAcc = ref(null)
const cleanupForm = reactive({ r1: true, r2: false, r3: false, r4: false, end_days: 7, forward_days: 0, scan_gap: 1.0 })
const cleanupLoading = ref(false)
const cleanupStat = ref(null)
const cleanupRunning = ref(false)
const cleanupStage = ref('')
const cleanupProgress = ref({})
const cleanupPercent = computed(() => {
  const p = cleanupProgress.value
  if (!p.pages && !p.forwards) return 5
  if (p.stage?.includes('删除中')) {
    return Math.min(99, Math.round(((p.deleted || 0) + (p.failed || 0)) / Math.max(1, (p.candidates || 1)) * 100))
  }
  return 60   // 扫描/匹配阶段固定展示中段进度
})
const cleanupWhitelistText = ref('')
const whitelistCount = ref(0)

// 白名单读写（存设置 cleanup_whitelist，JSON 数组）
async function loadWhitelist() {
  try {
    const s = await settingApi.get()
    let list = []
    try { list = JSON.parse(s.cleanup_whitelist || '[]') } catch (e) { list = [] }
    if (!Array.isArray(list)) list = []
    cleanupWhitelistText.value = list.join('\n')
    whitelistCount.value = list.length
  } catch (e) { /* ignore */ }
}
async function saveWhitelist() {
  const ids = cleanupWhitelistText.value.split('\n').map(s => s.trim()).filter(Boolean)
  await settingApi.save({ cleanup_whitelist: JSON.stringify(ids) })
  whitelistCount.value = ids.length
  ElMessage.success(`白名单已保存（${ids.length} 条）`)
}

const activeCount = computed(() => accounts.value.filter(a => a.status === 'active').length)
const expiredCount = computed(() => accounts.value.length - activeCount.value)

async function load() {
  loading.value = true
  try {
    accounts.value = await accountApi.list()
    // 账号列表刷新后立即重算未读基线，避免历史未读误报
    refreshUnreadBaseline()
    refreshUnreadBadges()
  } finally { loading.value = false }
}

// 导出指定账号的登录 cookies（JSON，迁移到其他机器/exe/Docker 时用）
async function exportCookies(row) {
  try {
    const data = await accountApi.exportCookies(row.id)
    const text = JSON.stringify(data, null, 2)
    try {
      await navigator.clipboard.writeText(text)
      ElMessage.success(`已复制 ${row.username} 的 cookies 到剪贴板`)
    } catch (e) {
      // 剪贴板 API 不可用（非 https/localhost）时兜底 textarea 复制
      const ta = document.createElement('textarea')
      ta.value = text
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
      ElMessage.success(`已复制 ${row.username} 的 cookies（兼容模式）`)
    }
  } catch (e) { /* 拦截器已提示 */ }
}

// ---------- 导入 Cookie（剪贴板 JSON → 后端验证 → 按 uid 去重建/更新账号） ----------
const importVisible = ref(false)
const importText = ref('')
const importLoading = ref(false)
function openImportDialog() {
  importText.value = ''
  importVisible.value = true
}
async function doImportCookies() {
  const raw = (importText.value || '').trim()
  if (!raw) {
    ElMessage.warning('请先粘贴 Cookie JSON')
    return
  }
  importLoading.value = true
  try {
    const res = await accountApi.importCookies({ cookies_json: raw })
    const imported = res.imported || []
    const failed = res.failed || []
    if (res.ok === false && imported.length === 0) {
      ElMessage.error(failed[0]?.reason || '导入失败')
      return
    }
    if (imported.length > 0) {
      ElMessage.success(`成功导入 ${imported.length} 个账号：${imported.map(i => i.username).join('、')}`)
      importVisible.value = false
      load()
      refreshUnreadBadges()
    }
    if (failed.length > 0) {
      ElMessage.warning(`${failed.length} 个失败：${failed[0].reason}`)
    }
  } catch (e) {
    ElMessage.error('导入失败：' + (e?.message || '请检查 JSON 格式'))
  } finally {
    importLoading.value = false
  }
}

// ---------- 清理动态（账号维度）：条件设置 → 统计 → 删除 ----------
function openCleanup(row) {
  cleanupAcc.value = row
  cleanupStat.value = null
  cleanupVisible.value = true
  loadWhitelist()
}

function cleanupPayload() {
  // 白名单文本 → 数组
  const wl = cleanupWhitelistText.value.split('\n').map(s => s.trim()).filter(Boolean)
  return {
    r1: cleanupForm.r1,
    r2: cleanupForm.r2,
    r3: cleanupForm.r3,
    r4: cleanupForm.r4,
    end_days: cleanupForm.end_days,
    forward_days: cleanupForm.forward_days,
    llm_parse: cleanupForm.r3,
    interactive_clean: cleanupForm.r4,
    scan_gap: cleanupForm.scan_gap,
    whitelist: wl,
  }
}

// 轮询进度直到完成，完成时返回 result
async function pollCleanupProgress(accountId, timeoutMs = 600000) {
  const start = Date.now()
  return new Promise((resolve, reject) => {
    const timer = setInterval(async () => {
      try {
        const p = await cleanupApi.accountProgress(accountId)
        cleanupProgress.value = p
        cleanupStage.value = p.stage || ''
        // 完成 = 线程结束且已有 result（刚启动时 running 可能还没置 True，需等待）
        if (!p.running && (p.result || p.stage === '完成')) {
          clearInterval(timer)
          resolve(p.result || null)
        } else if (!p.running && !p.result && p.stage === '未开始') {
          // 线程刚启动，progress 还没初始化，继续等
        } else if (!p.running && p.stage === '出错: 请至少勾选一条清理规则') {
          clearInterval(timer)
          resolve(p.result || null)
        } else if (Date.now() - start > timeoutMs) {
          clearInterval(timer)
          reject(new Error('统计超时'))
        }
      } catch (e) {
        clearInterval(timer)
        reject(e)
      }
    }, 1200)
  })
}

async function cleanupAccountPreview() {
  cleanupRunning.value = true
  cleanupLoading.value = true
  cleanupStat.value = null
  cleanupStage.value = '启动中...'
  cleanupProgress.value = { running: true, stage: '启动中...' }
  try {
    await cleanupApi.accountDynamics(cleanupAcc.value.id, {
      ...cleanupPayload(), dry_run: true,
    })
    const result = await pollCleanupProgress(cleanupAcc.value.id)
    if (result?.error) {
      ElMessage.warning(result.error)
      cleanupStat.value = null
    } else {
      cleanupStat.value = { ...result, dry_run: true }
      ElMessage.success(`统计完成：可删除 ${result.to_delete} 条`)
    }
  } catch (e) {
    if (e?.response?.data?.detail) ElMessage.warning(e.response.data.detail)
    else if (!/超时|拦截/.test(String(e))) ElMessage.error('统计失败')
  } finally {
    cleanupRunning.value = false
    cleanupLoading.value = false
  }
}

async function cleanupAccountRun() {
  const n = cleanupStat.value?.to_delete || 0
  if (!n) {
    ElMessage.info('统计结果为 0，没有可删除的动态。请检查规则勾选或先点「统计」')
    return
  }
  try {
    await ElMessageBox.confirm(
      `将删除该账号 ${n} 条转发动态，不可恢复！确定执行？`,
      '危险操作确认', { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' })
  } catch (e) { return }
  cleanupRunning.value = true
  cleanupLoading.value = true
  cleanupStage.value = '删除中...'
  cleanupProgress.value = { running: true, stage: '删除中...', candidates: n, deleted: 0 }
  try {
    await cleanupApi.accountDynamics(cleanupAcc.value.id, {
      ...cleanupPayload(), dry_run: false,
      items: cleanupStat.value.items || [],   // 复用统计结果，立即开始删除
    })
    const result = await pollCleanupProgress(cleanupAcc.value.id)
    if (result?.error) {
      ElMessage.warning(result.error)
    } else {
      cleanupStat.value = { ...result, dry_run: false }
      ElMessage.success(`已删除 ${result.deleted} 条（失败 ${result.failed}）`)
    }
  } catch (e) {
    if (e?.response?.data?.detail) ElMessage.warning(e.response.data.detail)
    else if (!/超时|拦截/.test(String(e))) ElMessage.error('删除失败')
  } finally {
    cleanupRunning.value = false
    cleanupLoading.value = false
  }
}

// ---------- 账号未读徽标（头像上显示，独立于私信检测白名单） ----------
let unreadBadgeTimer = null
async function refreshUnreadBadges() {
  // 查询每个 active 账号未读数：@提及（右上角徽标）+ 私信（右下角徽标）
  for (const acc of accounts.value) {
    if (acc.status !== 'active') continue
    try {
      const u = await accountApi.unread(acc.id)
      acc.unread_at = u.at_count || 0          // 被@未读
      acc.unread_dm = u.dm_count || 0          // 私信未读
      acc.unread_total = acc.unread_at + acc.unread_dm
    } catch (e) { acc.unread_at = 0; acc.unread_dm = 0; acc.unread_total = 0 }
  }
}
function startUnreadBadgePolling() {
  stopUnreadBadgePolling()
  unreadBadgeTimer = setInterval(refreshUnreadBadges, 60000)  // 每分钟刷新角标
}
function stopUnreadBadgePolling() { if (unreadBadgeTimer) { clearInterval(unreadBadgeTimer); unreadBadgeTimer = null } }

// ---------- 扫码登录 / 重登 ----------
const loginVisible = ref(false)
const loginMode = ref('login')
const reloginAccount = ref(null)
const qrUrl = ref('')
const qrKey = ref('')
const qrLoading = ref(false)
const qrMessage = ref('')
const qrStatusType = ref('info')
let pollTimer = null

function openLoginDialog(account) {
  loginMode.value = account ? 'relogin' : 'login'
  reloginAccount.value = account || null
  loginVisible.value = true
  qrUrl.value = ''; qrKey.value = ''; qrMessage.value = ''
  generateQr()
}

async function generateQr() {
  qrLoading.value = true
  qrMessage.value = '正在生成二维码...'; qrStatusType.value = 'info'
  try {
    const res = await accountApi.genQr(loginMode.value === 'relogin' ? reloginAccount.value?.id : null)
    qrKey.value = res.qrcode_key
    qrUrl.value = res.image_url
    qrMessage.value = '等待扫码...'; qrStatusType.value = 'info'
    startPolling()
  } finally {
    qrLoading.value = false
  }
}

function startPolling() {
  stopPolling()
  pollTimer = setInterval(async () => {
    if (!qrKey.value) return
    try {
      const res = await accountApi.pollQr(qrKey.value)
      qrMessage.value = res.message
      if (res.status === 'pending' || res.status === 'scanned') {
        qrStatusType.value = res.status === 'scanned' ? 'warning' : 'info'
      } else if (res.status === 'expired') {
        qrStatusType.value = 'danger'
        stopPolling()
      } else if (res.status === 'success') {
        qrStatusType.value = 'success'
        stopPolling()
        const tip = loginMode.value === 'relogin'
          ? `重新登录成功：${res.account.username}`
          : `登录成功：${res.account.username}`
        ElMessage.success(tip)
        loginVisible.value = false
        qrUrl.value = ''; qrKey.value = ''
        load()
      }
    } catch (e) { /* 轮询失败忽略，下次重试 */ }
  }, 2000)
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}

// ---------- 私信回复轮询（新回复提示） ----------
// 白名单时间段检测（设置页可配置）：
//   dm_check_interval_min 检测间隔（分钟，默认 30）
//   dm_check_start/end    白名单窗口（默认 08:00 ~ 22:00，窗口内按间隔检测，其余时间不检测）
let dmPollTimer = null
let dmConfig = { intervalMin: 30, start: '08:00', end: '22:00' }
const unreadBase = new Map()     // accountId -> { dm, at } 上次未读数

// 当前时间是否在检测窗口内（支持跨天：22:00 ~ 09:00）
function inCheckWindow() {
  const now = new Date()
  const curMin = now.getHours() * 60 + now.getMinutes()
  const [sh, sm] = (dmConfig.start || '22:00').split(':').map(Number)
  const [eh, em] = (dmConfig.end || '09:00').split(':').map(Number)
  const startMin = sh * 60 + (sm || 0)
  const endMin = eh * 60 + (em || 0)
  if (startMin < endMin) return curMin >= startMin && curMin < endMin
  return curMin >= startMin || curMin < endMin   // 跨天（如 22:00~09:00）
}

async function loadDmConfig() {
  try {
    const { settingApi } = await import('../api')
    const s = await settingApi.get()
    dmConfig = {
      intervalMin: Number(s.dm_check_interval_min) || 30,
      start: s.dm_check_start || '08:00',
      end: s.dm_check_end || '22:00',
    }
  } catch (e) { /* 用默认值 */ }
  return dmConfig
}

async function checkUnread() {
  // 白名单外不请求接口（省流量+不打扰）；窗口内首次检查只建基线不提示
  if (!inCheckWindow()) return
  const actives = accounts.value.filter(a => a.status === 'active')
  for (const acc of actives) {
    try {
      const u = await accountApi.unread(acc.id)
      const dm = u.dm_count || 0
      const at = u.at_count || 0
      const prev = unreadBase.get(acc.id)
      if (prev) {
        // 仅当未读数比上次增加时才提示
        if (dm > prev.dm || at > prev.at) {
          ElNotification({
            title: '📩 新的私信回复',
            message: `${acc.username} 收到新私信/回复（私信 ${dm}，@ ${at}）`,
            type: 'info',
            duration: 6000,
            position: 'bottom-right',
          })
        }
      }
      unreadBase.set(acc.id, { dm, at })
    } catch (e) { /* 单个账号查询失败忽略 */ }
  }
}

function refreshUnreadBaseline() {
  // 仅记录基线，不触发提示（避免把历史未读当新消息）
  accounts.value.filter(a => a.status === 'active').forEach(a => {
    accountApi.unread(a.id).then(u => {
      unreadBase.set(a.id, { dm: u.dm_count || 0, at: u.at_count || 0 })
    }).catch(() => {})
  })
}

async function startDmPolling() {
  stopDmPolling()
  const cfg = await loadDmConfig()
  // 立即执行一次（若在窗口内），随后按配置间隔轮询
  checkUnread()
  const intervalMs = Math.max(1, cfg.intervalMin) * 60 * 1000
  dmPollTimer = setInterval(checkUnread, intervalMs)
}
function stopDmPolling() {
  if (dmPollTimer) { clearInterval(dmPollTimer); dmPollTimer = null }
}

// ---------- 删除 / 刷新 ----------
async function removeAccount(row) {
  await ElMessageBox.confirm(`确定删除账号 ${row.username}(${row.uid})？`, '提示', { type: 'warning' })
  await accountApi.remove(row.id)
  unreadBase.delete(row.id)
  ElMessage.success('已删除')
  load()
}
async function refreshAccount(row) {
  const updated = await accountApi.refresh(row.id)
  if (updated.status === 'active') ElMessage.success('刷新成功')
  else ElMessage.warning('刷新失败，账号可能已失效')
  load()
}

// ---------- 私信回复 ----------
const msgVisible = ref(false)
const msgAccount = ref(null)
const sessions = ref([])
const sessionsLoading = ref(false)
const msgKeyword = ref('')
const currentTalker = ref('')
const thread = ref([])
const threadLoading = ref(false)

const filteredSessions = computed(() =>
  sessions.value.filter(s =>
    !msgKeyword.value || s.name.includes(msgKeyword.value) || (s.last_message || '').includes(msgKeyword.value)))

async function openMessages(row) {
  msgAccount.value = row
  msgVisible.value = true
  currentTalker.value = ''
  thread.value = []
  sessionsLoading.value = true
  try {
    const res = await accountApi.messages(row.id)
    sessions.value = res.sessions || []
  } finally {
    sessionsLoading.value = false
  }
}

// ---------- 评论/艾特（被@ + 评论回复） ----------
const mentionVisible = ref(false)
const mentionTab = ref('at')
const mentionAccount = ref(null)
const mentionLoading = ref(false)
const atItems = ref([])
const replyItems = ref([])

async function openMentions(row) {
  mentionAccount.value = row
  mentionVisible.value = true
  mentionTab.value = 'at'
  mentionLoading.value = true
  atItems.value = []
  replyItems.value = []
  try {
    const [atRes, replyRes] = await Promise.all([
      accountApi.atMessages(row.id).catch(() => ({ items: [] })),
      accountApi.replyMessages(row.id).catch(() => ({ items: [] })),
    ])
    atItems.value = atRes.items || []
    replyItems.value = replyRes.items || []
  } finally {
    mentionLoading.value = false
  }
}

// 私信会话「全部已读」：遍历所有未读会话逐个标记已读（read_session），角标立即清空
const ackSessionsLoading = ref(false)
async function ackAllSessions() {
  if (!msgAccount.value) return
  const id = msgAccount.value.id
  const unreadOnes = (sessions.value || []).filter(s => (s.unread || 0) > 0)
  if (unreadOnes.length === 0) {
    ElMessage.info('当前没有未读会话')
    return
  }
  ackSessionsLoading.value = true
  try {
    let okCnt = 0
    for (const s of unreadOnes) {
      try {
        await accountApi.readSession(id, s.talker_id, s.last_seqno)
        s.unread = 0
        okCnt++
      } catch (e) { /* 单个失败不中断 */ }
    }
    ElMessage.success(`已将 ${okCnt}/${unreadOnes.length} 个私信会话标记为已读`)
    refreshUnreadBadges()
  } finally {
    ackSessionsLoading.value = false
  }
}

async function openThread(s) {
  currentTalker.value = s.talker_id
  threadLoading.value = true
  try {
    const res = await accountApi.messageThread(msgAccount.value.id, s.talker_id)
    thread.value = res.messages || []
    // 打开会话即标记该私信会话已读（需带上最后消息序号 ack_seqno）
    if (s.unread > 0 && msgAccount.value) {
      accountApi.readSession(msgAccount.value.id, s.talker_id, s.last_seqno).then(() => {
        s.unread = 0
        refreshUnreadBadges()
      }).catch(() => {})
    }
  } finally {
    threadLoading.value = false
  }
}

// 点击评论人头像 → 新标签打开其 B 站主页（uid 为空时不响应）
// 注：改用原生 <a href> 链接实现（见模板），window.open 会被浏览器弹窗拦截

async function ackAllMentions() {
  if (!mentionAccount.value) return
  const id = mentionAccount.value.id
  await Promise.all([
    accountApi.ackAtUnread(id).catch(() => {}),
    accountApi.ackReplyUnread(id).catch(() => {}),
  ])
  ElMessage.success('已将 @提及 和 评论回复 全部标记为已读')
  refreshUnreadBadges()
}

onMounted(() => {
  loadEmotes()       // 加载 B 站官方表情映射（评论/私信渲染）
  load()             // 内部含 refreshUnreadBadges()：进入页面即检测红点
  startDmPolling()
  startUnreadBadgePolling()
})
onUnmounted(() => {
  stopPolling()
  stopDmPolling()
  stopUnreadBadgePolling()
})
</script>

<style scoped>
.mt { margin-top: 16px; }
.rule-item { display: flex; align-items: center; gap: 4px; margin-bottom: 8px; line-height: 1.6; }
.rule-item .el-checkbox { margin-right: 2px; }
.card-header { display: flex; justify-content: space-between; align-items: center; }
.user-cell { display: flex; align-items: center; gap: 10px; }
.uname { font-weight: 600; display: flex; align-items: center; gap: 6px; }
.avatar-wrap {
  position: relative;
  display: inline-flex;
  flex-shrink: 0;
  line-height: 0;
}
/* 未读角标：绝对定位在头像内部右上角，绝不超出容器（不会被表格行裁剪） */
.avatar-badge {
  position: absolute;
  right: -2px;
  top: -2px;
  min-width: 16px;
  height: 16px;
  line-height: 16px;
  padding: 0 4px;
  border-radius: 8px;
  background: #409eff;   /* 右上角：被@未读（蓝色，对应艾特按钮） */
  color: #fff;
  font-size: 11px;
  font-weight: 600;
  text-align: center;
  box-sizing: border-box;
  box-shadow: 0 0 0 1px var(--el-bg-color);
  z-index: 2;
}
/* 右下角：私信未读（红色，对应私信按钮） */
.avatar-badge-dm {
  right: -2px;
  top: auto;
  bottom: -2px;
  background: #f56c6c;
}
.uid { font-size: 12px; color: var(--el-text-color-secondary); }
.qr-wrap { display: flex; flex-direction: column; align-items: center; gap: 14px; padding: 10px 0; }
.qr-placeholder { display: flex; flex-direction: column; align-items: center; gap: 12px; color: var(--el-text-color-secondary); }
.qr-status { font-size: 13px; }
.qr-tips { font-size: 12px; color: var(--el-text-color-secondary); }
.mention-list { max-height: 55vh; overflow-y: auto; }
.mention-item { display: flex; gap: 10px; padding: 10px 0; border-bottom: 1px solid var(--el-border-color-lighter); }
.mention-avatar-link { display: inline-flex; cursor: pointer; text-decoration: none; border-radius: 50%; line-height: 0; }
.mention-avatar-link:hover .mention-avatar { opacity: 0.85; }
.mention-info { flex: 1; min-width: 0; }
.mention-head { display: flex; align-items: center; gap: 8px; }
.mention-user { font-weight: 600; font-size: 13px; }
.mention-time { font-size: 12px; }
.mention-content { font-size: 13px; margin-top: 3px; line-height: 1.5; word-break: break-all; }
.mention-origin { display: flex; align-items: center; gap: 6px; margin-top: 4px; font-size: 12px; color: var(--el-text-color-secondary); }
.mention-origin-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mention-activity-tag { flex-shrink: 0; }
.mention-link { font-size: 12px; color: var(--el-color-primary); text-decoration: none; }
.action-card { display: flex; align-items: center; height: 100%; }
.ops { display: flex; align-items: center; gap: 4px; white-space: nowrap; }
.msg-body { display: flex; gap: 12px; height: 420px; }
.msg-left { width: 280px; display: flex; flex-direction: column; border-right: 1px solid var(--el-border-color-lighter); padding-right: 12px; }
.session-list { flex: 1; overflow-y: auto; }
.session-item { display: flex; align-items: center; gap: 10px; padding: 8px; border-radius: 8px; cursor: pointer; }
.session-item:hover { background: var(--el-fill-color-light); }
.session-item.active { background: var(--el-color-primary-light-9); }
.session-info { flex: 1; overflow: hidden; }
.s-name { font-weight: 600; font-size: 13px; }
.s-preview { font-size: 12px; color: var(--el-text-color-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.msg-right { flex: 1; display: flex; flex-direction: column; }
.thread { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 10px; }
.bubble-row { display: flex; flex-direction: column; }
.bubble-row.self { align-items: flex-end; }
.bubble-row.other { align-items: flex-start; }
.bubble { max-width: 75%; padding: 8px 12px; border-radius: 10px; font-size: 13px; line-height: 1.5; }
.bubble.self { background: var(--el-color-primary); color: #fff; border-top-right-radius: 2px; }
.bubble.other { background: var(--el-fill-color); border-top-left-radius: 2px; }
.b-time { font-size: 11px; color: var(--el-text-color-secondary); margin-top: 2px; }
.mb8 { margin-bottom: 8px; }
</style>
