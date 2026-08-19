// B 站官方表情渲染：把 [doge] 等文本代码替换为官方表情图片
import http from '../api'

let emoteMap = {}
let loaded = false
let loadingPromise = null

// 拉取表情映射（全局缓存；force 强制刷新）
export function loadEmotes(force = false) {
  if (loaded && !force) return Promise.resolve()
  if (loadingPromise) return loadingPromise
  loadingPromise = http.get('/emotes')
    .then(res => {
      emoteMap = (res && res.emotes) || {}
    })
    .catch(() => { emoteMap = {} })
    .finally(() => { loaded = true; loadingPromise = null })
  return loadingPromise
}

// 文本 → HTML（先转义 HTML 防 XSS，再把 [表情] 替换为图片）
export function emoteHtml(text) {
  if (!text) return ''
  let s = String(text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;')
  if (Object.keys(emoteMap).length) {
    s = s.replace(/\[[^\]]+\]/g, m => {
      const url = emoteMap[m]
      if (!url) return m
      return `<img class="bili-emote" src="${url}" alt="${m}" loading="lazy">`
    })
  }
  return s
}
