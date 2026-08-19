"""转发动态清理接口：账号维度统计/执行（异步 + 进度轮询）"""
import threading
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import SessionLocal, get_db
from .. import models
from ..services import cleanup_service
from .logs import add_log

router = APIRouter(prefix="/api/cleanup", tags=["cleanup"])


class AccountCleanupBody(BaseModel):
    # 规则勾选（多选 = AND，全部满足才删）
    r1: bool = True                  # 规则1：列表内结束日期超 N 天
    r2: bool = False                 # 规则2：转发时间超 X 天直接删
    r3: bool = False                 # 规则3：列表外动态用 LLM 解析结束时间
    r4: bool = False                 # 规则4：官方互动抽奖已截止 → 专门检查清除
    end_days: int = 7                # 规则1/3：结束日期距今超过 N 天
    forward_days: int = 0            # 规则2：转发时间距今超过 X 天（0=不启用）
    whitelist: Optional[list] = None  # 白名单：中奖动态 id，跳过不删
    llm_parse: bool = False          # （兼容）规则3 别名
    interactive_clean: bool = False  # （兼容）规则4 别名
    scan_gap: float = 1.0            # 翻页间隔（秒），防风控
    max_pages: int = 30              # 扫描空间动态的最大翻页数
    items: Optional[list] = None     # 复用统计结果直删（{id, repost_id, ...}），不重新扫描
    dry_run: bool = True             # True=统计预览 / False=执行删除


def _run_cleanup_thread(account_id: int, body: AccountCleanupBody):
    """后台线程：执行统计/删除，进度写 cleanup_service._progress，完成后存 result"""
    db = SessionLocal()
    try:
        cleanup_service._init_progress(account_id, stage="准备中...")
        acc = db.get(models.Account, account_id)
        if not acc:
            cleanup_service._set_progress(account_id, stage="账号不存在")
            cleanup_service._finish_progress(account_id, {"error": "账号不存在"})
            return
        if body.items:
            # 复用统计结果直删（删除立即开始，进度 = 删除 x/N）
            stat = cleanup_service.delete_cleanup_items(
                db, acc, account_id=account_id,
                items=body.items, dry_run=body.dry_run)
        else:
            stat = cleanup_service.cleanup_account_dynamics(
                db, acc, account_id=account_id,
                end_days=body.end_days, forward_days=body.forward_days,
                whitelist=body.whitelist, llm_parse=body.llm_parse,
                interactive_clean=body.interactive_clean,
                r1=body.r1, r2=body.r2, r3=body.r3, r4=body.r4,
                dry_run=body.dry_run, max_pages=body.max_pages, scan_gap=body.scan_gap)
        if stat.get("error"):
            cleanup_service._set_progress(account_id, stage=stat["error"])
        if stat["deleted"]:
            add_log(db, "warning", "activity",
                    f"账号 {acc.username} 清理：删除 {stat['deleted']} 条转发动态")
        cleanup_service._finish_progress(account_id, stat)
    except Exception as e:
        cleanup_service._set_progress(account_id, stage=f"出错: {e}")
        cleanup_service._finish_progress(account_id, {"error": str(e)})
    finally:
        db.close()


@router.post("/accounts/{account_id}/dynamics")
def start_cleanup_account(account_id: int, body: AccountCleanupBody):
    """启动账号维度清理（异步）。统计过程与结果通过 GET progress 轮询获取。"""
    if cleanup_service.get_progress(account_id).get("running"):
        raise HTTPException(400, "该账号清理任务正在运行中，请等待完成")
    t = threading.Thread(target=_run_cleanup_thread,
                         args=(account_id, body), daemon=True)
    t.start()
    return {"started": True, "dry_run": body.dry_run}


@router.get("/accounts/{account_id}/progress")
def cleanup_account_progress(account_id: int):
    """轮询清理进度：扫描页数/候选数/删除进度；完成后返回 result（统计结果）"""
    return cleanup_service.get_progress(account_id)
