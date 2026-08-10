import sys, time, traceback
sys.path.insert(0, '.')
from app.database import SessionLocal
from app import models
from app.services.bili_client import BiliClient, cookies_from_json
db = SessionLocal()
acc = db.query(models.Account).filter_by(status='active').first()
client = BiliClient(cookies_from_json(acc.cookies) if acc and acc.cookies else None)
db.close()
uid = '3546771737283009'
try:
    t0 = time.time()
    items = client.get_space_dynamics(uid, username='就叫蛋卷罢', source_type='repost', since_days=10)
    print(f'蛋卷罢动态: {len(items)} 条, 耗时 {time.time()-t0:.1f}s')
    lottery = [i for i in items if i.get('is_lottery') is True]
    print(f'抽奖候选: {len(lottery)} 条')
    for i in lottery[:8]:
        print(f'  {i["activity_id"]} | {i.get("title","")[:35]} | pub={i.get("publish_time")}')
except Exception:
    traceback.print_exc()
