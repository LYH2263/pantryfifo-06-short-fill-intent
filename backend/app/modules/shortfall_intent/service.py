"""Shortfall-intent persistence and confirmation.

A pending intent records the item and the frozen shortfall qty captured at the
moment a FEFO consume failed with reason="short". It occupies no stock: no lot
row exists until confirmation, so pending intents never enter FEFO candidates.
Confirmation inserts exactly one inbound lot with the frozen shortfall qty and
is idempotent.
"""
import json
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException

from app.db import connect, immediate

DEFAULT_EXPIRY_DAYS = 30

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def register_shortfall(c, *, item_id: int, layer: str | None,
                       demand_qty: float, short_qty: float,
                       deductions: list[dict]) -> dict:
    """Insert-or-merge the single pending intent of an item (latest snapshot wins).

    Runs inside the caller's open transaction; `c` is expected to be a
    connection already holding an IMMEDIATE lock.
    """
    row = c.execute(
        """INSERT INTO shortfall_intents
             (item_id, layer, demand_qty, short_qty, status, deductions_json, created_at)
           VALUES (?,?,?,?,'pending',?,?)
           ON CONFLICT(item_id) WHERE status='pending'
           DO UPDATE SET demand_qty=excluded.demand_qty,
                         short_qty=excluded.short_qty,
                         deductions_json=excluded.deductions_json
           RETURNING id""",
        (item_id, layer, float(demand_qty), float(short_qty),
         json.dumps(deductions), _now()),
    ).fetchone()
    return {"id": row["id"]}

def _row_to_dict(r) -> dict:
    d = dict(r)
    try:
        d["deductions"] = json.loads(d.pop("deductions_json") or "[]")
    except (ValueError, TypeError):
        d["deductions"] = []
    return d

def list_intents(status: str | None = None) -> list[dict]:
    c = connect()
    q = """SELECT s.*, i.name, i.unit
           FROM shortfall_intents s
           LEFT JOIN items i ON i.id=s.item_id"""
    args = []
    if status:
        q += " WHERE s.status=?"; args.append(status)
    q += " ORDER BY s.created_at DESC, s.id DESC"
    rows = [_row_to_dict(r) for r in c.execute(q, args)]
    c.close()
    return rows

def _resolve_expiry(expiry: str | None) -> str:
    if expiry is None or expiry == "":
        return (date.today() + timedelta(days=DEFAULT_EXPIRY_DAYS)).isoformat()
    try:
        return date.fromisoformat(expiry).isoformat()
    except (ValueError, TypeError):
        raise HTTPException(400, "bad_expiry")

def confirm_intent(intent_id: int, expiry: str | None = None) -> dict:
    """Insert one inbound lot covering the frozen shortfall. Idempotent."""
    with immediate() as c:
        row = c.execute(
            "SELECT * FROM shortfall_intents WHERE id=?", (intent_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "intent")
        intent = dict(row)
        if intent["status"] == "confirmed":
            lot = c.execute("SELECT expiry FROM lots WHERE id=?",
                            (intent["lot_id"],)).fetchone()
            return {"id": intent_id, "status": "confirmed",
                    "lot_id": intent["lot_id"], "already_confirmed": True,
                    "qty_in": intent["short_qty"],
                    "expiry": lot["expiry"] if lot else None}
        short_qty = float(intent["short_qty"])
        if short_qty <= 0:
            raise HTTPException(400, "short_qty_non_positive")
        exp = _resolve_expiry(expiry)
        cur = c.execute(
            "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality)"
            " VALUES (?,?,?,?,?,?)",
            (intent["item_id"], short_qty, short_qty, exp, "on_shelf", "clean"))
        lot_id = cur.lastrowid
        cas = c.execute(
            """UPDATE shortfall_intents
                  SET status='confirmed', lot_id=?, confirmed_at=?
                WHERE id=? AND status='pending'""",
            (lot_id, _now(), intent_id))
        if cas.rowcount != 1:
            # Lost a race despite the IMMEDIATE lock: undo the lot, never orphan it.
            raise HTTPException(409, "intent_not_pending")
        return {"id": intent_id, "status": "confirmed", "lot_id": lot_id,
                "already_confirmed": False, "qty_in": short_qty, "expiry": exp}
