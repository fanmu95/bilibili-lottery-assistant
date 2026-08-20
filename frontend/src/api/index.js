import axios from 'axios'
import { ElMessage } from 'element-plus'

const http = axios.create({ baseURL: '/api', timeout: 60000 })

http.interceptors.response.use(
  (res) => res.data,
  (err) => {
    const msg = err.response?.data?.detail || err.response?.data?.message || err.message || '请求失败'
    ElMessage.error(typeof msg === 'string' ? msg : '请求失败')
    return Promise.reject(err)
  }
)

// ---------------- 账号 ----------------
export const accountApi = {
  list: () => http.get('/accounts'),
  update: (id, data) => http.put(`/accounts/${id}`, data),
  remove: (id) => http.delete(`/accounts/${id}`),
  refresh: (id) => http.post(`/accounts/${id}/refresh`),
  genQr: (accountId) => http.post('/login/qrcode', accountId ? { account_id: accountId } : {}),
  pollQr: (key) => http.post('/login/poll', { qrcode_key: key }),
  unread: (id) => http.get(`/accounts/${id}/unread`),
  messages: (id) => http.get(`/accounts/${id}/messages`),
  messageThread: (id, talkerId) => http.get(`/accounts/${id}/messages/${talkerId}`),
  atMessages: (id) => http.get(`/accounts/${id}/messages/at`),
  replyMessages: (id) => http.get(`/accounts/${id}/messages/reply`),
  ackAtUnread: (id) => http.post(`/accounts/${id}/ack-at-unread`),
  ackReplyUnread: (id) => http.post(`/accounts/${id}/ack-reply-unread`),
  readSession: (id, talkerId, ackSeqno) => http.post(`/accounts/${id}/sessions/${talkerId}/read`, null, { params: { ack_seqno: ackSeqno || 0 } }),
  exportCookies: (id) => http.get(`/accounts/${id}/export-cookies`),
  importCookies: (data) => http.post('/accounts/import-cookies', data),
}

// ---------------- 监控用户（活动发现） ----------------
export const watchApi = {
  list: (params) => http.get('/watch-users', { params }),
  add: (data) => http.post('/watch-users', data),
  batchAdd: (data) => http.post('/watch-users/batch', data),
  update: (id, data) => http.put(`/watch-users/${id}`, data),
  remove: (id) => http.delete(`/watch-users/${id}`),
  scan: (id) => http.post(`/watch-users/${id}/scan`),
  batchDelete: (ids) => http.post('/watch-users/batch-delete', { ids }),
  exportList: () => http.get('/watch-users/export'),
}

// ---------------- 活动 ----------------
export const activityApi = {
  list: (params) => http.get('/activities', { params }),
  stats: () => http.get('/activities/stats'),
  get: (id) => http.get(`/activities/${id}`),
  create: (data) => http.post('/activities', data),
  update: (id, data) => http.put(`/activities/${id}`, data),
  remove: (id) => http.delete(`/activities/${id}`),
  participate: (id, accountId) => http.post(`/activities/${id}/participate`, { account_id: accountId || null }),
  participateProgress: (id) => http.get(`/activities/${id}/participate-progress`),
  participateCancel: (id) => http.post(`/activities/${id}/participate-cancel`),
  participateStatus: () => http.get('/activities/participate-status'),
  discoverPro: (id) => http.post(`/activities/${id}/discover-pro`),
  discoverProProgress: () => http.get('/activities/discover-pro/progress'),
  batchParticipate: (ids, accountId) => http.post('/activities/batch-participate', { activity_ids: ids, account_id: accountId || null }),
  batchDelete: (ids) => http.post('/activities/batch-delete', { ids }),
  participateTriple: () => http.post('/activities/participate-triple'),
  tripleTargets: () => http.get('/activities/triple-targets'),
  refreshStatus: () => http.post('/activities/refresh-status'),
}

// ---------------- 日志 ----------------
export const logApi = {
  list: (params) => http.get('/logs', { params }),
  modules: () => http.get('/logs/modules'),
  clear: () => http.delete('/logs'),
}

// ---------------- 设置 ----------------
export const settingApi = {
  get: () => http.get('/settings'),
  save: (settings) => http.put('/settings', { settings }),
  fetchModels: (data) => http.post('/settings/llm/models', data),
  testLlm: (data) => http.post('/settings/llm/test', data),
}

// ---------------- 扫描 ----------------
export const scanApi = {
  start: (userIds, reset = false) => http.post('/scan/start', { user_ids: userIds, reset }),
  stop: () => http.post('/scan/stop'),
  progress: () => http.get('/scan/progress'),
}

// ---------------- 全自动模式 ----------------
export const autoApi = {
  start: () => http.post('/auto/start'),
  stop: () => http.post('/auto/stop'),
  progress: () => http.get('/auto/progress'),
}

export const summaryApi = {
  get: () => http.get('/summary'),
}

// ---------------- 转发动态清理（已开奖未中奖删除） ----------------
export const cleanupApi = {
  preview: () => http.post('/cleanup/preview'),
  run: () => http.post('/cleanup/run'),
  accountDynamics: (id, data) => http.post(`/cleanup/accounts/${id}/dynamics`, data),
  accountProgress: (id) => http.get(`/cleanup/accounts/${id}/progress`),
}

// ---------------- 版本检查 / 自动更新 ----------------
export const updateApi = {
  check: () => http.get('/update/check'),
  version: () => http.get('/update/version'),
  download: () => http.get('/update/download'),
  progress: () => http.get('/update/progress'),
  apply: () => http.post('/update/apply'),
}

export default http
