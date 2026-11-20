
import hashlib, secrets, sqlite3
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

# 扇出切断卡（ops/扇出切断卡.md）冻结口径：只允许甲路径。
# 每个目标新键各插正式行，缺一则整笔事务回滚、正式零增；不得混用乙路径。
CUT_PATH = "甲"
LOCK_TTL_SECONDS = 60

def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def connect_rw():
    """显式事务连接：隔离级别 None，由扇出写路径自己 BEGIN/COMMIT/ROLLBACK。"""
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn

def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()

def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))

class LoginIn(BaseModel):
    username: str
    password: str

def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u

from datetime import datetime
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "实体键桥"
SKELETON = "实体键映射生效后入站先重映再进正式区，冲突进队列，映射版本与正式区对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS map_versions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version TEXT UNIQUE NOT NULL,
          status TEXT NOT NULL,
          effective_from TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mappings(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version TEXT NOT NULL,
          old_key TEXT NOT NULL,
          new_key TEXT NOT NULL,
          kind TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS formal_entities(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          entity_key TEXT NOT NULL,
          mapped_from TEXT NOT NULL,
          version TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS aliases(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version TEXT NOT NULL,
          old_key TEXT NOT NULL,
          new_key TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(version, old_key, new_key)
        );
        CREATE TABLE IF NOT EXISTS fanout_receipts(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version TEXT NOT NULL,
          old_key TEXT NOT NULL,
          target_count INTEGER NOT NULL,
          formal_count INTEGER NOT NULL,
          alias_count INTEGER NOT NULL,
          status TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(version, old_key)
        );
        CREATE TABLE IF NOT EXISTS fanout_locks(
          old_key TEXT PRIMARY KEY,
          holder TEXT NOT NULL,
          acquired_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS conflicts(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          old_key TEXT NOT NULL,
          reason TEXT NOT NULL,
          status TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_formal_mapped_from
          ON formal_entities(version, mapped_from);
        CREATE INDEX IF NOT EXISTS idx_aliases_old_key
          ON aliases(version, old_key);
        """)
        if conn.execute("SELECT COUNT(*) c FROM map_versions").fetchone()["c"] == 0:
            conn.execute("INSERT INTO map_versions(version,status,effective_from) VALUES (?,?,?)", ("m-2026.11","生效","2026-11-01"))
            conn.executemany("INSERT INTO mappings(version,old_key,new_key,kind) VALUES (?,?,?,?)", [
                ("m-2026.11","OLD-A1","NEW-100","一对一"),
                ("m-2026.11","OLD-B1","NEW-200","多对一"),
                ("m-2026.11","OLD-B2","NEW-200","多对一"),
            ])
            conn.execute("INSERT INTO formal_entities(entity_key,mapped_from,version,created_at) VALUES (?,?,?,?)",
                         ("NEW-100","OLD-A1","m-2026.11","2026-11-10T09:00:00"))
            conn.execute("INSERT INTO conflicts(old_key,reason,status) VALUES (?,?,?)", ("OLD-C9","无映射","挂起"))

        # 一对多扇出夹具：只在缺失时插入，保持幂等，不改动已有映射行。
        if conn.execute("SELECT COUNT(*) c FROM mappings WHERE old_key='OLD-F1'").fetchone()["c"] == 0:
            conn.executemany("INSERT INTO mappings(version,old_key,new_key,kind) VALUES (?,?,?,?)", [
                ("m-2026.11","OLD-F1","NEW-310","一对多"),
                ("m-2026.11","OLD-F1","NEW-311","一对多"),
                ("m-2026.11","OLD-F1","NEW-312","一对多"),
            ])

def backfill_aliases(conn):
    """幂等回填：只为正式区已存在的目标补别名/回执，不伪造未落库的目标。"""
    formal_rows = conn.execute(
        """SELECT version, mapped_from AS old_key, entity_key,
                  MIN(created_at) AS created_at
           FROM formal_entities GROUP BY version, mapped_from, entity_key"""
    ).fetchall()
    for r in formal_rows:
        conn.execute(
            "INSERT OR IGNORE INTO aliases(version,old_key,new_key,created_at) VALUES (?,?,?,?)",
            (r["version"], r["old_key"], r["entity_key"], r["created_at"]),
        )
    pairs = conn.execute(
        "SELECT DISTINCT version, old_key FROM mappings ORDER BY version, old_key"
    ).fetchall()
    for p in pairs:
        version, old_key = p["version"], p["old_key"]
        mapped = {x["new_key"] for x in conn.execute(
            "SELECT DISTINCT new_key FROM mappings WHERE version=? AND old_key=?", (version, old_key)
        ).fetchall()}
        formal = {x["entity_key"] for x in conn.execute(
            "SELECT DISTINCT entity_key FROM formal_entities WHERE version=? AND mapped_from=?", (version, old_key)
        ).fetchall()}
        alias = {x["new_key"] for x in conn.execute(
            "SELECT DISTINCT new_key FROM aliases WHERE version=? AND old_key=?", (version, old_key)
        ).fetchall()}
        # 只有映射册目标齐全地同时存在于正式区与别名表，才写「齐全」回执。
        if mapped and mapped == formal == alias:
            conn.execute(
                """INSERT INTO fanout_receipts(version,old_key,target_count,formal_count,alias_count,status,created_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(version,old_key) DO NOTHING""",
                (version, old_key, len(mapped), len(formal), len(alias), "齐全",
                 datetime.now().isoformat(timespec="seconds")),
            )
        conn.commit()

@app.on_event("startup")
def _s():
    init_db()
    with connect() as conn:
        backfill_aliases(conn)

@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM mappings").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "mappings": n, "db": str(DB_PATH), "cut_path": CUT_PATH}

@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username,role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row: raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok]=row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}

@app.get("/api/map-versions")
def versions(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM map_versions ORDER BY id").fetchall()]}

@app.get("/api/mappings")
def mappings(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM mappings ORDER BY id").fetchall()]}

class RemapIn(BaseModel):
    old_key: str

def _record_conflict(conn, old_key: str, reason: str, status: str):
    conn.execute("INSERT INTO conflicts(old_key,reason,status) VALUES (?,?,?)", (old_key, reason, status))

def _parse_ts(value: str):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None

def acquire_fanout_lock(old_key: str, holder: str) -> bool:
    """同一旧键独占锁；后到立即判争用；超过 TTL 的陈旧锁可回收。"""
    now = datetime.now()
    with connect_rw() as conn:
        try:
            conn.execute(
                "INSERT INTO fanout_locks(old_key,holder,acquired_at) VALUES (?,?,?)",
                (old_key, holder, now.isoformat(timespec="seconds")),
            )
            return True
        except sqlite3.IntegrityError:
            row = conn.execute("SELECT * FROM fanout_locks WHERE old_key=?", (old_key,)).fetchone()
            if row is None:
                return False
            acquired = _parse_ts(row["acquired_at"])
            if acquired is None or (now - acquired).total_seconds() > LOCK_TTL_SECONDS:
                conn.execute(
                    "UPDATE fanout_locks SET holder=?, acquired_at=? WHERE old_key=?",
                    (holder, now.isoformat(timespec="seconds"), old_key),
                )
                return True
            return False

def release_fanout_lock(old_key: str, holder: str):
    with connect_rw() as conn:
        conn.execute("DELETE FROM fanout_locks WHERE old_key=? AND holder=?", (old_key, holder))

class FanoutIncomplete(Exception):
    pass

def fanout_write(version: str, old_key: str, targets: list[str], now: str) -> dict:
    """甲路径：全部目标同事务落正式行 + 别名行 + 回执；任一缺失整笔回滚。"""
    targets_set = set(targets)
    conn = connect_rw()
    try:
        conn.execute("BEGIN IMMEDIATE")

        existing_formal = {
            r["entity_key"] for r in conn.execute(
                "SELECT DISTINCT entity_key FROM formal_entities WHERE version=? AND mapped_from=?",
                (version, old_key),
            ).fetchall()
        }
        for key in sorted(targets_set):
            if key not in existing_formal:
                conn.execute(
                    "INSERT INTO formal_entities(entity_key,mapped_from,version,created_at) VALUES (?,?,?,?)",
                    (key, old_key, version, now),
                )
        for key in sorted(targets_set):
            conn.execute(
                "INSERT OR IGNORE INTO aliases(version,old_key,new_key,created_at) VALUES (?,?,?,?)",
                (version, old_key, key, now),
            )

        # 提交前复核：正式区与别名表都必须覆盖映射册全部目标。
        formal_now = {
            r["entity_key"] for r in conn.execute(
                "SELECT DISTINCT entity_key FROM formal_entities WHERE version=? AND mapped_from=?",
                (version, old_key),
            ).fetchall()
        }
        alias_now = {
            r["new_key"] for r in conn.execute(
                "SELECT DISTINCT new_key FROM aliases WHERE version=? AND old_key=?",
                (version, old_key),
            ).fetchall()
        }
        if not targets_set.issubset(formal_now) or not targets_set.issubset(alias_now):
            raise FanoutIncomplete("扇出未落齐：正式区或别名表缺目标")

        conn.execute(
            """INSERT INTO fanout_receipts(version,old_key,target_count,formal_count,alias_count,status,created_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(version,old_key) DO UPDATE SET
                 target_count=excluded.target_count,
                 formal_count=excluded.formal_count,
                 alias_count=excluded.alias_count,
                 status=excluded.status,
                 created_at=excluded.created_at""",
            (version, old_key, len(targets_set), len(formal_now), len(alias_now), "齐全", now),
        )

        # 回执落库后再次从库内事实取数，确保五处同态。
        receipt = conn.execute(
            "SELECT * FROM fanout_receipts WHERE version=? AND old_key=?", (version, old_key)
        ).fetchone()
        if (receipt["target_count"], receipt["formal_count"], receipt["alias_count"]) != (
            len(targets_set), len(targets_set), len(targets_set)
        ):
            raise FanoutIncomplete("回执计数与落库不一致")

        conn.execute("COMMIT")
        return {
            "entity_key": sorted(targets_set)[0],
            "targets": sorted(targets_set),
            "target_count": len(targets_set),
            "version": version,
        }
    except FanoutIncomplete:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    except sqlite3.Error as exc:
        # 任一目标插入失败（如别名写失败）都整笔回滚，正式区零增。
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise FanoutIncomplete(f"落库失败：{exc}") from exc
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    finally:
        conn.close()

@app.post("/api/remap")
def remap(body: RemapIn, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    now = datetime.now().isoformat(timespec="seconds")

    # 切断卡冻结口径：非甲路径一律拒写，并记「扇出偏了」。
    if CUT_PATH != "甲":
        with connect() as c:
            _record_conflict(c, body.old_key, "扇出偏了", "拒绝")
            c.commit()
        raise HTTPException(409, "切断卡口径偏离（非甲），拒绝写入，已记扇出偏了")

    with connect() as c:
        ver = c.execute("SELECT version FROM map_versions WHERE status='生效' ORDER BY id DESC LIMIT 1").fetchone()
        if not ver: raise HTTPException(404, "无生效映射版本")
        rows = c.execute(
            "SELECT DISTINCT new_key FROM mappings WHERE version=? AND old_key=? ORDER BY new_key",
            (ver["version"], body.old_key),
        ).fetchall()
        targets = [r["new_key"] for r in rows]
        if not targets:
            _record_conflict(c, body.old_key, "无映射", "挂起")
            c.commit()
            raise HTTPException(409, "无映射，已进冲突队列")

    holder = f"{user}:{secrets.token_hex(8)}"
    if not acquire_fanout_lock(body.old_key, holder):
        with connect() as c:
            _record_conflict(c, body.old_key, "争用", "争用")
            c.commit()
        raise HTTPException(409, "扇出争用：旧键正被其他写者处理，已记争用")

    try:
        return fanout_write(ver["version"], body.old_key, targets, now)
    except FanoutIncomplete as exc:
        with connect() as c:
            _record_conflict(c, body.old_key, f"扇出失败：{exc}", "回滚")
            c.commit()
        raise HTTPException(409, f"扇出失败，已整笔回滚：{exc}")
    finally:
        release_fanout_lock(body.old_key, holder)

@app.get("/api/formal")
def formal(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM formal_entities ORDER BY id DESC").fetchall()]}

@app.get("/api/aliases")
def aliases(authorization: str | None = Header(default=None), old_key: str | None = None):
    require_user(authorization)
    with connect() as c:
        if old_key is None:
            rows = c.execute("SELECT * FROM aliases ORDER BY id DESC").fetchall()
        else:
            rows = c.execute("SELECT * FROM aliases WHERE old_key=? ORDER BY id", (old_key,)).fetchall()
        return {"items":[dict(r) for r in rows]}

@app.get("/api/fanout-receipts")
def fanout_receipts(authorization: str | None = Header(default=None), old_key: str | None = None):
    require_user(authorization)
    with connect() as c:
        if old_key is None:
            rows = c.execute("SELECT * FROM fanout_receipts ORDER BY id DESC").fetchall()
        else:
            rows = c.execute("SELECT * FROM fanout_receipts WHERE old_key=? ORDER BY id", (old_key,)).fetchall()
        return {"items":[dict(r) for r in rows]}

@app.get("/api/reconcile")
def reconcile(authorization: str | None = Header(default=None)):
    """互证页：只读库内事实，按 (version, old_key) 对齐映射册/正式区/别名表/回执。"""
    require_user(authorization)
    with connect() as c:
        mapped = c.execute(
            """SELECT version, old_key, COUNT(DISTINCT new_key) mapped_count
               FROM mappings GROUP BY version, old_key ORDER BY version, old_key"""
        ).fetchall()
        items = []
        for m in mapped:
            version, old_key = m["version"], m["old_key"]
            formal_count = c.execute(
                "SELECT COUNT(DISTINCT entity_key) n FROM formal_entities WHERE version=? AND mapped_from=?",
                (version, old_key),
            ).fetchone()["n"]
            alias_count = c.execute(
                "SELECT COUNT(DISTINCT new_key) n FROM aliases WHERE version=? AND old_key=?",
                (version, old_key),
            ).fetchone()["n"]
            receipt = c.execute(
                "SELECT * FROM fanout_receipts WHERE version=? AND old_key=?", (version, old_key)
            ).fetchone()
            receipt_count = receipt["target_count"] if receipt else 0
            consistent = bool(
                receipt
                and receipt["status"] == "齐全"
                and receipt_count == m["mapped_count"] == formal_count == alias_count
                and receipt["formal_count"] == formal_count
                and receipt["alias_count"] == alias_count
            )
            items.append({
                "version": version,
                "old_key": old_key,
                "mapped_count": m["mapped_count"],
                "formal_count": formal_count,
                "alias_count": alias_count,
                "receipt_count": receipt_count,
                "receipt_status": receipt["status"] if receipt else "缺失",
                "consistent": consistent,
            })
        return {"items": items}

@app.get("/api/conflicts")
def conflicts(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM conflicts ORDER BY id DESC").fetchall()]}

@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
