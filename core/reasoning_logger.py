"""
core/reasoning_logger.py — Agent Reasoning Logger (GitHub Security Principle #3)

Principle: "Log everything."

Captures a full forensic record of every agent decision — not just the outcome
but the reasoning chain: strategy, edge %, market signals, stage, outcome.

Logs to:
  - SQLite: reasoning_log table in polybot.db
  - JSON file: data/reasoning_log.json (rolling, last 500 entries)

Based on: https://github.blog/ai-and-ml/generative-ai/
          under-the-hood-security-architecture-of-github-agentic-workflows/
"""

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional, Any

logger = logging.getLogger("polybot.reasoning")

_DB_PATH: Optional[str] = None
_JSON_PATH: Optional[str] = None
_MAX_JSON_ENTRIES = 500


def init_reasoning_logger(db_path: str, data_dir: str = "data") -> None:
    """Initialise the reasoning logger. Call once from main.py at startup."""
    global _DB_PATH, _JSON_PATH
    _DB_PATH = db_path
    _JSON_PATH = str(Path(data_dir) / "reasoning_log.json")

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reasoning_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                logged_at     TEXT NOT NULL,
                strategy      TEXT,
                market_question TEXT,
                market_type   TEXT,
                token_id      TEXT,
                side          TEXT,
                price         REAL,
                size_usd      REAL,
                edge_pct      REAL,
                signals       TEXT,
                stage         TEXT,
                outcome       TEXT,
                reject_reason TEXT,
                order_id      TEXT,
                dry_run       INTEGER,
                duration_ms   REAL,
                extra         TEXT
            )
        """)
        conn.commit()
        conn.close()
        logger.info(f"ReasoningLogger: table ready in {db_path}")
    except Exception as exc:
        logger.warning(f"ReasoningLogger: could not init DB table — {exc}")

    Path(data_dir).mkdir(parents=True, exist_ok=True)


def log_signal(
    strategy: str,
    market_question: str,
    market_type: str,
    token_id: str,
    side: str,
    price: float,
    size_usd: float,
    edge_pct: float,
    signals: dict,
    stage: str = "signal_detected",
    extra: Optional[dict] = None,
) -> None:
    """Log a raw signal before any staging or validation."""
    _write_entry(
        strategy=strategy,
        market_question=market_question,
        market_type=market_type,
        token_id=token_id,
        side=side,
        price=price,
        size_usd=size_usd,
        edge_pct=edge_pct,
        signals=signals,
        stage=stage,
        outcome="pending",
        reject_reason=None,
        order_id=None,
        dry_run=None,
        extra=extra,
    )


def log_order_decision(order: Any) -> None:
    """Log the final staging decision for a StagedOrder."""
    if order.validated and order.order_result:
        stage = "executed" if order.order_result["success"] else "exec_failed"
        outcome = "success" if order.order_result["success"] else "failed"
        order_id = order.order_result.get("order_id")
        reject_reason = order.order_result.get("error")
    elif order.rejected:
        stage = "rejected"
        outcome = "rejected"
        order_id = None
        reject_reason = order.reject_reason
    else:
        stage = "unknown"
        outcome = "unknown"
        order_id = None
        reject_reason = None

    staged_at = getattr(order, "staged_at", time.time())
    duration_ms = (time.time() - staged_at) * 1000

    _write_entry(
        strategy=getattr(order, "strategy", "unknown"),
        market_question=getattr(order, "market_question", ""),
        market_type=getattr(order, "market_type", "free"),
        token_id=getattr(order, "token_id", ""),
        side=getattr(order, "side", ""),
        price=getattr(order, "price", 0.0),
        size_usd=getattr(order, "size_usd", 0.0),
        edge_pct=getattr(order, "edge_pct", 0.0),
        signals={},
        stage=stage,
        outcome=outcome,
        reject_reason=reject_reason,
        order_id=order_id,
        dry_run=int(getattr(order, "dry_run", True)),
        duration_ms=duration_ms,
    )


def log_cycle_summary(cycle_num: int, duration_s: float, staging_summary: dict) -> None:
    """Log a summary record for the full scan cycle."""
    _write_entry(
        strategy="__cycle__",
        market_question=f"Cycle #{cycle_num}",
        market_type="",
        token_id="",
        side="",
        price=0.0,
        size_usd=staging_summary.get("total_usd", 0.0),
        edge_pct=0.0,
        signals={},
        stage="cycle_complete",
        outcome="ok",
        reject_reason=None,
        order_id=None,
        dry_run=None,
        duration_ms=duration_s * 1000,
        extra=staging_summary,
    )


def get_recent_decisions(n: int = 20) -> list:
    """Return the last N reasoning log entries from SQLite."""
    if not _DB_PATH:
        return []
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM reasoning_log ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.debug(f"ReasoningLogger: get_recent_decisions error — {exc}")
        return []


def get_rejection_breakdown(last_n_hours: int = 10) -> dict:
    """Return rejection reason counts — useful for auto-tuning thresholds."""
    if not _DB_PATH:
        return {}
    try:
        conn = sqlite3.connect(_DB_PATH)
        rows = conn.execute(
            """
            SELECT reject_reason, COUNT(*) as cnt
            FROM reasoning_log
            WHERE stage = 'rejected' AND logged_at > datetime('now', ?)
            GROUP BY reject_reason ORDER BY cnt DESC
            """,
            (f"-{last_n_hours} hours",),
        ).fetchall()
        conn.close()
        return {r[0]: r[1] for r in rows if r[0]}
    except Exception as exc:
        logger.debug(f"ReasoningLogger: rejection breakdown error — {exc}")
        return {}


def _write_entry(
    strategy, market_question, market_type, token_id, side,
    price, size_usd, edge_pct, signals, stage, outcome,
    reject_reason, order_id, dry_run, duration_ms=None, extra=None,
) -> None:
    """Write a single entry to SQLite + JSON log."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    row = {
        "logged_at": now,
        "strategy": strategy,
        "market_question": market_question[:200] if market_question else "",
        "market_type": market_type,
        "token_id": token_id[:64] if token_id else "",
        "side": side,
        "price": price,
        "size_usd": size_usd,
        "edge_pct": edge_pct,
        "signals": json.dumps(signals) if signals else "{}",
        "stage": stage,
        "outcome": outcome,
        "reject_reason": reject_reason,
        "order_id": order_id,
        "dry_run": dry_run,
        "duration_ms": duration_ms,
        "extra": json.dumps(extra) if extra else None,
    }

    logger.debug(
        f"[{stage.upper()}] {strategy} | {side} outcome={outcome}"
    )

    if _DB_PATH:
        try:
            conn = sqlite3.connect(_DB_PATH)
            conn.execute(
                """INSERT INTO reasoning_log
                   (logged_at, strategy, market_question, market_type, token_id, side,
                    price, size_usd, edge_pct, signals, stage, outcome, reject_reason,
                    order_id, dry_run, duration_ms, extra)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    row["logged_at"], row["strategy"], row["market_question"],
                    row["market_type"], row["token_id"], row["side"],
                    row["price"], row["size_usd"], row["edge_pct"],
                    row["signals"], row["stage"], row["outcome"],
                    row["reject_reason"], row["order_id"], row["dry_run"],
                    row["duration_ms"], row["extra"],
                ),
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.warning(f"ReasoningLogger SQLite write failed: {exc}")

    if _JSON_PATH:
        try:
            path = Path(_JSON_PATH)
            entries: list = []
            if path.exists():
                with open(path) as f:
                    entries = json.load(f)
            entries.append(row)
            if len(entries) > _MAX_JSON_ENTRIES:
                entries = entries[-_MAX_JSON_ENTRIES:]
            with open(path, "w") as f:
                json.dump(entries, f, indent=2)
        except Exception as exc:
            logger.debug(f"ReasoningLogger JSON write failed (non-fatal): {exc}")
