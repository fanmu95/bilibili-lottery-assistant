# 自动抽奖助手

多账号自动参与 平台抽奖动态的一体化工具：监控用户扫描 → LLM 深度解析 → 两阶段复核 → 自动三连参与 → 私信检测回复。

- **后端**：FastAPI + SQLite（单文件库，配置/账号/活动全在一个 db）
- **前端**：Vue3 + Element Plus（浏览器访问）
- **部署**：Windows exe（免安装双击运行） / Docker 镜像（NAS/服务器，amd64+arm64）
- **CI**：GitHub Actions 自动构建 exe 与镜像（push 即触发）

---

## 一、快速开始

### 方式 A：Windows exe（桌面，最简单）

1. **下载**：仓库 **Releases** 页 → 最新版 → `lottery-assistant-win64.zip`
2. **运行**：解压 → 双击 `抽奖助手.exe` → 自动打开浏览器 `http://127.0.0.1:8000`（端口被占用自动换 8001~8019）
3. **数据目录**：exe 旁 `data/` 文件夹（备份 = 拷它；迁移 = 整个文件夹拷走）

### 方式 B：Docker（NAS / Linux 服务器）

镜像已公开拉取（ghcr.io，免登录，amd64/arm64 自动匹配）。推荐 compose：

```yaml
# docker-compose.yml
services:
  lottery-app:
    image: ghcr.io/fanmu95/lottery-assistant:latest
    container_name: lottery-app
    ports:
      - "8000:8000"          # 左边宿主机端口可改，右边必须是 8000
    volumes:
      - /volume1/Docker/lottery-app/data:/app/data   # 数据持久化，升级不丢
    restart: unless-stopped
```

```bash
docker compose up -d        # 启动
docker compose pull && docker compose up -d   # 升级
# 访问 http://<NAS_IP>:8000
```

> ⚠️ 数据卷 `/app/data` **必须挂载**——账号 cookies/配置/活动都在这，不挂载=容器删除全丢。

---

## 二、界面功能（四个页面）

| 页面 | 功能 |
|---|---|
| **活动列表** | 全量活动表格（标题/奖品/结束时间/参与状态/参与文案），顶部统计卡片（总数/待参与/已参与/已结束/**待复核**），支持筛选、手动参与、批量操作 |
| **活动发现** | 监控用户管理（添加/批量导入/**导出**/扫描/职业号发现），全量扫描进度条，**自动扫描开关** |
| **账号管理** | 多账号扫码登录、私信回复、评论/艾特处理、**每行导出 Cookie**、刷新/重登/删除 |
| **设置** | 全部参数配置（详见下文） |

---

## 三、设置项详解

> 全部设置持久化保存，改完点「保存」立即生效（自动模式每轮重新读取）。

### 1️⃣ LLM 大模型（智能解析核心）

| 设置项 | 说明 | 默认 |
|---|---|---|
| `llm_enabled` | 总开关：启用 LLM 深度解析/复核/评论生成 | 关 |
| `llm_base_url` | API 地址（OpenAI 兼容格式），如 `https://api.deepseek.com/v1` | deepseek |
| `llm_api_key` | 密钥（**仅本地存储，永不随代码上传**） | 空 |
| `llm_model` | 模型名，如 `deepseek-chat` / `qwen-plus` | 空 |
| `llm_temperature` | 生成随机性（0=稳定，1=发散；解析建议 0.1~0.3） | 0.7 |
| `llm_max_tokens` | 最大输出；**未填时按模型自动最大化**（deepseek/sensenova→65536 等） | 自动 |
| `llm_top_p` | 核采样 | 1.0 |
| `llm_system_prompt` | 解析提示词（高级用户可自定义） | 内置 |
| `llm_model_overrides` | 按模型的参数覆盖（JSON：`{"模型名":{"temperature":0.1,...}}`） | 空 |

> **提示**：换模型无需手动调 max_tokens——系统按模型名自动匹配最大输出预算。

### 2️⃣ 参与文案（评论内容）

| 设置项 | 说明 |
|---|---|
| `participate_text_mode` | 评论模式：**custom** 固定文案 / **random_comment** 借用评论区真实评论 / **llm_generate** LLM 贴合正文生成 / **random** 随机混合（推荐） |
| `participate_text` | custom 模式下的固定评论内容 |

> 评论形态已拟人化：长短随机（短评"好运/抽我"+ 长评）、支持 平台表情代码（[doge] 等）、兜底文案池随机抽取。

### 3️⃣ 扫描与调度

| 设置项 | 说明 | 默认 |
|---|---|---|
| `scan_interval` | 自动扫描间隔（分钟）——后端常驻定时批量扫描监控用户补货 | 60 |
| `auto_scan_enabled` | **自动扫描开关**（活动发现页也有此开关） | 开 |
| `scan_llm_verify` | 扫描时是否用 LLM 增强识别（更准但更慢） | 关 |
| `watch_backfill_days` | 扫描监控用户时的动态回溯天数 | 10 |
| `auto_schedule_enabled` | 定时自动启动全自动（如每天 08:20 自动开跑） | 关 |
| `auto_schedule_time` | 定时启动时刻 | 10:00 |
| `review_interval_min` | **后台复核间隔**（分钟）——后端启动常驻，独立自动修正奖品/时间/识别合集 | 5 |
| `auto_pro_scan_enabled` | **自动模式开关**：轮次冷却间隙是否执行职业号发现（错峰） | 开 |
| `monitor_empty_scan_remove` | 监控用户连续 N 次扫描无活动 → 自动标记失效剔除；0=不启用 | 3 |

### 4️⃣ 参与与防风控

| 设置项 | 说明 | 默认 |
|---|---|---|
| `participate_batch` | 每轮参与活动数 | 3 |
| `skip_charge_lottery` | 充电抽奖自动跳过 | 开 |
| `daily_participate_limit` | **每日参与配额**（全账号合计；满额后暂停参与等 0 点恢复） | 100 |
| `action_interval_min/max` | 三连每步动作间隔（秒，随机抖动） | 1.5~3 |
| `activity_gap_min/max` | 每个活动参与完的间隔（秒） | 3~5 |
| `auto_round_sleep` | **轮次间隔**（秒）——每轮参与完的等待；待参与<10 时自动跳过 | 60 |
| `bili_rps` | 平台接口全局限流速率（请求/秒） | 3 |

> **提速技巧**：想快点跑完配额 → 调大 `participate_batch`、调小 `auto_round_sleep`（如 120）。注意别太激进（防风控）。

### 5️⃣ 私信检测

| 设置项 | 说明 | 默认 |
|---|---|---|
| `dm_check_interval_min` | 私信检测间隔（分钟）——自动已读+自动回复 | 30 |
| `dm_check_start/end` | 检测时间窗（如 08:00~22:00，夜间不打扰） | 08:00~22:00 |

---

## 四、自动模式工作机制

```
一轮循环：
  ① 清理已过期活动 → 统计待参与
  ② 配额检查：今日已达上限 → 暂停参与（等 0 点）
  ③ 扫描决策：待参与 <10 → 扫描监控用户补货（<3 连扫 5 人 / 3~10 冷却扫 1 人）
  ④ 参与：每轮 participate_batch 个活动，按【最近开奖日期优先】排序，
          每个活动一轮内参与完所有未参与账号（点赞→关注→转发→评论）
  ⑤ 冷却等待（auto_round_sleep）→ 间隙可执行【职业号发现】（错峰，下轮自动暂停）
  ⑥ 每轮参与完触发【后台复核】线程（修正奖品/时间/识别合集，独立于全自动）
```

**排序规则**：未参与活动优先 → 有开奖日期的按**最近开奖升序** → 无日期活动排最后。

**已结束判定**：仅依据 ①end_time 过期 ②官方 lottery_notice 已开奖（不误杀长周期抽奖）。

---

## 五、数据与安全

- **所有数据在一个 SQLite 文件**（`bili_lottery.db`）：配置、账号 cookies、监控用户、活动、日志
- **备份**：拷 `data/` 目录（exe）或挂载卷目录（Docker）
- **Cookie = 账号钥匙**：绝不传到公开仓库；账号页可单账号导出 Cookie 用于迁移
- **LLM 密钥**：只在本地 db，永不入库 GitHub

---

## 六、常见问题

| 问题 | 解决 |
|---|---|
| exe 双击没反应 | 看 `data/app.log`；大概率端口被占已自动换端口 |
| 参与不按最近开奖 | 已修复（无日期活动排最后），用最新版本 |
| Docker 访问不了 | 检查端口映射（右边必须 8000）、防火墙放行 |
| 换机器迁移 | 拷贝 `data/`（exe）或挂载卷目录（Docker），登录态随行 |
| 想重新扫码 | 账号管理 → 重登 |

---

*版本 v0.1.0 · 构建：GitHub Actions 自动出 exe + Docker 镜像*
