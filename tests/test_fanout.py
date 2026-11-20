"""扇出切断卡（ops/扇出切断卡.md）的执行测试。

红线两条（先见红，后改写路径）：
  - test_alias_failure_rolls_back_formal_zero_gain
  - test_same_old_key_contends

全绿口径：映射册目标数 == 正式行数 == 别名行数 == 回执行数。
"""
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import app as appmod
from app import app, init_db

OLD = "OLD-F9"
VERSION = "m-2026.11"
TARGETS = ("NEW-901", "NEW-902", "NEW-903")


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "DB_PATH", tmp_path / "t.db")
    appmod.SESSIONS.clear()
    init_db()
    yield appmod.DB_PATH


@pytest.fixture
def client(_db):
    return TestClient(app)


def _auth(client):
    tok = client.post(
        "/api/login", json={"username": "admin", "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _add_fanout(db, old_key=OLD, targets=TARGETS):
    with closing(sqlite3.connect(db)) as conn:
        conn.executemany(
            "INSERT INTO mappings(version,old_key,new_key,kind) VALUES (?,?,?,?)",
            [(VERSION, old_key, t, "一对多") for t in targets],
        )
        conn.commit()


def _rows(db, sql, args=()):
    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _count(db, sql, args=()):
    return _rows(db, sql, args)[0]["n"]


def _formal(db, old_key=OLD):
    return {
        r["entity_key"]
        for r in _rows(
            db,
            "SELECT DISTINCT entity_key FROM formal_entities WHERE version=? AND mapped_from=?",
            (VERSION, old_key),
        )
    }


def _aliases(db, old_key=OLD):
    return {
        r["new_key"]
        for r in _rows(
            db,
            "SELECT DISTINCT new_key FROM aliases WHERE version=? AND old_key=?",
            (VERSION, old_key),
        )
    }


def _receipts(db, old_key=OLD):
    return _rows(
        db,
        "SELECT * FROM fanout_receipts WHERE version=? AND old_key=?",
        (VERSION, old_key),
    )


# ---------------------------------------------------------------- 红测一
def test_alias_failure_rolls_back_formal_zero_gain(client, _db):
    """甲路径：任一目标别名写失败，整笔事务回滚，正式区零增。"""
    _add_fanout(_db)
    # 第一条别名能写，第二条失败：确保回滚时正式区/别名表已有写入。
    # SQLite 触发器不能绑定变量，改裁判器挂到别名表上：写第二个目标时失败。
    with closing(sqlite3.connect(_db)) as conn:
        conn.execute(
            f"""CREATE TRIGGER fail_second_alias BEFORE INSERT ON aliases
                WHEN (SELECT COUNT(*) FROM aliases
                      WHERE version='{VERSION}' AND old_key='{OLD}') >= 1
                BEGIN SELECT RAISE(ABORT, 'alias write failed'); END;"""
        )
        conn.commit()

    formal_before = _count(_db, "SELECT COUNT(*) n FROM formal_entities")
    alias_before = _count(_db, "SELECT COUNT(*) n FROM aliases")

    r = client.post("/api/remap", headers=_auth(client), json={"old_key": OLD})

    assert r.status_code in (409, 500), r.text
    assert _count(_db, "SELECT COUNT(*) n FROM formal_entities") == formal_before
    assert _count(_db, "SELECT COUNT(*) n FROM aliases") == alias_before
    assert _aliases(_db, OLD) == set()
    assert _receipts(_db, OLD) == []
    assert _count(
        _db, "SELECT COUNT(*) n FROM fanout_locks WHERE old_key=?", (OLD,)
    ) == 0


# ---------------------------------------------------------------- 红测二
def test_same_old_key_contends(client, _db):
    """同一旧键已占锁，后到立即判争用，不排队、不落正式区。"""
    _add_fanout(_db)
    held_at = datetime.now().isoformat(timespec="seconds")
    with closing(sqlite3.connect(_db)) as conn:
        conn.execute(
            "INSERT INTO fanout_locks(old_key,holder,acquired_at) VALUES (?,?,?)",
            (OLD, "other-writer", held_at),
        )
        conn.commit()

    formal_before = _count(_db, "SELECT COUNT(*) n FROM formal_entities")
    r = client.post("/api/remap", headers=_auth(client), json={"old_key": OLD})

    assert r.status_code == 409, r.text
    assert "争用" in r.json()["detail"]
    assert _formal(_db) == set()
    assert _count(_db, "SELECT COUNT(*) n FROM formal_entities") == formal_before
    assert _receipts(_db, OLD) == []
    conflict = _rows(
        _db,
        "SELECT * FROM conflicts WHERE old_key=? AND reason='争用' AND status='争用'",
        (OLD,),
    )
    assert len(conflict) == 1
    lock = _rows(_db, "SELECT * FROM fanout_locks WHERE old_key=?", (OLD,))
    assert len(lock) == 1 and lock[0]["holder"] == "other-writer"


# ---------------------------------------------------------------- 甲路径完备性
def test_fanout_lands_all_targets_and_five_places_agree(client, _db):
    _add_fanout(_db)
    h = _auth(client)

    r = client.post("/api/remap", headers=h, json={"old_key": OLD})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.get("targets", [])) == set(TARGETS)

    # 映射册 / 正式区 / 别名表
    mapped = {
        x["new_key"]
        for x in _rows(
            _db,
            "SELECT DISTINCT new_key FROM mappings WHERE version=? AND old_key=?",
            (VERSION, OLD),
        )
    }
    assert mapped == _formal(_db) == _aliases(_db) == set(TARGETS)

    # 扇出回执
    receipts = _receipts(_db)
    assert len(receipts) == 1
    rec = receipts[0]
    assert (rec["target_count"], rec["formal_count"], rec["alias_count"]) == (3, 3, 3)
    assert rec["status"] == "齐全"

    # 别名查询
    q = client.get("/api/aliases", headers=h, params={"old_key": OLD}).json()["items"]
    assert {x["new_key"] for x in q} == set(TARGETS)

    # 互证页（只读库内事实，不由页面拼接）
    items = client.get("/api/reconcile", headers=h).json()["items"]
    row = [x for x in items if x["old_key"] == OLD and x["version"] == VERSION]
    assert len(row) == 1
    assert row[0]["consistent"] is True
    assert row[0]["mapped_count"] == row[0]["formal_count"] == 3
    assert row[0]["alias_count"] == row[0]["receipt_count"] == 3

    # 写路径自释放锁
    assert _rows(_db, "SELECT * FROM fanout_locks WHERE old_key=?", (OLD,)) == []


def test_fanout_is_idempotent(client, _db):
    """重复扇出同一旧键不产生重复正式行/别名，五处仍同态。"""
    _add_fanout(_db)
    h = _auth(client)
    assert client.post("/api/remap", headers=h, json={"old_key": OLD}).status_code == 200
    assert client.post("/api/remap", headers=h, json={"old_key": OLD}).status_code == 200
    assert _formal(_db) == _aliases(_db) == set(TARGETS)
    rec = _receipts(_db)[0]
    assert (rec["target_count"], rec["formal_count"], rec["alias_count"]) == (3, 3, 3)


def test_real_concurrent_fanout_contends(client, _db, monkeypatch):
    """真并发：写者持锁期间后到者必判争用，只允许一个成功。"""
    _add_fanout(_db)
    h = _auth(client)
    entered = threading.Event()
    release = threading.Event()
    original = appmod.fanout_write

    def blocked_fanout(*args, **kwargs):
        entered.set()
        assert release.wait(5), "测试屏障超时"
        return original(*args, **kwargs)

    monkeypatch.setattr(appmod, "fanout_write", blocked_fanout)

    results: dict[str, int] = {}

    def first():
        c = TestClient(app)
        results["first"] = c.post("/api/remap", headers=h, json={"old_key": OLD}).status_code

    t = threading.Thread(target=first)
    t.start()
    assert entered.wait(5), "首个写者未进入扇出"

    # 首个写者此刻仍持锁：后到者必须立即判争用。
    late = TestClient(app).post("/api/remap", headers=h, json={"old_key": OLD})
    assert late.status_code == 409, late.text
    assert "争用" in late.json()["detail"]

    release.set()
    t.join(5)
    assert not t.is_alive()

    assert results["first"] == 200
    assert _formal(_db) == _aliases(_db) == set(TARGETS)
    assert len(_receipts(_db)) == 1
    assert _rows(_db, "SELECT * FROM fanout_locks WHERE old_key=?", (OLD,)) == []
    assert _count(
        _db,
        "SELECT COUNT(*) n FROM conflicts WHERE old_key=? AND reason='争用' AND status='争用'",
        (OLD,),
    ) == 1


def test_deviation_from_cut_card_is_rejected(client, _db, monkeypatch):
    """切断卡只认甲路径：运行期被改成乙（或其它）一律拒写并记扇出偏了。"""
    _add_fanout(_db)
    monkeypatch.setattr(appmod, "CUT_PATH", "乙")

    r = client.post("/api/remap", headers=_auth(client), json={"old_key": OLD})

    assert r.status_code == 409, r.text
    assert "扇出偏了" in r.json()["detail"]
    assert _count(_db, "SELECT COUNT(*) n FROM formal_entities WHERE mapped_from=?", (OLD,)) == 0
    assert _aliases(_db) == set()
    assert _receipts(_db) == []
    bad = _rows(
        _db,
        "SELECT * FROM conflicts WHERE old_key=? AND reason='扇出偏了' AND status='拒绝'",
        (OLD,),
    )
    assert len(bad) == 1
