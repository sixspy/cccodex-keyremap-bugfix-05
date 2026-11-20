"""复现：一对多扇出回执绿、正式区只有主新键、别名表空/断链。

只读复现，不改写路径。使用临时 SQLite 库，不碰 data/app.db。
用法：.venv/bin/python ops/repro_扇出假绿.py
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import app as appmod  # noqa: E402
from app import app, init_db  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

TARGETS = ["NEW-901", "NEW-902", "NEW-903"]

tmp = Path(tempfile.mkdtemp(prefix="fanout-repro-"))
appmod.DB_PATH = tmp / "repro.db"
init_db()

with sqlite3.connect(appmod.DB_PATH) as conn:
    conn.executemany(
        "INSERT INTO mappings(version,old_key,new_key,kind) VALUES (?,?,?,?)",
        [("m-2026.11", "OLD-F9", t, "一对多") for t in TARGETS],
    )
    conn.commit()

client = TestClient(app)
token = client.post("/api/login", json={"username": "admin", "password": "123456"}).json()["token"]
h = {"Authorization": "Bearer " + token}
r = client.post("/api/remap", headers=h, json={"old_key": "OLD-F9"})

conn = sqlite3.connect(appmod.DB_PATH)
conn.row_factory = sqlite3.Row

def scalar(sql, args=()):
    return conn.execute(sql, args).fetchone()[0]

tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
mapped_targets = [r["new_key"] for r in conn.execute(
    "SELECT DISTINCT new_key FROM mappings WHERE version='m-2026.11' AND old_key='OLD-F9' ORDER BY new_key")]
formal_targets = [r["entity_key"] for r in conn.execute(
    "SELECT DISTINCT entity_key FROM formal_entities WHERE version='m-2026.11' AND mapped_from='OLD-F9' ORDER BY entity_key")]
alias_rows = (scalar("SELECT COUNT(*) FROM aliases WHERE old_key='OLD-F9'") if "aliases" in tables else 0)
receipts = (scalar("SELECT COUNT(*) FROM fanout_receipts WHERE old_key='OLD-F9'") if "fanout_receipts" in tables else 0)

print("== 复现：扇出假绿 ==")
print("POST /api/remap OLD-F9 ->", r.status_code, r.json())
print("映射册目标 (mappings)   :", mapped_targets, "共", len(mapped_targets))
print("正式区目标 (formal)     :", formal_targets, "共", len(formal_targets))
print("别名表 (aliases)        :", "表存在" if "aliases" in tables else "表不存在", "行数", alias_rows)
print("扇出回执 (receipts)     :", "表存在" if "fanout_receipts" in tables else "表不存在", "行数", receipts)
print()
fake_green = (r.status_code == 200 and len(mapped_targets) == 3
              and len(formal_targets) < 3 and alias_rows == 0)
print("回执口径按映射册计：目标", len(mapped_targets), "/ 3 ->", "齐全（绿）")
print("正式区实际落库：", len(formal_targets), "/ 3；别名：", alias_rows, "/ 3")
print("结论：", "假绿复现成功（回执绿、正式区只有主新键、别名表空/断链）" if fake_green else "未复现，需人工复核")
sys.exit(0 if fake_green else 1)
