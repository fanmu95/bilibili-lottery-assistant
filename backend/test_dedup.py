import sys, json, time
sys.path.insert(0, '.')
from app.database import SessionLocal
from app import models
from app.services.bili_client import BiliClient, cookies_from_json

db = SessionLocal()
acc = db.query(models.Account).filter_by(status='active').first()
client = BiliClient(cookies_from_json(acc.cookies) if acc and acc.cookies else None)

# 1. 拉蛋卷罢候选
items = client.get_space_dynamics('3546771737283009', username='就叫蛋卷罢',
                                  source_type='repost', since_days=10)
danjuan_ids = {i["activity_id"] for i in items if i.get("is_lottery") is True}
print(f'蛋卷罢抽奖候选: {len(danjuan_ids)} 条')

# 2. 本地库（阿祾哥哥之前扫到 + 之前所有）
local_rows = db.query(models.Activity).all()
local_ids = {r.activity_id for r in local_rows}
print(f'本地库活动: {len(local_ids)} 条')

# 3. 交集（跨用户重复）
dup = danjuan_ids & local_ids
new = danjuan_ids - local_ids
print(f'与本地库重复: {len(dup)} 条')
print(f'真正新增: {len(new)} 条')
print()
print('重复的具体活动（应被预筛跳过，不再提交 LLM）:')
for did in list(dup)[:8]:
    row = next((r for r in local_rows if r.activity_id == did), None)
    if row:
        print(f'  {did} | {row.title[:35]} | status={row.status}')
db.close()
