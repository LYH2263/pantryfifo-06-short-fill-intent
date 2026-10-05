import sqlite3
import threading
from datetime import date, timedelta

import pytest
from fastapi import HTTPException

from app import main, seed
from app.db import connect
from app.modules.shortfall_intent import service


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seed.init_db()
    yield


def _consume_short(item_id: int, qty: float) -> dict:
    with pytest.raises(HTTPException) as ei:
        main.consume(main.ConsumeIn(item_id=item_id, qty=qty))
    assert ei.value.status_code == 409
    return ei.value.detail


def _count(conn, table: str, where: str = "", args=()) -> int:
    return conn.execute(f"SELECT COUNT(*) c FROM {table} {where}", args).fetchone()["c"]


def _all_lots(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM lots ORDER BY id")]


def _pending_intents(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM shortfall_intents WHERE status='pending' ORDER BY id")]


# --- 需求 1、2：short 失败登记意图，全层余量保持失败前 ----------------------

def test_short_registers_intent_and_leaves_lots_untouched(db):
    # item 1: 2 @2026-10-01 + 1 @2026-09-28 = 3 on shelf
    c = connect()
    before_lots = _all_lots(c)
    consumptions_before = _count(c, "consumptions")

    detail = _consume_short(1, 7)

    after_lots = _all_lots(c)
    assert detail["reason"] == "short" and detail["short"] == 4
    assert isinstance(detail["intent_id"], int)
    intents = _pending_intents(c)
    assert len(intents) == 1
    assert intents[0]["item_id"] == 1 and intents[0]["short_qty"] == 4
    assert intents[0]["demand_qty"] == 7 and intents[0]["layer"] == "upper"
    assert after_lots == before_lots                      # 零扣减
    assert _count(c, "consumptions") == consumptions_before  # 失败不留流水
    c.close()


# --- 需求 2：模拟扣减只作快照，不落库 -------------------------------------

def test_simulated_deductions_never_persist(db):
    import json
    detail = _consume_short(1, 7)
    c = connect()
    intent = c.execute("SELECT * FROM shortfall_intents WHERE id=?",
                       (detail["intent_id"],)).fetchone()
    snap = json.loads(intent["deductions_json"])
    assert [d["take"] for d in snap] == [1, 2]            # 临期批先"模拟"吃
    assert all(r["status"] == "on_shelf"
               for r in c.execute("SELECT * FROM lots WHERE item_id=1"))
    assert {r["qty_remain"] for r in c.execute(
        "SELECT * FROM lots WHERE item_id=1")} == {1, 2}
    c.close()


# --- 需求 3：确认插入一批，数量=冻结缺口，默认到期日 -----------------------

def test_confirm_inserts_one_lot_with_frozen_qty(db):
    detail = _consume_short(1, 7)
    lots_before = 0
    c = connect(); lots_before = _count(c, "lots"); c.close()

    out = service.confirm_intent(detail["intent_id"])

    c = connect()
    assert out["already_confirmed"] is False
    assert _count(c, "lots") == lots_before + 1
    lot = dict(c.execute("SELECT * FROM lots WHERE id=?", (out["lot_id"],)).fetchone())
    assert lot["item_id"] == 1
    assert lot["qty_in"] == 4 and lot["qty_remain"] == 4
    assert lot["status"] == "on_shelf" and lot["data_quality"] == "clean"
    assert lot["expiry"] == (date.today() + timedelta(days=30)).isoformat()
    intent = dict(c.execute("SELECT * FROM shortfall_intents WHERE id=?",
                            (detail["intent_id"],)).fetchone())
    assert intent["status"] == "confirmed" and intent["lot_id"] == lot["id"]
    assert intent["confirmed_at"]
    c.close()


def test_confirm_with_explicit_expiry(db):
    detail = _consume_short(1, 7)
    out = service.confirm_intent(detail["intent_id"], "2027-01-15")
    assert out["expiry"] == "2027-01-15" and out["qty_in"] == 4


def test_confirm_bad_expiry_400(db):
    detail = _consume_short(1, 7)
    with pytest.raises(HTTPException) as ei:
        service.confirm_intent(detail["intent_id"], "not-a-date")
    assert ei.value.status_code == 400
    c = connect()
    assert _count(c, "shortfall_intents", "WHERE status='pending'") == 1
    c.close()


# --- 需求 6：二次确认幂等，不再增一批 -------------------------------------

def test_second_confirm_is_idempotent(db):
    detail = _consume_short(1, 7)
    first = service.confirm_intent(detail["intent_id"])
    c = connect(); lots_after_first = _count(c, "lots"); c.close()

    second = service.confirm_intent(detail["intent_id"])

    c = connect()
    assert second["already_confirmed"] is True
    assert second["lot_id"] == first["lot_id"]
    assert _count(c, "lots") == lots_after_first
    c.close()


# --- 需求 4、7：pending 不冻结任何东西，也不进在架候选 ---------------------

def test_pending_freezes_nothing(db):
    shelf_before = main.fridge()
    detail = _consume_short(1, 7)
    # pending 不产生任何在架批：全层快照与失败前完全一致
    assert main.fridge() == shelf_before
    # 同品入库不被阻止
    inbound = main.inbound(main.LotIn(item_id=1, qty=5, expiry="2027-01-01"))
    # 足量消费照常扣减（新批到期晚，先吃旧批）
    ok = main.consume(main.ConsumeIn(item_id=1, qty=2))
    assert ok["ok"] is True
    assert isinstance(inbound["id"], int)
    c = connect()
    pending = _pending_intents(c)
    assert len(pending) == 1 and pending[0]["id"] == detail["intent_id"]  # 成功消费不自动关闭
    c.close()


# --- 需求 4：重复 short 合并到同一 pending（latest-wins） ------------------

def test_repeated_short_merges_to_same_pending(db):
    first = _consume_short(1, 7)     # 存量 3，缺口 4
    second = _consume_short(1, 6)    # 失败未扣，存量仍 3，缺口 3
    c = connect()
    pending = _pending_intents(c)
    assert len(pending) == 1
    assert pending[0]["id"] == first["intent_id"] == second["intent_id"]
    assert pending[0]["short_qty"] == 3 and pending[0]["demand_qty"] == 6
    # 确认后同品可以再次登记新的 pending
    service.confirm_intent(first["intent_id"])
    third = _consume_short(1, 100)
    pending2 = _pending_intents(c)
    assert len(pending2) == 1 and pending2[0]["id"] != first["intent_id"]
    c.close()


# --- 需求 5：sweep 带走同品过期批，缺口不被改写 ----------------------------

def test_confirm_does_not_recompute_after_sweep(db):
    detail = _consume_short(1, 7)    # 缺口 4
    # 把同品在架批显式置为昨天到期，再让下架扫把它们带走
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    c = connect()
    c.execute("UPDATE lots SET expiry=? WHERE item_id=1 AND status='on_shelf'",
              (yesterday,))
    c.commit(); c.close()

    swept = main.expire_sweep()

    c = connect()
    swept_item1 = {r["id"] for r in c.execute(
        "SELECT id FROM lots WHERE item_id=1 AND status='expired'")}
    c.close()
    assert swept_item1 and swept_item1.issubset(set(swept["expired_ids"]))

    out = service.confirm_intent(detail["intent_id"])

    c = connect()
    lot = dict(c.execute("SELECT * FROM lots WHERE id=?", (out["lot_id"],)).fetchone())
    assert lot["qty_in"] == 4 and lot["qty_remain"] == 4   # 仍是登记缺口，非重算
    c.close()


# --- 需求 8：缺口量非正数，确认失败 ---------------------------------------

def test_confirm_non_positive_short_400(db):
    c = connect()
    c.execute(
        "INSERT INTO shortfall_intents(item_id,demand_qty,short_qty,status,created_at)"
        " VALUES (1,1,0,'pending',?)", ("2026-10-05T00:00:00+00:00",))
    c.commit()
    iid = c.execute("SELECT id FROM shortfall_intents WHERE short_qty=0").fetchone()["id"]
    lots_before = _count(c, "lots")
    c.close()

    with pytest.raises(HTTPException) as ei:
        service.confirm_intent(iid)
    assert ei.value.status_code == 400

    c = connect()
    assert _count(c, "lots") == lots_before
    assert c.execute("SELECT status FROM shortfall_intents WHERE id=?",
                     (iid,)).fetchone()["status"] == "pending"
    c.close()


# --- 附加边界 -------------------------------------------------------------

def test_confirm_unknown_id_404(db):
    with pytest.raises(HTTPException) as ei:
        service.confirm_intent(9999)
    assert ei.value.status_code == 404


def test_qty_non_positive_no_intent(db):
    with pytest.raises(HTTPException) as ei:
        main.consume(main.ConsumeIn(item_id=1, qty=0))
    assert ei.value.status_code == 400
    c = connect()
    assert _count(c, "shortfall_intents") == 0
    c.close()


def test_unique_pending_index_raw(db):
    c = connect()
    c.execute(
        "INSERT INTO shortfall_intents(item_id,demand_qty,short_qty,status,created_at)"
        " VALUES (1,1,1,'pending',?)", ("2026-10-05T00:00:00+00:00",))
    c.commit()
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(
            "INSERT INTO shortfall_intents(item_id,demand_qty,short_qty,status,created_at)"
            " VALUES (1,2,2,'pending',?)", ("2026-10-05T00:00:01+00:00",))
        c.commit()
    c.close()


def test_concurrent_double_confirm_inserts_one_lot(db):
    detail = _consume_short(1, 7)
    iid = detail["intent_id"]
    c = connect(); lots_before = _count(c, "lots"); c.close()

    barrier = threading.Barrier(2)
    results, errors = [], []

    def worker():
        barrier.wait()
        try:
            results.append(service.confirm_intent(iid))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert sorted(r["already_confirmed"] for r in results) == [False, True]
    assert results[0]["lot_id"] == results[1]["lot_id"]
    c = connect()
    assert _count(c, "lots") == lots_before + 1
    c.close()


def test_consume_success_still_deducts_and_logs(db):
    c = connect(); before = _count(c, "consumptions"); c.close()
    result = main.consume(main.ConsumeIn(item_id=1, qty=2))
    assert result["ok"] is True
    c = connect()
    assert _count(c, "consumptions") == before + 1
    # 临期批 2026-09-28（1 盒）先扣完 → consumed；再从 2026-10-01 扣 1
    assert dict(c.execute(
        "SELECT * FROM lots WHERE item_id=1 AND expiry='2026-09-28'").fetchone())["status"] == "consumed"
    assert dict(c.execute(
        "SELECT * FROM lots WHERE item_id=1 AND expiry='2026-10-01'").fetchone())["qty_remain"] == 1
    c.close()
