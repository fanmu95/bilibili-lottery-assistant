<template>
  <div class="page">
    <el-card shadow="never">
      <el-tabs v-model="tab">
        <!-- ==================== LLM 配置 ==================== -->
        <el-tab-pane label="大模型配置" name="llm">
          <div class="tab-wrap">
            <el-alert type="info" :closable="false" show-icon class="mb16"
              title="用于解析转发抽奖正文（奖品、开奖时间），增强活动识别。兼容 OpenAI 格式接口，推荐 DeepSeek-V4-Flash。" />

            <el-form label-width="140px" style="max-width: 720px">
              <el-form-item label="启用 LLM 识别">
                <el-switch v-model="form.llm_enabled" active-text="启用" inactive-text="关闭" />
              </el-form-item>
              <el-form-item label="接口地址 (Base URL)">
                <el-input v-model="form.llm_base_url" placeholder="https://api.deepseek.com/v1">
                  <template #append>以 /v1 结尾</template>
                </el-input>
              </el-form-item>
              <el-form-item label="API Key">
                <el-input v-model="form.llm_api_key" type="password" show-password placeholder="sk-..." autocomplete="off" />
              </el-form-item>
              <el-form-item label="模型">
                <div class="model-row">
                  <el-select v-model="form.llm_model" placeholder="点击「获取模型列表」后选择" style="width: 280px" filterable @change="onModelChange">
                    <el-option v-for="m in models" :key="m.id" :label="m.id" :value="m.id">
                      <span>{{ m.id }}</span>
                      <span class="dim" style="float: right; font-size: 12px">{{ m.owned_by }}</span>
                    </el-option>
                  </el-select>
                  <el-button :icon="Download" :loading="fetchingModels" @click="fetchModels">获取模型列表</el-button>
                  <el-button :icon="Connection" :loading="testing" @click="testLlm">测试连接</el-button>
                </div>
                <div class="model-msg" v-if="modelsMessage">{{ modelsMessage }}</div>
              </el-form-item>

              <el-divider content-position="left">当前模型的输入输出设置：{{ form.llm_model || '未选择模型' }}</el-divider>

              <el-form-item label="温度 Temperature">
                <div class="slider-wrap">
                  <el-slider v-model="override.temperature" :min="0" :max="2" :step="0.1" show-input />
                  <div class="hint">越低越稳定，识别类任务建议 0.1~0.3</div>
                </div>
              </el-form-item>
              <el-form-item label="Top P">
                <div class="slider-wrap">
                  <el-slider v-model="override.top_p" :min="0" :max="1" :step="0.05" show-input />
                </div>
              </el-form-item>
              <el-form-item label="最大输出 Tokens">
                <el-input-number v-model="override.max_tokens" :min="64" :max="131072" :step="256"
                  controls-position="right" style="width: 200px" />
                <span class="hint" style="margin-left: 8px">输出长度上限，最大支持 128k（131072）</span>
              </el-form-item>
              <el-form-item label="系统提示词（输入）">
                <el-input v-model="override.system_prompt" type="textarea" :rows="4"
                  placeholder="抽奖识别指令，默认已内置" />
              </el-form-item>
              <el-form-item>
                <el-button type="primary" :icon="Setting" @click="saveModelOverride">保存当前模型参数</el-button>
                <el-button @click="resetModelOverride">恢复默认</el-button>
              </el-form-item>
              <el-form-item>
                <el-button type="primary" :icon="Check" @click="save">保存全部设置</el-button>
              </el-form-item>
            </el-form>
          </div>
        </el-tab-pane>

        <!-- ==================== 扫描与参与 ==================== -->
        <el-tab-pane label="扫描与参与" name="scan">
          <div class="tab-wrap">
            <el-form label-width="180px" style="max-width: 720px">
              <el-divider content-position="left">活动发现</el-divider>
              <el-form-item label="自动扫描间隔（分钟）">
                <el-input-number v-model="form.scan_interval" :min="5" :max="1440" />
              </el-form-item>
              <el-form-item label="监控回溯时间（天）">
                <el-input-number v-model="form.watch_backfill_days" :min="1" :max="90" controls-position="right" style="width: 160px" />
                <span class="hint" style="margin-left: 8px">整数型。扫描监控用户时，只识别最近 N 天内转发的抽奖动态（默认 10，最大 90）</span>
              </el-form-item>
              <el-form-item label="扫描时 LLM 增强识别">
                <el-switch v-model="form.scan_llm_verify" />
                <span class="hint" style="margin-left: 8px">需要在上方配置并启用 LLM</span>
              </el-form-item>

              <el-divider content-position="left">参与设置</el-divider>
              <el-form-item label="参与文案模式">
                <el-radio-group v-model="form.participate_text_mode">
                  <el-radio value="custom">自定义文案</el-radio>
                  <el-radio value="random_comment">借用随机评论</el-radio>
                  <el-radio value="llm_generate">LLM 生成贴合评论</el-radio>
                  <el-radio value="random">随机混合</el-radio>
                </el-radio-group>
                <div class="hint" style="margin-left: 8px">随机混合：每次随机从【真实评论 / LLM 贴合评论 / 内置自然文案】中挑一种，避免千篇一律</div>
              </el-form-item>
              <el-form-item v-if="form.participate_text_mode === 'custom'" label="参与文案">
                <el-input v-model="form.participate_text" type="textarea" :rows="2"
                  placeholder="自定义模式下转发/评论时使用的内容" />
              </el-form-item>
              <el-form-item v-else label="参与文案">
                <el-alert type="info" :closable="false" show-icon
                  title="当前模式不使用自定义文案"
                  :description="form.participate_text_mode === 'random_comment'
                    ? '将随机借用该动态评论区中真实用户的评论（跳过前 5 条热评）'
                    : form.participate_text_mode === 'random'
                      ? '随机混合：真实评论 / LLM 贴合评论 / 内置自然文案池随机挑一种，失败自动降级'
                      : '将根据活动正文，由 LLM 生成贴合内容的简短评论'" />
              </el-form-item>
              <el-form-item label="单批次参与数量">
                <el-input-number v-model="form.participate_batch" :min="1" :max="10" />
              </el-form-item>
              <el-form-item label="充电抽奖自动跳过">
                <el-switch v-model="form.skip_charge_lottery" />
              </el-form-item>

              <el-divider content-position="left">防风控</el-divider>
              <el-form-item label="每日参与上限">
                <el-input-number v-model="form.daily_participate_limit" :min="0" :max="500" controls-position="right" style="width: 160px" />
                <span class="hint" style="margin-left: 8px">每账号每日最多参与数（0=不限），超限自动暂停</span>
              </el-form-item>
              <el-form-item label="动作间隔（秒）">
                <el-input-number v-model="form.action_interval_min" :min="0.5" :max="10" :step="0.5" controls-position="right" style="width: 120px" />
                <span class="hint" style="margin: 0 6px">至</span>
                <el-input-number v-model="form.action_interval_max" :min="0.5" :max="20" :step="0.5" controls-position="right" style="width: 120px" />
                <span class="hint" style="margin-left: 8px">点赞/关注/转发/评论之间随机抖动</span>
              </el-form-item>
              <el-form-item label="活动间隔（秒）">
                <el-input-number v-model="form.activity_gap_min" :min="0.5" :max="30" :step="0.5" controls-position="right" style="width: 120px" />
                <span class="hint" style="margin: 0 6px">至</span>
                <el-input-number v-model="form.activity_gap_max" :min="0.5" :max="60" :step="0.5" controls-position="right" style="width: 120px" />
                <span class="hint" style="margin-left: 8px">参与完一个活动到下一个的随机间隔</span>
              </el-form-item>
              <el-form-item label="轮次间隔（秒）">
                <el-input-number v-model="form.auto_round_sleep" :min="10" :max="3600" :step="10" controls-position="right" style="width: 160px" />
                <span class="hint" style="margin-left: 8px">全自动每轮循环间隔（最小 10s）</span>
              </el-form-item>
              <el-form-item label="请求速率（RPS）">
                <el-input-number v-model="form.bili_rps" :min="0.5" :max="10" :step="0.5" controls-position="right" style="width: 160px" />
                <span class="hint" style="margin-left: 8px">全局 B 站请求限流（越低越安全）</span>
              </el-form-item>

              <el-divider content-position="left">私信消息检测（白名单）</el-divider>
              <el-form-item label="检测间隔（分钟）">
                <el-input-number v-model="form.dm_check_interval_min" :min="5" :max="1440" controls-position="right" style="width: 160px" />
                <span class="hint" style="margin-left: 8px">默认 30 分钟检查一次新私信/回复</span>
              </el-form-item>
              <el-form-item label="白名单时间段">
                <el-time-picker v-model="dmCheckStart" format="HH:mm" value-format="HH:mm" style="width: 140px" />
                <span class="hint" style="margin: 0 6px">至</span>
                <el-time-picker v-model="dmCheckEnd" format="HH:mm" value-format="HH:mm" style="width: 140px" />
                <span class="hint" style="margin-left: 8px">默认 08:00 ~ 22:00，白名单内按间隔检测，其余时间不检测</span>
              </el-form-item>

              <el-divider content-position="left">定时自动参与（对齐 bilibinggo 定时点击）</el-divider>
              <el-form-item label="启用定时调度">
                <el-switch v-model="form.auto_schedule_enabled" />
              </el-form-item>
              <el-form-item label="每日参与时间">
                <el-time-picker v-model="scheduleTime" format="HH:mm" value-format="HH:mm" />
              </el-form-item>
              <el-form-item>
                <el-button type="primary" :icon="Check" @click="save">保存设置</el-button>
              </el-form-item>
            </el-form>
          </div>
        </el-tab-pane>

        <!-- ==================== 关于 ==================== -->
        <el-tab-pane label="关于" name="about">
          <div class="tab-wrap">
            <el-descriptions :column="1" border style="max-width: 720px">
              <el-descriptions-item label="项目">B站自动化抽奖助手</el-descriptions-item>
              <el-descriptions-item label="版本">v1.0.0</el-descriptions-item>
              <el-descriptions-item label="技术栈">Vue 3 + Element Plus + Vite / FastAPI + SQLAlchemy + SQLite</el-descriptions-item>
              <el-descriptions-item label="接口契约">对齐 bilibinggo（luovicter-collab/bilibinggo）控制台 API</el-descriptions-item>
              <el-descriptions-item label="功能">
                <el-tag v-for="f in features" :key="f" size="small" class="mr4">{{ f }}</el-tag>
              </el-descriptions-item>
              <el-descriptions-item label="参考项目">
                <a href="https://github.com/luovicter-collab/bilibinggo" target="_blank">https://github.com/luovicter-collab/bilibinggo</a>
              </el-descriptions-item>
              <el-descriptions-item label="免责声明">
                本工具仅供学习研究，请遵守哔哩哔哩社区规范与相关法律法规，合理控制使用频率，避免对平台造成负担。
              </el-descriptions-item>
            </el-descriptions>
          </div>
        </el-tab-pane>
      </el-tabs>
    </el-card>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Check, Download, Connection, Setting } from '@element-plus/icons-vue'
import { settingApi } from '../api'

const tab = ref('llm')
const form = reactive({})
const scheduleTime = ref('10:00')
const dmCheckStart = ref('08:00')
const dmCheckEnd = ref('22:00')
const models = ref([])
const modelsMessage = ref('')
const fetchingModels = ref(false)
const testing = ref(false)
const override = reactive({ temperature: 0.7, top_p: 1.0, max_tokens: 1024, system_prompt: '' })

const features = ['多账号管理', '扫码登录', '私信回复查看', 'DM 计数', '@提及计数', '活动发现', '批量导入', '扫描进度', 'LLM 解析', '三连参与', '定时调度']

const DEFAULT_PROMPT = '你是哔哩哔哩抽奖活动识别助手。判断用户输入是否为抽奖活动，如果是，输出 JSON：{"is_lottery": true, "prize": "奖品", "winner_count": 0}；如果不是输出 {"is_lottery": false}。只输出 JSON。'

async function loadSettings() {
  const data = await settingApi.get()
  Object.assign(form, data)
  if (data.auto_schedule_time) scheduleTime.value = data.auto_schedule_time
  if (data.dm_check_start) dmCheckStart.value = data.dm_check_start
  if (data.dm_check_end) dmCheckEnd.value = data.dm_check_end
  // 加载当前模型的覆盖参数
  try {
    const overrides = JSON.parse(data.llm_model_overrides || '{}')
    if (data.llm_model && overrides[data.llm_model]) {
      Object.assign(override, overrides[data.llm_model])
    } else {
      Object.assign(override, { temperature: Number(data.llm_temperature) || 0.7, top_p: Number(data.llm_top_p) || 1, max_tokens: Number(data.llm_max_tokens) || 1024, system_prompt: data.llm_system_prompt || DEFAULT_PROMPT })
    }
  } catch (e) { /* ignore */ }
}

function onModelChange() {
  loadModelOverride()
}

async function loadModelOverride() {
  const data = await settingApi.get()
  try {
    const overrides = JSON.parse(data.llm_model_overrides || '{}')
    if (form.llm_model && overrides[form.llm_model]) {
      Object.assign(override, overrides[form.llm_model])
    } else {
      Object.assign(override, { temperature: Number(data.llm_temperature) || 0.7, top_p: Number(data.llm_top_p) || 1, max_tokens: Number(data.llm_max_tokens) || 1024, system_prompt: data.llm_system_prompt || DEFAULT_PROMPT })
    }
  } catch (e) { /* ignore */ }
}

async function fetchModels() {
  if (!form.llm_base_url) return ElMessage.warning('请先填写接口地址')
  fetchingModels.value = true
  modelsMessage.value = ''
  try {
    const res = await settingApi.fetchModels({ base_url: form.llm_base_url, api_key: form.llm_api_key })
    if (res.ok) {
      models.value = res.models
      modelsMessage.value = `获取到 ${res.models.length} 个模型` + (res.message || '')
      if (res.models.length && !form.llm_model) form.llm_model = res.models[0].id
      ElMessage.success(`获取到 ${res.models.length} 个模型`)
      loadModelOverride()
    } else {
      modelsMessage.value = res.message || '获取失败'
      ElMessage.error(res.message || '获取模型列表失败')
    }
  } finally { fetchingModels.value = false }
}

async function testLlm() {
  if (!form.llm_base_url || !form.llm_model) return ElMessage.warning('请先填写接口地址并选择模型')
  testing.value = true
  try {
    const res = await settingApi.testLlm({
      base_url: form.llm_base_url,
      api_key: form.llm_api_key,
      model: form.llm_model,
      temperature: override.temperature,
      max_tokens: override.max_tokens,
      top_p: override.top_p,
      system_prompt: override.system_prompt,
      message: '你好，请回复：连接成功',
    })
    if (res.ok) ElMessage.success(`连接成功：${res.reply?.slice(0, 60)}`)
    else ElMessage.error(res.message || '连接失败')
  } finally { testing.value = false }
}

async function saveModelOverride() {
  if (!form.llm_model) return ElMessage.warning('请先选择模型')
  const data = await settingApi.get()
  let overrides = {}
  try { overrides = JSON.parse(data.llm_model_overrides || '{}') } catch (e) { overrides = {} }
  overrides[form.llm_model] = {
    temperature: override.temperature,
    top_p: override.top_p,
    max_tokens: override.max_tokens,
    system_prompt: override.system_prompt || DEFAULT_PROMPT,
  }
  await settingApi.save({ llm_model_overrides: JSON.stringify(overrides) })
  ElMessage.success(`已保存模型 ${form.llm_model} 的输入输出参数`)
}

function resetModelOverride() {
  Object.assign(override, { temperature: 0.7, top_p: 1.0, max_tokens: 1024, system_prompt: DEFAULT_PROMPT })
}

async function save() {
  const payload = {
    llm_enabled: form.llm_enabled,
    llm_base_url: form.llm_base_url,
    llm_api_key: form.llm_api_key,
    llm_model: form.llm_model,
    llm_temperature: override.temperature,
    llm_top_p: override.top_p,
    llm_max_tokens: override.max_tokens,
    llm_system_prompt: override.system_prompt || DEFAULT_PROMPT,
    scan_interval: form.scan_interval,
    scan_llm_verify: form.scan_llm_verify,
    watch_backfill_days: form.watch_backfill_days,
    participate_text: form.participate_text,
    participate_text_mode: form.participate_text_mode,
    participate_batch: form.participate_batch,
    skip_charge_lottery: form.skip_charge_lottery,
    daily_participate_limit: form.daily_participate_limit,
    action_interval_min: form.action_interval_min,
    action_interval_max: form.action_interval_max,
    activity_gap_min: form.activity_gap_min,
    activity_gap_max: form.activity_gap_max,
    auto_round_sleep: form.auto_round_sleep,
    bili_rps: form.bili_rps,
    auto_schedule_enabled: form.auto_schedule_enabled,
    auto_schedule_time: scheduleTime.value || '10:00',
    dm_check_interval_min: form.dm_check_interval_min,
    dm_check_start: dmCheckStart.value || '08:00',
    dm_check_end: dmCheckEnd.value || '22:00',
  }
  await settingApi.save(payload)
  ElMessage.success('设置已保存')
}

onMounted(loadSettings)
</script>

<style scoped>
.tab-wrap { padding-top: 8px; }
.mb16 { margin-bottom: 16px; }
.model-row { display: flex; align-items: center; gap: 8px; width: 100%; }
.model-msg { font-size: 12px; color: var(--el-text-color-secondary); margin-top: 6px; }
.slider-wrap { width: 100%; }
.hint { font-size: 12px; color: var(--el-text-color-secondary); }
.dim { color: var(--el-text-color-secondary); }
.mr4 { margin-right: 4px; }
</style>
