"""哔哩哔哩 API 客户端

包含：二维码登录、扫码轮询、用户信息、空间动态抓取、抽奖识别、私信会话。
公开接口（详情/空间卡片/动态列表）优先用匿名 pub_session 不带 cookie，
仅参与动作（点赞/关注/转发/评论）与私信接口使用登录 session。
接口失败返回空/抛出，不回退演示数据。
"""
import json
import re
import time
import urllib.parse
from datetime import datetime, timedelta

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

BASE = "https://api.bilibili.com"
PASSPORT = "https://passport.bilibili.com"
VC = "https://api.vc.bilibili.com"

# 互动抽奖公告接口（对齐 bilibinggo）：business_type=1 互动抽奖，
# 返回结构化抽奖数据（各档奖品/人数/开奖时间/参与人数/中奖名单）
LOTTERY_NOTICE_URL = "https://api.vc.bilibili.com/lottery_svr/v1/lottery_svr/lottery_notice"

# 对齐 bilibinggo：从 opus 页 HTML 提取正文（API 被 -412/-352 风控时的兜底）
INITIAL_STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;", re.S)
MIN_CONTENT_LEN = 12      # HTML 正文最短长度，过短视为未提取成功

LOTTERY_KEYWORDS = [
    "抽奖", "转发抽奖", "转发关注", "关注转发", "转发送", "抽送", "抽一个",
    "中奖", "评论抽", "留言抽", "点赞抽", "福利", "送会员", "送大会员", "送b币",
    "随机抽", "抽三名", "抽两位", "抽三位", "关注+转发", "转发+点赞", "转发+关注",
    "抽5位", "抽3位", "抽2位", "抽10位", "抽一位", "抽一名",
]
PRIZE_KEYWORDS = [
    "iPhone", "苹果", "iPad", "耳机", "手办", "周边", "红包", "现金", "B币",
    "大会员", "游戏", "皮肤", "键盘", "鼠标", "显示器", "Switch", "PS5", "主机",
    "小米", "华为", "盲盒", "立牌", "徽章", "抱枕", "玩偶", "礼盒", "签名",
    "专辑", "门票", "蓝牙音箱", "音响", "手机", "平板", "美图", "画集", "徽章",
]

# ---------------------------------------------------------------------------
# 抽奖文本识别
# ---------------------------------------------------------------------------

def detect_lottery(text: str) -> bool:
    """判断文本是否包含抽奖意图"""
    if not text:
        return False
    low = text.lower()
    return any(kw.lower() in low for kw in LOTTERY_KEYWORDS)


def extract_prize(text: str) -> str:
    """从文本中提取奖品关键词"""
    if not text:
        return ""
    low = text.lower()
    found = [kw for kw in PRIZE_KEYWORDS if kw.lower() in low]
    return "、".join(dict.fromkeys(found))


def extract_end_time(text: str, publish_time: datetime) -> datetime | None:
    """从文案中解析开奖/结束时间；无法解析返回 None"""
    if not text:
        return None
    # 1) "N天后开奖/截止"
    m = re.search(r"(\d{1,3})\s*天(?:后|之后)?\s*(?:开奖|截止|结束)", text)
    if m:
        return publish_time + timedelta(days=int(m.group(1)))
    # 2) "x月x日开奖" / "x月x日"
    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日?\s*(?:开奖|截止|结束)?", text)
    if m:
        try:
            month, day = int(m.group(1)), int(m.group(2))
            year = publish_time.year
            try:
                dt = datetime(year, month, day)
            except ValueError:
                dt = datetime(year + 1, month, day)
            if dt < publish_time - timedelta(days=1):
                dt = datetime(year + 1, month, day)
            return dt
        except ValueError:
            pass
    # 3) ISO / 日期格式 2026-08-15、2026/8/15、2026年8月15日
    m = re.search(r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})\s*日?", text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------------------
# B 站客户端
# ---------------------------------------------------------------------------

# 私信对端用户名/头像缓存（talker_id -> (name, avatar)），避免每次列表都重复请求
_talker_cache: dict = {}


def normalize_avatar(url: str) -> str:
    """头像 URL 统一规范为 https（避免混合内容拦截 / 防盗链异常）"""
    if not url:
        return ""
    return url.replace("http://", "https://") if url.startswith("http://") else url


class BiliClient:
    def __init__(self, cookies: dict = None):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        if cookies:
            for k, v in cookies.items():
                self.session.cookies.set(k, v)
        # 匿名 session：只带 UA，不带任何账号 cookie。
        # 用于公开接口（动态详情等），避免高频请求把登录账号标记风控（-352）。
        self.pub_session = requests.Session()
        self.pub_session.headers.update({
            "User-Agent": UA,
            "Referer": "https://www.bilibili.com/",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        # 最近一次详情请求的风控码（-352 账号级 / -412 IP级），供调用方退避
        self.last_detail_risk = None
        # 浏览器指纹预热：先访问 B 站主页获取 buvid3/b_nut，
        # 避免匿名请求因缺指纹被风控（-412 request was banned）
        self._warmup_fingerprint()

    def _warmup_fingerprint(self) -> None:
        """对齐 bilibinggo：访问 B 站主页触发指纹 cookie（buvid3/b_nut）。

        无指纹的匿名 API 请求会被 B 站风控返回 -412；warmup 后自动获得。
        带登录态的 session 一般已有 buvid3（登录流程附带），只对 pub_session 做。
        """
        try:
            self.pub_session.get("https://www.bilibili.com", timeout=15)
        except Exception:
            pass
        if "buvid3" not in self.pub_session.cookies:
            import uuid as _uuid
            try:
                self.pub_session.cookies.set(
                    "buvid3", f"{_uuid.uuid4().hex}infoc", domain=".bilibili.com")
            except Exception:
                pass

    def _refresh_pub_fingerprint(self) -> None:
        """重建匿名 session 并重新 warmup，获得全新 buvid3 指纹。

        同一指纹连续请求过多会被 B 站累计标记（-412）；
        定期换新指纹可避免（对齐 bilibinggo 每条动态独立客户端实例的做法）。
        """
        try:
            self.pub_session.close()
        except Exception:
            pass
        self.pub_session = requests.Session()
        self.pub_session.headers.update({
            "User-Agent": UA,
            "Referer": "https://www.bilibili.com/",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        self._warmup_fingerprint()

    # ---------------- WBI 签名（对齐 bilibinggo，防 -352 风控） ----------------
    # B 站部分接口（nav/搜索等）要求 w_rid 签名，缺失会被风控。
    _wbi_img_key: str | None = None
    _wbi_sub_key: str | None = None

    @staticmethod
    def _wbi_mixin_key(img_key: str, sub_key: str) -> str:
        enc_tab = [
            46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
            33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
            26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
            20, 34, 44, 52,
        ]
        material = img_key + sub_key
        return "".join(material[i] for i in enc_tab)[:32]

    def _ensure_wbi_keys(self) -> None:
        """从 nav 接口获取 WBI 签名密钥（img_url/sub_url 文件名）"""
        if self._wbi_img_key and self._wbi_sub_key:
            return
        import re as _re
        import urllib.parse as _up
        try:
            r = self.session.get(f"{BASE}/x/web-interface/nav", timeout=10)
            d = r.json()
            wbi = (d.get("data") or {}).get("wbi_img") or {}
            img = wbi.get("img_url", "").rsplit("/", 1)[-1].split(".")[0]
            sub = wbi.get("sub_url", "").rsplit("/", 1)[-1].split(".")[0]
            if not img or not sub:
                return
            self._wbi_img_key, self._wbi_sub_key = img, sub
        except Exception:
            pass

    def wbi_sign(self, params: dict) -> dict:
        """给参数字典加 w_rid 签名（对齐 bilibinggo wbi_sign）"""
        import hashlib as _hashlib
        import re as _re
        import urllib.parse as _up
        if not self._wbi_img_key or not self._wbi_sub_key:
            self._ensure_wbi_keys()
        if not self._wbi_img_key or not self._wbi_sub_key:
            return params          # 拿不到密钥则原样返回（接口可能失败，但不再阻塞）
        signed = {k: _re.sub(r"[!'()*]", "", str(v)) for k, v in params.items()}
        signed["wts"] = int(time.time())
        query = _up.urlencode(sorted(signed.items()))
        mixin = self._wbi_mixin_key(self._wbi_img_key, self._wbi_sub_key)
        signed["w_rid"] = _hashlib.md5((query + mixin).encode()).hexdigest()
        return signed

    # ---------------- 二维码登录 ----------------

    def generate_qr(self) -> dict:
        r = self.session.get(f"{PASSPORT}/x/passport-login/web/qrcode/generate", timeout=10)
        data = r.json()["data"]
        return {"url": data["url"], "qrcode_key": data["qrcode_key"]}

    def poll_qr(self, qrcode_key: str) -> dict:
        """轮询扫码状态。

        注意：B 站 poll 接口是双层 code——
          外层 code==0 只代表请求本身成功；
          真正的扫码状态在 data.code：
            86101 未扫码 / 86090 已扫码待确认 / 86038 已过期 / 0 登录成功。
        返回的 code 字段统一为内层状态码，调用方据此判定。
        """
        r = self.session.get(
            f"{PASSPORT}/x/passport-login/web/qrcode/poll",
            params={"qrcode_key": qrcode_key}, timeout=10)
        d = r.json()
        data = d.get("data") or {}
        inner_code = data.get("code", d.get("code", -1))
        result = {
            "code": inner_code,
            "message": data.get("message", "") or d.get("message", ""),
        }
        if inner_code == 0:
            cookies = {c.name: c.value for c in self.session.cookies}
            # 兜底：从跳转 url 中提取关键 cookie（url 内是 URL 编码值，需解码）
            url = data.get("url", "") or ""
            for k in ("SESSDATA", "bili_jct", "DedeUserID"):
                m = re.search(k + r"=([^&]+)", url)
                if m and k not in cookies:
                    cookies[k] = urllib.parse.unquote(m.group(1))
            result["cookies"] = cookies
        return result

    # ---------------- 用户信息 ----------------

    def get_user_info(self) -> dict:
        """当前登录账号信息"""
        r = self.session.get(f"{BASE}/x/web-interface/nav", timeout=10)
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError(d.get("message", "未登录"))
        info = d["data"]
        return {
            "uid": str(info["mid"]),
            "username": info["uname"],
            "avatar": normalize_avatar(info.get("face", "")),
            "level": info["level_info"]["current_level"],
            "vip_status": info.get("vip_status", 0),
            "coins": info.get("money", 0),
        }

    def get_user_space(self, uid) -> dict:
        """任意用户的空间卡片信息（公开接口，优先匿名 pub_session 不带 cookie）"""
        # 公开接口默认用 pub_session（匿名），避免账号被标记风控；
        # 匿名失败（风控）时回退登录 session 兜底。
        try:
            r = self.pub_session.get(f"{BASE}/x/web-interface/card",
                                     params={"mid": uid, "photo": "false"},
                                     headers={"Referer": "https://www.bilibili.com/"},
                                     timeout=10)
            d = r.json()
            if d.get("code") != 0:
                raise RuntimeError(d.get("message", "获取用户信息失败"))
        except Exception:
            r = self.session.get(f"{BASE}/x/web-interface/card",
                                 params={"mid": uid, "photo": "false"}, timeout=10)
            d = r.json()
            if d.get("code") != 0:
                raise RuntimeError(d.get("message", "获取用户信息失败"))
        card = d["data"]["card"]
        lv = card.get("level_info", {})
        return {
            "uid": str(uid),
            "username": card.get("name", ""),
            "avatar": normalize_avatar(card.get("face", "")),
            "sign": card.get("sign", ""),
            "fans": card.get("fans", 0),
            "level": lv.get("current_level", 0) if isinstance(lv, dict) else 0,
        }

    # ---------------- 空间动态（抽奖识别） ----------------
    #
    # 对齐 bilibinggo 的"发现活动"流程：
    #   阶段1：扫描监控用户空间动态，只收集 FORWARD 转发原动态的 id（顶层 item.orig.id_str）
    #   阶段2：对每个原动态 id 调详情接口（dynamic/detail，失败回退 opus/detail）
    #          获取完整正文（feed 里的 DRAW 原动态正文常为空，详情接口才有）

    def get_space_dynamics(self, uid, username: str = "", source_type: str = "repost",
                           max_pages: int = 8, since_days: int = 10,
                           only_lottery: bool = True,
                           stop_callback=None) -> list:
        """抓取监控用户的抽奖活动（两阶段：收集动态 id -> 详情取正文）。

        stop_callback：可选回调，每页/每条处理前检查（True=请求停止，提前返回）。
        source_type=repost（默认，对齐 bilibinggo）：监控用户**转发**的抽奖活动，
        只收集 DYNAMIC_TYPE_FORWARD 的转发原动态 id（author 为原 UP 主）。
        source_type=publish：监控用户**自己发布**的抽奖活动，收集用户自己发布的
        图文/文字/视频/互动抽奖卡片等动态（排除转发）。

        时间窗口内去重收集；详情正文关键词初筛 + 官方 lottery_notice 探测后
        返回活动 dict 列表。only_lottery=False 返回全部并标注 is_lottery
        （用于职业抽奖号比例统计）。接口失败返回 []（不 mock）。
        """
        # ---- 阶段1：收集动态 id ----
        links = []       # [{dynamic_id, author_uid, author_name, pub_ts, repost_count}]
        seen = set()
        now_ts = time.time()
        lower = now_ts - since_days * 86400
        is_publish = str(source_type) == "publish"
        try:
            offset = ""
            for _ in range(max_pages):
                if stop_callback and stop_callback():
                    return []
                # WBI 签名（对齐 bilibinggo：B 站逐步要求 w_rid，带上有备无患）
                feed_params = self.wbi_sign({
                    "host_mid": uid, "offset": offset,
                    "timezone_offset": "-480", "platform": "web"})
                r = self.pub_session.get(
                    f"{BASE}/x/polymer/web-dynamic/v1/feed/space",
                    params=feed_params, timeout=10)
                d = r.json()
                if d.get("code") != 0:
                    # 匿名被风控（-412/-352）时回退登录 session（仅此场景带 cookie）
                    r = self.session.get(
                        f"{BASE}/x/polymer/web-dynamic/v1/feed/space",
                        params=feed_params, timeout=10)
                    d = r.json()
                    if d.get("code") != 0:
                        return []          # 风控/未登录 -> 无真实数据
                data = d.get("data", {})
                items = data.get("items", [])
                # 匿名接口偶发"空返回"（code=0 但 items 空，非风控码，B 站抖动）：
                # 用登录 session 重试一次，避免把用户动态误判为"无数据"
                if not items and self.session is not self.pub_session:
                    try:
                        r = self.session.get(
                            f"{BASE}/x/polymer/web-dynamic/v1/feed/space",
                            params=feed_params, timeout=10)
                        d = r.json()
                        data = d.get("data", {})
                        items = data.get("items", [])
                    except Exception:
                        items = []
                if not items:
                    break
                reached_older = False
                for item in items:
                    if stop_callback and stop_callback():
                        return []
                    if not isinstance(item, dict):
                        continue
                    itype = item.get("type") or ""
                    if is_publish:
                        # publish：只收集用户自己发布的动态（排除转发）
                        if itype == "DYNAMIC_TYPE_FORWARD":
                            continue
                        pub_ts = self._extract_feed_pub_ts(item)
                        if pub_ts is not None and pub_ts < lower:
                            reached_older = True
                            continue
                        oid = str(item.get("id_str") or "").strip()
                        if not oid or oid in seen:
                            continue
                        seen.add(oid)
                        mods = item.get("modules") or {}
                        oauthor = mods.get("module_author") or {}
                        ostats = mods.get("module_stat") or {}
                        links.append({
                            "dynamic_id": oid,
                            "author_uid": str(oauthor.get("mid", "")) or str(uid),
                            "author_name": oauthor.get("name", "") or username,
                            "pub_ts": pub_ts or int(now_ts),
                            "repost_count": ostats.get("forward", 0) or 0,
                        })
                    else:
                        # repost：只收集转发的原动态 id
                        if itype != "DYNAMIC_TYPE_FORWARD":
                            continue
                        pub_ts = self._extract_feed_pub_ts(item)
                        if pub_ts is not None and pub_ts < lower:
                            reached_older = True
                            continue
                        orig = item.get("orig") or {}
                        oid = str(orig.get("id_str") or "").strip()
                        if not oid or oid in seen:
                            continue
                        seen.add(oid)
                        orig_mods = orig.get("modules") or {}
                        oauthor = orig_mods.get("module_author") or {}
                        ostats = orig_mods.get("module_stat") or {}
                        links.append({
                            "dynamic_id": oid,
                            "author_uid": str(oauthor.get("mid", "")) or str(uid),
                            "author_name": oauthor.get("name", "") or username,
                            "pub_ts": pub_ts or int(now_ts),
                            "repost_count": ostats.get("forward", 0) or 0,
                        })
                if reached_older:
                    break               # 已到时间窗口边界，不再翻更早的页
                if not data.get("has_more"):
                    break
                offset = data.get("offset", "")
        except Exception:
            return []                    # 网络异常 -> 无真实数据，不 mock
        if not links:
            return []

        # ---- 阶段2：详情取正文 + 关键词初筛 ----
        out = []
        consecutive_risk = 0      # 连续风控次数，超限提前结束（避免无限等待）
        fingerprint_refresh = 0   # 每 30 条重建匿名指纹（对齐 bilibinggo 防累计标记）
        for idx, link in enumerate(links):
            # 详情接口匿名请求：0.8s 间隔防 IP 级风控(-412)
            if idx > 0:
                time.sleep(0.8)
            # 定期换新指纹（新 buvid3），避免同一指纹累计请求被标记
            fingerprint_refresh += 1
            if fingerprint_refresh >= 30:
                fingerprint_refresh = 0
                self._refresh_pub_fingerprint()
            detail = self.get_dynamic_detail(link["dynamic_id"])
            text = self._extract_detail_text(detail) if detail else ""
            if not text or len(text.strip()) < MIN_CONTENT_LEN:
                # API 无果（风控 / 正文为空）→ HTML 兜底（对齐 bilibinggo：
                # 抓 opus 页面 window.__INITIAL_STATE__ 提取正文）
                html_text = self._fetch_html_dynamic_text(link["dynamic_id"])
                if len(html_text.strip()) > len(text.strip()):
                    text = html_text
            if not text:
                if self.last_detail_risk is not None:
                    # 命中风控（-412 IP级 / -352 账号级）：换新指纹退避后重试同一条
                    consecutive_risk += 1
                    if consecutive_risk >= 3:
                        break                      # 持续风控，放弃剩余（保留已收集）
                    self._refresh_pub_fingerprint()
                    time.sleep(60)
                    detail = self.get_dynamic_detail(link["dynamic_id"])
                    if detail:
                        text = self._extract_detail_text(detail)
                        if not text:
                            html_text = self._fetch_html_dynamic_text(link["dynamic_id"])
                            if len(html_text.strip()) > 0:
                                text = html_text
                    if not text:
                        continue                   # 重试仍风控/无正文，跳过
                else:
                    consecutive_risk = 0
                    continue
            else:
                consecutive_risk = 0
            # 互动抽奖探测（对齐 bilibinggo）：lottery_notice(business_type=1)
            # 官方结构化数据（奖品/时间/人数/中奖名单），比正文+LLM 更权威。
            # 注意：正文无抽奖关键词但存在互动抽奖卡片（如卡特亚图文动态），
            # notice 命中即视为抽奖，即使 detect_lottery 未命中。
            notice = self.get_lottery_notice(link["dynamic_id"])
            is_lottery = bool(notice) or bool(text and detect_lottery(text))
            if only_lottery and not is_lottery:
                continue
            pub = datetime.fromtimestamp(link["pub_ts"]) if link["pub_ts"] else datetime.now()
            # 字段优先级：notice（官方结构化）> 正文关键词提取
            prize_info = (BiliClient.format_notice_prizes(notice)
                          if notice else extract_prize(text))
            end_time = (BiliClient.notice_end_time(notice)
                        if notice else extract_end_time(text, pub))
            out.append({
                "activity_id": link["dynamic_id"],
                "title": (text[:80].replace("\n", " ") if text else prize_info[:60]
                          or "转发动态"),
                "desc": text or prize_info,
                # 对齐 bilibinggo：opus 链接才是 B 站当前有效的动态地址（t.bilibili.com 已失效 404）
                "link": f"https://www.bilibili.com/opus/{link['dynamic_id']}",
                "author_uid": link["author_uid"],
                "author_name": link["author_name"],
                "source_uid": str(uid),
                "source_name": username,
                "source_type": source_type,
                "publish_time": pub,
                "end_time": end_time,
                "repost_count": link["repost_count"],
                "prize_info": prize_info,
                "winner_count": (BiliClient.notice_winner_count(notice)
                                 if notice else 0),
                "participants": int(notice.get("participants") or 0) if notice else 0,
                "lottery_id": str(notice.get("lottery_id")) if notice else "",
                "notice": notice,
                "is_lottery": is_lottery,
            })
        return out[:60]

    def find_my_repost_id(self, uid: str, source_dynamic_id: str,
                          max_pages: int = 4, gap: float = 0.0) -> str:
        """在自己空间动态里查找「转发了指定源动态」的转发动态 id。

        场景：清理已开奖未中奖的转发时，删除接口需要**转发动态自己的 id**
        （rm_dynamic），而库里存的是源动态 id（activity_id）。
        gap：翻页间隔秒数（防风控）。
        返回转发动态 id；找不到返回空字符串。
        """
        try:
            offset = ""
            for _i in range(max_pages):
                if gap and _i > 0:
                    time.sleep(gap)
                params = self.wbi_sign({
                    "host_mid": uid, "offset": offset,
                    "timezone_offset": "-480", "platform": "web"})
                d = None
                for _retry in range(3):
                    r = self.session.get(
                        f"{BASE}/x/polymer/web-dynamic/v1/feed/space",
                        params=params, timeout=10)
                    d = r.json()
                    if d.get("code") == 0:
                        break
                    if d.get("code") in (-352, -412):
                        time.sleep(60)
                    else:
                        break
                if not d or d.get("code") != 0:
                    break
                data = d.get("data") or {}
                items = data.get("items") or []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if (item.get("type") == "DYNAMIC_TYPE_FORWARD"
                            and str((item.get("orig") or {}).get("id_str") or "")
                            == str(source_dynamic_id)):
                        rid = str(item.get("id_str") or "")
                        if rid:
                            return rid
                if not data.get("has_more"):
                    break
                offset = data.get("offset", "")
            return ""
        except Exception:
            return ""

    def scan_my_forwards(self, uid: str, max_pages: int = 100, gap: float = 0.0,
                         on_page=None) -> list:
        """扫描自己空间里的全部转发动态。

        返回 [{repost_id(转发动态id), orig_id(源动态id), pub_ts}]。
        供账号维度清理：检查账号转发过的抽奖动态，已开奖未中奖的删除。
        默认翻 100 页（约 1200 条），覆盖长历史账号。
        on_page：每翻一页回调（参数为当前页数，用于进度展示）。
        """
        out = []
        seen = set()
        try:
            offset = ""
            for _pi in range(max_pages):
                if gap and _pi > 0:
                    time.sleep(gap)
                if on_page:
                    try:
                        on_page(_pi + 1)
                    except Exception:
                        pass
                params = self.wbi_sign({
                    "host_mid": uid, "offset": offset,
                    "timezone_offset": "-480", "platform": "web"})
                d = None
                # 风控（-352/-412）冷却重试：等待 60s 重试当前页（最多 2 次）
                for _retry in range(3):
                    r = self.session.get(
                        f"{BASE}/x/polymer/web-dynamic/v1/feed/space",
                        params=params, timeout=10)
                    d = r.json()
                    if d.get("code") == 0:
                        break
                    if d.get("code") in (-352, -412):
                        time.sleep(60)
                    else:
                        break
                if not d or d.get("code") != 0:
                    break
                data = d.get("data") or {}
                items = data.get("items") or []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") != "DYNAMIC_TYPE_FORWARD":
                        continue
                    rid = str(item.get("id_str") or "")
                    orig_id = str((item.get("orig") or {}).get("id_str") or "")
                    if not rid or rid in seen:
                        continue
                    seen.add(rid)
                    out.append({
                        "repost_id": rid, "orig_id": orig_id,
                        "pub_ts": self._extract_feed_pub_ts(item) or 0})
                if not data.get("has_more"):
                    break
                offset = data.get("offset", "")
            return out
        except Exception:
            return out

    @staticmethod
    def extract_reserve(detail: dict) -> dict | None:
        """从动态详情提取「预约」信息（直播预约/视频预约）。

        存在 reserve 结构即预约类动态，返回 {rid, dynamic_id_str, reserve_total,
        title, button_status}；无预约返回 None。
        """
        try:
            additional = (((detail or {}).get("modules") or {})
                          .get("module_dynamic") or {}).get("additional") or {}
            reserve = additional.get("reserve") or {}
            rid = reserve.get("rid")
            if not rid:
                return None
            button = reserve.get("button") or {}
            return {
                "rid": str(rid),
                "dynamic_id_str": str((detail or {}).get("id_str") or ""),
                "reserve_total": int(reserve.get("reserve_total") or 0),
                "title": str(reserve.get("title") or "")[:80],
                "button_status": int(button.get("status") or 0),
            }
        except Exception:
            return None

    def reserve_live(self, rid: str, dynamic_id_str: str = "",
                     reserve_total: int = 0) -> dict:
        """点击动态内「预约」按钮（直播预约/视频预约，预约即参与抽奖）。

        实测接口：POST https://api.bilibili.com/x/dynamic/feed/reserve/click?csrf=
        JSON body: {reserve_id, cur_btn_status:1(预约), dynamic_id_str, reserve_total, spmid:""}
        返回 {"ok": bool, "message": str, "code": int}
        """
        try:
            csrf = ""
            for ck in self.session.cookies:
                if ck.name == "bili_jct":
                    csrf = ck.value or ""
                    break
            if not csrf:
                return {"ok": False, "message": "未登录或缺少 csrf（bili_jct）"}
            r = self.session.post(
                "https://api.bilibili.com/x/dynamic/feed/reserve/click",
                params={"csrf": csrf},
                json={
                    "reserve_id": int(rid or 0),
                    "cur_btn_status": 1,
                    "dynamic_id_str": str(dynamic_id_str or ""),
                    "reserve_total": int(reserve_total or 0),
                    "spmid": "",
                },
                headers={"Referer": "https://www.bilibili.com/"},
                timeout=12)
            d = r.json()
            if d.get("code") == 0:
                toast = ((d.get("data") or {}).get("toast")) or "预约成功"
                return {"ok": True, "message": str(toast), "code": 0}
            return {"ok": False,
                    "message": f"{d.get('code')}: {str(d.get('message'))[:60]}",
                    "code": d.get("code")}
        except Exception as e:
            return {"ok": False, "message": str(e)[:80]}

    def delete_dynamic(self, dynamic_id: str) -> bool:
        """删除自己的一条动态/转发（rm_dynamic，需登录态 + csrf）。

        返回是否删除成功（code==0）。
        """
        try:
            csrf = ""
            for ck in self.session.cookies:
                if ck.name == "bili_jct":
                    csrf = ck.value or ""
                    break
            r = self.session.post(
                "https://api.vc.bilibili.com/dynamic_svr/v1/dynamic_svr/rm_dynamic",
                data={"dynamic_id": str(dynamic_id), "csrf": csrf},
                timeout=12)
            d = r.json()
            return d.get("code") == 0
        except Exception:
            return False

    @staticmethod
    def _extract_feed_pub_ts(item: dict) -> int | None:
        """提取 feed 条目发布时间（modules.module_author.pub_ts），对齐 bilibinggo"""
        modules = item.get("modules")
        pub_ts = None
        if isinstance(modules, dict):
            pub_ts = (modules.get("module_author") or {}).get("pub_ts")
        elif isinstance(modules, list):
            for m in modules:
                if isinstance(m, dict) and m.get("module_author"):
                    pub_ts = m["module_author"].get("pub_ts")
                    break
        if pub_ts is None:
            return None
        try:
            val = int(pub_ts)
            return val if val > 0 else None
        except (TypeError, ValueError):
            return None

    def get_dynamic_detail(self, dynamic_id: str) -> dict | None:
        """获取动态详情：web-dynamic/v1/detail 优先，失败回退 opus/detail。

        对齐 bilibinggo：feed 里 FORWARD 原动态正文常为空，
        需用 id 单独调详情接口拿完整内容（opus/detail 含 module_title/module_content）。

        重要：动态详情是公开接口，使用匿名 pub_session（不带账号 cookie）请求，
        避免高频详情请求把登录账号标记风控（-352）。
        遇风控（-352/-412）时记录 self.last_detail_risk 供调用方退避，不再打账号 session。
        两路 API 均失败（风控/无正文）时返回 None，由调用方决定是否走 HTML 兜底。
        """
        self.last_detail_risk = None
        # 全局限流（令牌桶 3 RPS，与参与动作共享，防突发）
        try:
            from .rate_limit import acquire_bili_request_slot
            acquire_bili_request_slot()
        except Exception:
            pass
        # 匿名优先（不暴露账号身份）；带 opus Referer（对齐 bilibinggo，防 -412）
        referer = f"https://www.bilibili.com/opus/{dynamic_id}"
        try:
            r = self.pub_session.get(
                f"{BASE}/x/polymer/web-dynamic/v1/detail",
                params={"id": dynamic_id, "timezone_offset": "-480", "platform": "web"},
                headers={"Referer": referer},
                timeout=10)
            d = r.json()
            code = d.get("code")
            if code == 0:
                item = (d.get("data") or {}).get("item") or {}
                if item:
                    return item
            if code in (-352, -412):
                self.last_detail_risk = code
        except Exception:
            pass
        # 回退 opus/detail（同样匿名优先）
        try:
            r = self.pub_session.get(
                f"{BASE}/x/polymer/web-dynamic/v1/opus/detail",
                params={"id": dynamic_id,
                        "features": "htmlNewStyle,ugcDelete,editable,opusPrivateVisible"},
                headers={"Referer": referer},
                timeout=10)
            d = r.json()
            if d.get("code") == 0:
                item = (d.get("data") or {}).get("item") or {}
                if item:
                    return item
        except Exception:
            pass
        return None

    def get_lottery_notice(self, dynamic_id: str) -> dict | None:
        """探测动态是否为 B 站官方「互动抽奖」，返回结构化抽奖数据。

        对齐 bilibinggo：对候选动态调 lottery_notice(business_type=1)，
        有 lottery_id 即互动抽奖。返回字段（官方权威，比 LLM 解析可靠）：
          lottery_id / first_prize..third_prize(各档人数)
          first_prize_cmt..third_prize_cmt(各档奖品描述)
          lottery_time(unix 开奖时间) / participants(参与人数)
          status(0进行中/其他已开奖) / need_post(是否需发动态) / lottery_result(中奖名单)
        失败/非互动抽奖返回 None。匿名 pub_session 请求，不带账号 cookie。
        """
        try:
            r = self.pub_session.get(
                LOTTERY_NOTICE_URL,
                params={"business_id": dynamic_id, "business_type": 1},
                headers={"Referer": f"https://www.bilibili.com/opus/{dynamic_id}"},
                timeout=12)
            d = r.json()
            if d.get("code") == 0:
                notice = d.get("data") or {}
                if notice.get("lottery_id"):
                    return notice
        except Exception:
            pass
        return None

    @staticmethod
    def format_notice_prizes(notice: dict) -> str:
        """把互动抽奖 notice 的各档奖品格式化为可读文本：
        「648元红包×50」/「绯月曜耳机×1 + 周边×2」"""
        parts = []
        for key, cmt_key in (("first_prize", "first_prize_cmt"),
                             ("second_prize", "second_prize_cmt"),
                             ("third_prize", "third_prize_cmt")):
            try:
                count = int(notice.get(key) or 0)
            except (TypeError, ValueError):
                count = 0
            desc = str(notice.get(cmt_key) or "").strip()
            if desc:
                parts.append(f"{desc}×{count}" if count > 1 else desc)
            elif count > 0:
                parts.append(f"奖品×{count}")
        return " + ".join(parts)

    @staticmethod
    def notice_end_time(notice: dict):
        """互动抽奖 notice 的开奖时间（unix -> datetime），无效返回 None"""
        try:
            ts = int(notice.get("lottery_time") or 0)
            if ts > 0:
                return datetime.fromtimestamp(ts)
        except (TypeError, ValueError, OSError):
            pass
        return None

    @staticmethod
    def notice_is_ended(notice: dict) -> bool:
        """互动抽奖 notice 是否已开奖结束。

        官方字段：status=0 进行中 / 非 0 已开奖；
        lottery_result 有中奖名单 = 已开奖（最可靠）。
        """
        if not notice:
            return False
        try:
            if notice.get("lottery_result"):
                return True
            st = int(notice.get("status") or 0)
            return st != 0
        except (TypeError, ValueError):
            return False

    @staticmethod
    def notice_winner_count(notice: dict) -> int:
        """互动抽奖各档人数总和（中奖名额）"""
        total = 0
        for key in ("first_prize", "second_prize", "third_prize"):
            try:
                total += int(notice.get(key) or 0)
            except (TypeError, ValueError):
                pass
        return total

    def _fetch_html_dynamic_text(self, dynamic_id: str) -> str:
        """HTML 兜底：API 全被风控（-412/-352）时抓 opus 页面的正文。

        对齐 bilibinggo：抓取 https://www.bilibili.com/opus/{id} 页面，
        从 window.__INITIAL_STATE__ 中提取 detail.modules 里的正文
        （module_content.paragraphs[].text.nodes[].word.words 等）。
        仍用匿名 pub_session，不带账号 cookie。
        返回提取到的正文字符串；失败返回 ""（不抛异常，调用方继续退避）。
        """
        url = f"https://www.bilibili.com/opus/{dynamic_id}"
        try:
            r = self.pub_session.get(
                url,
                headers={"Referer": "https://www.bilibili.com/"},
                timeout=15)
            if r.status_code != 200:
                return ""
            text = BiliClient._extract_from_initial_state(r.text)
            return text.strip() if len(text.strip()) >= MIN_CONTENT_LEN else ""
        except Exception:
            return ""

    @staticmethod
    def _extract_from_initial_state(html: str) -> str:
        """从 opus 页 HTML 的 window.__INITIAL_STATE__ 提取动态正文。

        结构（实测瑞士篇）：
          state.detail.modules 为 list，
          module_content.paragraphs[].text.nodes[].word.words 存正文段落，
          module_title.text 存标题。
        兼容 detail 为 dict / list 两种形态。
        """
        m = INITIAL_STATE_RE.search(html)
        if not m:
            return ""
        try:
            state = json.loads(m.group(1))
        except json.JSONDecodeError:
            return ""
        detail = state.get("detail") or {}
        if isinstance(detail, list):
            detail = detail[0] if detail else {}
        if not isinstance(detail, dict):
            return ""
        # 复用统一提取逻辑（已兼容 modules dict/list + word.words/rich/text 节点）
        text = BiliClient._extract_detail_text(detail)
        if text:
            return text
        # 极端结构兜底：递归找 modules 里的长文本段落
        return BiliClient._deep_find_text(detail)

    @staticmethod
    def _deep_find_text(obj, path: str = "", depth: int = 0) -> str:
        """递归查找可读长文本（当标准结构解析不到正文时兜底）"""
        if depth > 8 or isinstance(obj, (str, int, float, bool)) or obj is None:
            return ""
        if isinstance(obj, dict):
            # 文本类字段优先
            for k in ("words", "text", "orig_text", "desc", "title", "summary"):
                v = obj.get(k)
                if isinstance(v, str) and len(v.strip()) >= MIN_CONTENT_LEN:
                    return v.strip()
            for v in obj.values():
                found = BiliClient._deep_find_text(v, path, depth + 1)
                if found:
                    return found
        elif isinstance(obj, list):
            for v in obj:
                found = BiliClient._deep_find_text(v, path, depth + 1)
                if found:
                    return found
        return ""

    @staticmethod
    def _extract_detail_text(item: dict) -> str:
        """从动态详情 item 提取正文，兼容两种 modules 结构：
        - dynamic/detail: modules 为 dict（module_dynamic.desc.text + major）
        - opus/detail:    modules 为 list（module_title.text + module_content.paragraphs）
        """
        if not item:
            return ""
        mods = item.get("modules")
        parts = []
        if isinstance(mods, dict):
            odyn = mods.get("module_dynamic") or {}
            desc = (odyn.get("desc") or {}).get("text") or ""
            if desc:
                parts.append(desc)
            parts.append(BiliClient._extract_major_text(odyn.get("major") or {}))
        elif isinstance(mods, list):
            for m in mods:
                if not isinstance(m, dict):
                    continue
                title = (m.get("module_title") or {}).get("text") or ""
                if title:
                    parts.append(title)
                content = m.get("module_content") or {}
                for para in (content.get("paragraphs") or []):
                    if not isinstance(para, dict):
                        continue
                    nodes = ((para.get("text") or {}).get("nodes")) or []
                    for node in nodes:
                        if not isinstance(node, dict):
                            continue
                        # HTML 版：TEXT_NODE_TYPE_WORD -> word.words
                        word = node.get("word") or {}
                        if isinstance(word, dict) and word.get("words"):
                            parts.append(str(word["words"]))
                            continue
                        # API 版富文本：rich.text / rich.orig_text
                        rich = node.get("rich") or {}
                        t = rich.get("text") or rich.get("orig_text") or ""
                        if t:
                            parts.append(t)
                            continue
                        # 纯文本兜底
                        plain = node.get("text")
                        if isinstance(plain, str) and plain.strip():
                            parts.append(plain)
        return "\n".join(p for p in parts if p).strip()

    @staticmethod
    def _extract_major_text(major: dict) -> str:
        """从 major 节点提取文本（DRAW 图文/专栏/视频等）"""
        if not isinstance(major, dict):
            return ""
        parts = []
        draw = major.get("draw") or {}
        if draw:
            for di in draw.get("items") or []:
                desc = (di.get("description") or "").strip()
                if desc:
                    parts.append(desc)
        for key in ("article", "opus"):
            obj = major.get(key) or {}
            if isinstance(obj, dict):
                t = obj.get("title") or ""
                if t:
                    parts.append(str(t))
                # 正文：article/opus 的 desc 常含完整文案（如"8月3日抽1位宝子得..."）
                d = obj.get("desc") or ""
                if isinstance(d, str) and d.strip():
                    parts.append(d.strip())
                summary = obj.get("summary") or {}
                if isinstance(summary, dict):
                    if summary.get("text"):
                        parts.append(summary["text"])
                    for node in (summary.get("rich_text_nodes") or []):
                        txt = (node.get("text") or "").strip()
                        if txt:
                            parts.append(txt)
        for key in ("archive", "pgc"):
            obj = major.get(key) or {}
            if isinstance(obj, dict):
                t = obj.get("title") or ""
                if t:
                    parts.append(str(t))
                d = obj.get("desc") or obj.get("subtitle") or ""
                if d:
                    parts.append(str(d))
        for key in ("music", "live", "live_rcmd", "common", "upower_common"):
            obj = major.get(key) or {}
            if isinstance(obj, dict):
                t = obj.get("title") or obj.get("name") or ""
                if t:
                    parts.append(str(t))
        common = major.get("common") or {}
        if isinstance(common, dict):
            for k in ("desc", "summary"):
                v = common.get(k) or ""
                if v:
                    parts.append(str(v))
        return "\n".join(p for p in parts if p).strip()

    # ---------------- 私信 ----------------

    def get_sessions(self) -> list:
        """私信会话列表（含未读等），尽量解析对端用户名/头像"""
        try:
            r = self.session.get(
                f"{VC}/session_svr/v1/session_svr/get_sessions",
                params={"session_type": 1, "group_fold": 1, "unfollow_fold": 0,
                        "sort_rule": 2, "build": 0, "mobi_app": "web", "size": 30},
                timeout=10)
            d = r.json()
            if d.get("code") != 0:
                raise RuntimeError(d.get("message", "获取私信失败"))
            sessions = []
            for s in d.get("data", {}).get("session_list", []):
                last = s.get("last_msg", {}) or {}
                content = self._decode_msg_content(last)
                tid = str(s.get("talker_id", ""))
                name, avatar = self._resolve_talker(tid)
                sessions.append({
                    "talker_id": tid,
                    "name": name,
                    "avatar": avatar,
                    "last_message": content,
                    "unread": s.get("unread_count", 0),
                    "last_seqno": (last.get("msg_seqno") or 0),  # 已读必需 ack_seqno
                    "msg_source": last.get("msg_source") or 0,   # 8=自动回复（官方标识）
                    "is_reply": bool(content and ("奖" in content or "中奖" in content)),
                })
            return sessions
        except Exception:
            return []   # 真实失败返回空列表，不回退演示数据

    def _resolve_talker(self, talker_id: str) -> tuple:
        """解析对端用户名/头像；失败回退数字 ID（带模块级缓存，避免重复请求）"""
        cached = _talker_cache.get(talker_id)
        if cached:
            return cached
        try:
            info = self.get_user_space(talker_id)
            name = info.get("username") or talker_id
            avatar = info.get("avatar", "")
        except Exception:
            name, avatar = talker_id, ""
        if len(_talker_cache) < 500:
            _talker_cache[talker_id] = (name, avatar)
        return name, avatar

    def get_session_messages(self, talker_id: str) -> list:
        """与某用户的私信消息列表

        注意：旧接口 session_svr/get_single_session 已失效（404），
        改用 svr_sync/fetch_session_msgs（B 站当前 Web 端私信接口）。
        begin_seqno=0 & end_seqno=0 表示拉取最近消息（实测有效）。
        """
        try:
            r = self.session.get(
                f"{VC}/svr_sync/v1/svr_sync/fetch_session_msgs",
                params={"talker_id": talker_id, "session_type": 1,
                        "begin_seqno": 0, "end_seqno": 0, "size": 20},
                timeout=10)
            d = r.json()
            if d.get("code") != 0:
                raise RuntimeError(d.get("message", "获取消息失败"))
            raw_msgs = (d.get("data") or {}).get("messages") or []
            me = str(self.session.cookies.get("DedeUserID", "0"))
            msgs = []
            for m in raw_msgs:
                sender_uid = str(m.get("sender_uid", ""))
                ts = m.get("timestamp") or m.get("msg_seqno") or 0
                msgs.append({
                    "sender": "self" if sender_uid == me else "other",
                    "sender_name": "我" if sender_uid == me else "对方",
                    "content": self._decode_msg_content(m),
                    "time": datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "",
                })
            # fetch_session_msgs 返回的消息按时间正序，无需反转
            return msgs
        except Exception:
            return []   # 真实失败返回空列表，不回退演示数据

    @staticmethod
    def _decode_msg_content(msg: dict) -> str:
        """B 站消息 content 可能是：纯文本 / JSON 字符串 / dict，递归提取可读文本"""
        if not msg:
            return ""
        raw = msg.get("content", "")
        return BiliClient._extract_text(raw)

    @staticmethod
    def _extract_text(raw) -> str:
        if raw is None:
            return ""
        if not isinstance(raw, str):
            raw = json.dumps(raw, ensure_ascii=False)
        # 尝试 JSON 解析（B 站 content 常为 JSON 字符串）
        try:
            obj = json.loads(raw)
        except Exception:
            return raw.strip()
        # 递归提取文本
        if isinstance(obj, dict):
            for k in ("content", "text", "message", "title", "desc", "summary"):
                if obj.get(k):
                    val = BiliClient._extract_text(obj[k])
                    if val:
                        return val
            # 富文本段落（如 [{"text": "..."}]）
            if isinstance(obj.get("rich_text"), dict):
                segs = obj["rich_text"].get("segments") or []
                parts = [BiliClient._extract_text(sg) for sg in segs]
                return "".join(p for p in parts if p)
            # 图片/表情消息兜底
            if obj.get("type") == "image":
                return "[图片]"
            return ""
        if isinstance(obj, list):
            return "".join(BiliClient._extract_text(x) for x in obj if x)
        return str(obj)

    # ---------------- 消息中心（@提及 / 评论回复） ----------------

    @staticmethod
    def _msg_time(ts) -> str:
        try:
            return datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M")
        except Exception:
            return ""

    def _fetch_msgfeed(self, kind: str, limit: int = 30) -> list:
        """拉取消息中心列表（kind: at=@提及 / reply=评论回复）"""
        url = f"{BASE}/x/msgfeed/{kind}"
        try:
            r = self.session.get(url, params={"build": 0, "mobi_app": "web"},
                                 timeout=10)
            d = r.json()
            if d.get("code") != 0:
                return []
            items = (d.get("data") or {}).get("items") or []
            out = []
            for it in items[:limit]:
                user = it.get("user") or {}
                item = it.get("item") or {}
                # 评论回复：评论内容在 reply.message / item
                reply = it.get("reply") or {}
                content = (reply.get("message") or item.get("content")
                           or item.get("title") or it.get("content") or "")
                # 动态链接（id_str 是动态 id）
                dyn_id = str(item.get("id_str") or item.get("id") or "")
                link = f"https://www.bilibili.com/opus/{dyn_id}" if dyn_id else ""
                out.append({
                    "time": self._msg_time(it.get("at_time") or it.get("ctime") or 0),
                    "from_user": user.get("uname", ""),
                    "from_uid": str(user.get("uid", "") or ""),
                    "from_avatar": normalize_avatar(user.get("avatar", "")),
                    "content": str(BiliClient._extract_text(content))[:300],
                    "link": link,
                })
            return out
        except Exception:
            return []

    def get_at_messages(self, limit: int = 30) -> list:
        """我被 @ 的列表（msgfeed/at）"""
        return self._fetch_msgfeed("at", limit)

    def get_reply_messages(self, limit: int = 30) -> list:
        """评论回复我的列表（msgfeed/reply）"""
        return self._fetch_msgfeed("reply", limit)

    # ---------------- 消息已读（私信 / @提及 / 评论回复） ----------------

    @staticmethod
    def is_auto_reply_msg(content: str) -> bool:
        """判断私信是否为「自动回复/关注欢迎」类消息（应自动已读不打扰）。

        特征：①消息内容自带"自动回复"字样；②关注欢迎语
        （"谢谢您的关注和厚爱""感谢关注""欢迎关注"等短句）。
        """
        if not content:
            return False
        import re as _re
        text = str(content)[:120]
        if "自动回复" in text:
            return True
        return bool(_re.search(
            r"(谢谢|感谢|多谢|欢迎|承蒙).{0,14}(关注|订阅|支持)"
            r"|(关注|订阅).{0,10}(谢谢|感谢|欢迎|厚爱)",
            text))

    def read_session(self, talker_id: str, ack_seqno: int = 0) -> bool:
        """标记单个私信会话已读（session_svr/update_ack）。

        ack_seqno 为会话最后一条消息的 msg_seqno（标记已读到该序号）。
        CSRF 需同时传 csrf 与 csrf_token（bili_jct），且不能用自定义
        Referer 覆盖默认（否则 -111 校验失败）。
        （注：read_session 旧路径已下线返回 404，正确接口为 update_ack）
        """
        try:
            csrf = self.session.cookies.get("bili_jct") or ""
            r = self.session.post(
                f"{VC}/session_svr/v1/session_svr/update_ack",
                data={"talker_id": int(talker_id), "session_type": 1,
                      "ack_seqno": int(ack_seqno or 0),
                      "csrf_token": csrf, "csrf": csrf},
                timeout=10)
            return r.json().get("code") == 0
        except Exception:
            return False

    def ack_at_unread(self) -> bool:
        """清除 @提及 未读（msgfeed/at_ack）"""
        try:
            r = self.session.post(
                f"{BASE}/x/msgfeed/at_ack",
                data={"build": 0, "mobi_app": "web", "platform": "web",
                      "seen_ts": int(time.time())},
                timeout=10)
            return r.json().get("code") == 0
        except Exception:
            return False

    def ack_reply_unread(self) -> bool:
        """清除 评论回复 未读（msgfeed/reply_ack）"""
        try:
            r = self.session.post(
                f"{BASE}/x/msgfeed/reply_ack",
                data={"build": 0, "mobi_app": "web", "platform": "web",
                      "seen_ts": int(time.time())},
                timeout=10)
            return r.json().get("code") == 0
        except Exception:
            return False


def cookies_from_json(text: str) -> dict:
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        return {}
