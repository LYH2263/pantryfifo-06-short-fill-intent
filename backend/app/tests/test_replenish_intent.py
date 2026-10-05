"""补位意图全链路:短量登记 → 未确认不冻结 → 确认补入恰好一次。

只用标准库 + 引擎 + 模块,镜像 main.py 端点里的同一组 SQL/调用序列,
因此无需 FastAPI 即可运行(与 test_fefo.py 同为引擎/模块级测试)。
"""
import os
import tempfile

from app import seed
from app.db import connect
from app.engines.fefo import consume_fefo, expire_lots
from app.modules import replenish_intent as replenish


def _conn():
    """全新临时库:建表 + 种子品项,清空批次与意图,用受控数据。"""
    os.environ["DATA_DIR"] = tempfile.mkdtemp()
    seed.init_db()
    c = connect()
    c.execute("DELETE FROM lots")
    c.execute("DELETE FROM replenish_intents")
    c.commit()
    return c


def _add_lot(c, item_id, qty, expiry):
    c.execute(
        "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
        (item_id, qty, qty, expiry, "on_shelf", "clean"),
    )
    c.commit()


def _consume(c, item_id, qty):
    """镜像 main.py /api/consume 的事务序列,返回 (result, intent_id|None)。"""
    lots = [dict(r) for r in c.execute(
        "SELECT * FROM lots WHERE item_id=? AND status='on_shelf' AND qty_remain>0", (item_id,))]
    result = consume_fefo(lots, qty)
    if not result["ok"]:
        if result["reason"] == "short":
            intent_id = replenish.register(c, item_id, result["short"])
            c.commit()
            return result, intent_id
        return result, None
    for d in result["deductions"]:
        c.execute("UPDATE lots SET qty_remain = qty_remain - ? WHERE id=?", (d["take"], d["lot_id"]))
        rem = c.execute("SELECT qty_remain FROM lots WHERE id=?", (d["lot_id"],)).fetchone()["qty_remain"]
        if rem <= 0:
            c.execute("UPDATE lots SET status='consumed', qty_remain=0 WHERE id=?", (d["lot_id"],))
    c.commit()
    return result, None


def _fridge(c):
    """镜像 main.py /api/fridge 的全层查询。"""
    return [dict(r) for r in c.execute(
        """SELECT lots.*, items.name, items.layer, items.unit FROM lots
           JOIN items ON items.id=lots.item_id WHERE lots.status='on_shelf'""")]


def _sweep(c, today):
    """镜像 main.py /api/expire-sweep。"""
    lots = [dict(r) for r in c.execute("SELECT * FROM lots WHERE status='on_shelf'")]
    ids = expire_lots(lots, today)
    for i in ids:
        c.execute("UPDATE lots SET status='expired' WHERE id=?", (i,))
    c.commit()
    return ids


def test_failed_consume_registers_intent_and_keeps_layers():
    c = _conn()
    _add_lot(c, 1, 2, "2026-11-01")
    before = [(l["id"], l["qty_remain"], l["status"]) for l in _fridge(c)]
    result, intent_id = _consume(c, 1, 5)
    assert result["ok"] is False and result["reason"] == "short" and result["short"] == 3
    # 失败当次全层余量保持失败前
    after = [(l["id"], l["qty_remain"], l["status"]) for l in _fridge(c)]
    assert after == before
    # 登记品项 + 缺口量,未确认
    row = c.execute("SELECT * FROM replenish_intents WHERE id=?", (intent_id,)).fetchone()
    assert row["item_id"] == 1 and row["qty_short"] == 3
    assert row["status"] == "pending" and row["lot_id"] is None
    c.close()


def test_confirm_inserts_one_lot_and_marks_fulfilled():
    c = _conn()
    intent_id = replenish.register(c, 1, 3)
    c.commit()
    out = replenish.confirm(c, intent_id, "2026-12-01")
    c.commit()
    assert out["ok"] and out["qty"] == 3 and out["status"] == "fulfilled"
    lot = c.execute("SELECT * FROM lots WHERE id=?", (out["lot_id"],)).fetchone()
    assert lot["qty_in"] == 3 and lot["qty_remain"] == 3
    assert lot["status"] == "on_shelf" and lot["expiry"] == "2026-12-01"
    intent = c.execute("SELECT * FROM replenish_intents WHERE id=?", (intent_id,)).fetchone()
    assert intent["status"] == "fulfilled" and intent["lot_id"] == out["lot_id"] and intent["confirmed_at"]
    # 全层看见新批
    assert any(l["id"] == out["lot_id"] for l in _fridge(c))
    # 同一意图第二次确认不得再增一批
    try:
        replenish.confirm(c, intent_id, "2026-12-01")
        assert False, "second confirm must fail"
    except replenish.AlreadyFulfilled:
        c.rollback()
    assert c.execute("SELECT COUNT(*) n FROM lots").fetchone()["n"] == 1
    c.close()


def test_confirm_non_positive_short_fails():
    c = _conn()
    c.execute("INSERT INTO replenish_intents(item_id,qty_short,status,created_at) VALUES (1,0,'pending','2026-10-05T00:00:00+00:00')")
    bad_id = c.execute("SELECT id FROM replenish_intents").fetchone()["id"]
    c.commit()
    try:
        replenish.confirm(c, bad_id, "2026-12-01")
        assert False, "non-positive short must fail"
    except replenish.ShortNonPositive:
        c.rollback()
    # 不翻转、不插批
    assert c.execute("SELECT COUNT(*) n FROM lots").fetchone()["n"] == 0
    assert c.execute("SELECT status FROM replenish_intents WHERE id=?", (bad_id,)).fetchone()["status"] == "pending"
    # 登记入口同样拒绝非正缺口
    try:
        replenish.register(c, 1, -1)
        assert False, "non-positive register must fail"
    except replenish.ShortNonPositive:
        c.rollback()
    c.close()


def test_pending_intent_not_in_fefo_candidates():
    c = _conn()
    _add_lot(c, 1, 2, "2026-11-01")
    replenish.register(c, 1, 3)
    c.commit()
    # 意图不是批次:全层只有真实批次
    assert len(_fridge(c)) == 1
    # 未确认意图不进入按临期候选:再次消费仍短同样的量
    result, _ = _consume(c, 1, 5)
    assert result["ok"] is False and result["short"] == 3
    c.close()


def test_confirm_after_sweep_keeps_registered_shortfall():
    c = _conn()
    _add_lot(c, 1, 2, "2026-09-01")  # 已过期批
    result, intent_id = _consume(c, 1, 5)
    assert result["short"] == 3
    # 确认前下架扫把同品过期批带走
    ids = _sweep(c, "2026-10-05")
    assert len(ids) == 1
    out = replenish.confirm(c, intent_id, "2026-12-01")
    c.commit()
    # 新批仍按意图缺口 3 入库,不被改写成下架带走的 2
    lot = c.execute("SELECT * FROM lots WHERE id=?", (out["lot_id"],)).fetchone()
    assert lot["qty_in"] == 3 and lot["qty_remain"] == 3
    fridge = _fridge(c)
    assert len(fridge) == 1 and fridge[0]["id"] == out["lot_id"]
    c.close()


def test_second_consume_and_other_inbound_do_not_double_book():
    c = _conn()
    _add_lot(c, 1, 2, "2026-11-01")
    _, intent_id = _consume(c, 1, 5)          # 短 3,意图 pending,不冻结
    r2, _ = _consume(c, 1, 2)                 # 第二次消费只看见真实批次
    assert r2["ok"]
    _add_lot(c, 1, 4, "2026-12-01")           # 另一条同品入库,独立于意图
    out = replenish.confirm(c, intent_id, "2027-01-01")
    c.commit()
    assert out["qty"] == 3
    # 账目:他条入库 4 + 补位 3 在架;缺口 3 只物化一次,无一处占额度另一处又确认双开
    remain = c.execute("SELECT COALESCE(SUM(qty_remain),0) s FROM lots WHERE status='on_shelf'").fetchone()["s"]
    assert remain == 4 + 3
    assert c.execute("SELECT COUNT(*) n FROM lots WHERE qty_in=3").fetchone()["n"] == 1
    c.close()
