"""Time-based work, checked every minute (India time):

  07:00 on the shop's salesman day  -> propose the order (Reason + Propose)
  08:00 daily                       -> send reminders that fall due (return windows)
  10:00 daily                       -> ask the next queued yes/no question (max three a day)

Each job runs once per shop per day (job_run table), so restarts don't repeat messages.
"""
from __future__ import annotations

import logging
import threading
import time

from . import agent, clock, ledger
from .agent import YES_NO, Reply
from .db import rows
from .tools.owner import promote_next

log = logging.getLogger("munshi.scheduler")


def _claim(conn, shop_id: str, job: str) -> bool:
    cur = conn.execute("INSERT OR IGNORE INTO job_run VALUES (?,?,?)", (shop_id, job, clock.today().isoformat()))
    return cur.rowcount == 1


def tick(worker) -> list[tuple[str, str]]:
    """Run whatever is due now. Returns (shop_id, job) pairs that ran, for logs and tests."""
    conn, lock = worker.conn, worker.lock
    now = clock.now()
    ran = []
    with lock:
        shops = rows(conn, "SELECT * FROM shop WHERE onboarding_step IS NULL")
    for shop in shops:
        sid = shop["shop_id"]
        if now.strftime("%A") == (shop["salesman_day"] or "") and now.hour >= 7:
            with lock:
                # A shop with no bills or counts yet has nothing to order from; don't nag it weekly.
                due = bool(ledger.stocked_items(conn, sid)) and _claim(conn, sid, "morning_order")
                replies = agent.morning(conn, sid) if due else []
            if due:
                worker.proactive(shop, replies)
                ran.append((sid, "morning_order"))
        if now.hour >= 8:
            with lock:
                due = _claim(conn, sid, "reminders")
                rem = rows(conn, "SELECT * FROM reminder WHERE shop_id=? AND status='scheduled' AND due_on<=?",
                           sid, now.date().isoformat()) if due else []
                replies = agent.run_job(conn, sid, "⏰ Reminders", lambda: [
                    "Reminder: " + "; ".join(r["reason"] for r in rem) + " — the return window closes today."
                ]) if rem else []
                for r in rem:
                    conn.execute("UPDATE reminder SET status='sent' WHERE id=?", (r["id"],))
            if replies:
                worker.proactive(shop, replies)
                ran.append((sid, "reminders"))
        if now.hour >= 10:
            with lock:
                due = _claim(conn, sid, "questions")
                q = promote_next(conn, sid) if due else None
                replies = agent.run_job(conn, sid, "⏰ Today's question", lambda: [Reply(q["text"], YES_NO)]) if q else []
            if replies:
                worker.proactive(shop, replies)
                ran.append((sid, "questions"))
    return ran


def start(worker, interval: int = 60) -> threading.Thread:
    def loop():
        while True:
            try:
                tick(worker)
            except Exception:  # noqa: BLE001 - keep the scheduler alive
                log.exception("scheduler tick failed")
            time.sleep(interval)

    t = threading.Thread(target=loop, name="munshi-scheduler", daemon=True)
    t.start()
    return t

