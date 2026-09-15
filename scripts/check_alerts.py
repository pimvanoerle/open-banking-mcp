#!/usr/bin/env python3
"""Banking alert checker — runs after each daily sync.

Reads the SQLite cache directly (no MCP server needed) and:
  - Tier 1 (immediate Slack DM to group chat): single transaction ≥ £500,
    new payee + amount ≥ £100, same merchant 3+ times in 24h.
  - Tier 2 (flag for morning digest): new direct debits / standing orders,
    balance drops > £300 in 24h.

State is persisted in ~/.open-banking-mcp/alerts_state.json so we don't
re-alert on the same transaction.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

from open_banking_mcp.auth import TokenManager
from open_banking_mcp.config import load_settings
from open_banking_mcp.storage import build_store

# ── Config ────────────────────────────────────────────────────────────────────

HOME = Path.home()
OB_DIR = HOME / ".open-banking-mcp"
CACHE_DB = OB_DIR / "cache-production.db"
STATE_FILE = OB_DIR / "alerts_state.json"
PENDING_FILE = OB_DIR / "pending_alerts.json"

TIER1_AMOUNT = 500.0      # £ — absolute value, both incoming and outgoing
TIER1_NEW_PAYEE = 100.0   # £ — new merchant, lower bar
TIER1_REPEAT_N = 3        # same merchant this many times in 24h

TIER2_BALANCE_DROP = 300.0  # £ drop in available balance in 24h

CONSENT_AMBER_DAYS = 21
CONSENT_RED_DAYS = 7
CONSENT_AMBER_NUDGE_INTERVAL = timedelta(days=3)  # red nudges every run (script is daily)

# ── Slack ─────────────────────────────────────────────────────────────────────

def _load_slack_token() -> str:
    env_path = HOME / "ipinch-bot" / ".env"
    for line in env_path.read_text().splitlines():
        m = line.strip()
        if m.startswith("SLACK_BOT_TOKEN="):
            return m.split("=", 1)[1].strip().strip("'\"")
    raise RuntimeError("SLACK_BOT_TOKEN not found in ~/ipinch-bot/.env")

GROUP_CHAT = "C0AJZBKFFSP"

def slack_send(text: str) -> None:
    token = _load_slack_token()
    body = json.dumps({"channel": GROUP_CHAT, "text": text}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    if not data.get("ok"):
        raise RuntimeError(f"Slack error: {data.get('error')}")

# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "last_checked_at": None,
            "seen_tx_ids": [],
            "known_payees": [],
        }

def save_state(state: dict) -> None:
    OB_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))

def load_pending() -> list:
    try:
        return json.loads(PENDING_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def save_pending(alerts: list) -> None:
    OB_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_FILE.write_text(json.dumps(alerts, indent=2))

# ── DB helpers ────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(CACHE_DB)
    conn.row_factory = sqlite3.Row
    return conn

def fetch_new_transactions(since: str | None) -> list[dict]:
    if not CACHE_DB.exists():
        return []
    conn = _conn()
    try:
        if since:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE synced_at > ? AND is_pending = 0"
                " ORDER BY timestamp DESC",
                (since,),
            ).fetchall()
        else:
            # First run — look at last 48h
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
            rows = conn.execute(
                "SELECT * FROM transactions WHERE timestamp >= ? AND is_pending = 0"
                " ORDER BY timestamp DESC",
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def fetch_transactions_occurring_since(since: str) -> list[dict]:
    """Transactions whose *transaction date* falls after `since` — unlike
    fetch_new_transactions, not affected by when a bulk/backfill sync happened."""
    if not CACHE_DB.exists():
        return []
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE timestamp >= ? AND is_pending = 0"
            " ORDER BY timestamp DESC",
            (since,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def fetch_balance_snapshots() -> list[dict]:
    """Return all balance snapshots, newest first."""
    if not CACHE_DB.exists():
        return []
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT payload, synced_at FROM snapshots WHERE kind = 'balance'"
            " ORDER BY synced_at DESC"
        ).fetchall()
        out = []
        for r in rows:
            data = json.loads(r["payload"])
            data["_synced_at"] = r["synced_at"]
            out.append(data)
        return out
    finally:
        conn.close()

def fetch_snapshot(kind: str) -> list[dict]:
    if not CACHE_DB.exists():
        return []
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT payload, synced_at FROM snapshots WHERE kind = ? ORDER BY synced_at DESC",
            (kind,),
        ).fetchall()
        return [{"payload": json.loads(r["payload"]), "synced_at": r["synced_at"]} for r in rows]
    finally:
        conn.close()

# ── Formatting ────────────────────────────────────────────────────────────────

def _sign(amount: float) -> str:
    return "+" if amount >= 0 else "-"

def _fmt(amount: float, currency: str = "GBP") -> str:
    symbol = {"GBP": "£", "EUR": "€", "USD": "$"}.get(currency, currency + " ")
    return f"{_sign(amount)}{symbol}{abs(amount):,.2f}"

def _payee(tx: dict) -> str:
    return tx.get("merchant") or tx.get("description") or "(unknown)"

# ── Alert checks ──────────────────────────────────────────────────────────────

def check_transactions(txs: list[dict], state: dict) -> tuple[list[str], list[str]]:
    """Return (tier1_messages, tier2_messages) for new transactions."""
    seen = set(state.get("seen_tx_ids", []))
    known_payees = set(state.get("known_payees", []))
    tier1, tier2 = [], []

    # Track same-merchant counts in last 24h across *all* transactions, not just new ones
    now = datetime.now(timezone.utc)
    cutoff_24h = (now - timedelta(hours=24)).isoformat()
    merchant_counts: dict[str, int] = {}
    for tx in fetch_transactions_occurring_since(cutoff_24h):
        m = _payee(tx)
        merchant_counts[m] = merchant_counts.get(m, 0) + 1

    new_seen = set()
    new_payees = set()

    for tx in txs:
        tx_id = tx.get("transaction_id") or tx.get("normalised_provider_transaction_id") or ""
        if tx_id and tx_id in seen:
            continue

        amount = tx.get("amount") or 0.0
        currency = tx.get("currency") or "GBP"
        payee = _payee(tx)
        ts = tx.get("timestamp", "")[:10]

        if tx_id:
            new_seen.add(tx_id)

        # Tier 1: large transaction (outgoing or incoming)
        if abs(amount) >= TIER1_AMOUNT:
            direction = "incoming" if amount > 0 else "outgoing"
            tier1.append(
                f"💳 *Large {direction} transaction*: {_fmt(amount, currency)} "
                f"— {payee} ({ts})"
            )

        # Tier 1: new payee above lower threshold
        elif abs(amount) >= TIER1_NEW_PAYEE and payee not in known_payees:
            tier1.append(
                f"🆕 *New payee*: {_fmt(amount, currency)} to *{payee}* — first time seeing this merchant ({ts})"
            )
            new_payees.add(payee)

        # Track known payees (all processed transactions, even below threshold)
        known_payees.add(payee)
        new_payees.add(payee)

    # Tier 1: repeat merchant in 24h
    for merchant, count in merchant_counts.items():
        if count >= TIER1_REPEAT_N:
            tier1.append(
                f"🔁 *Repeated charge*: *{merchant}* appeared {count}× in the last 24h — worth a check"
            )

    # Update state
    state["seen_tx_ids"] = list(seen | new_seen)[-2000:]  # cap to avoid unbounded growth
    state["known_payees"] = list(known_payees)

    return tier1, tier2

def check_balance_drop(state: dict) -> list[str]:
    """Tier 2: flag if available balance dropped more than £300 since last check."""
    snapshots = fetch_balance_snapshots()
    if len(snapshots) < 2:
        return []

    latest = snapshots[0]
    previous = snapshots[1]

    prev_avail = previous.get("available") if isinstance(previous, dict) else None
    curr_avail = latest.get("available") if isinstance(latest, dict) else None

    if prev_avail is None or curr_avail is None:
        return []

    drop = prev_avail - curr_avail
    if drop >= TIER2_BALANCE_DROP:
        return [
            f"📉 *Balance drop*: available balance fell by £{drop:,.2f} since yesterday "
            f"(£{prev_avail:,.2f} → £{curr_avail:,.2f})"
        ]
    return []

def check_consent_expiry(state: dict) -> list[str]:
    """Nudge before a bank consent lapses. Amber (<=21 days) nudges every
    CONSENT_AMBER_NUDGE_INTERVAL; red (<=7 days) nudges every run since this
    script itself only runs once a day. Clears its own throttle record once
    a provider is renewed, so the next expiry cycle nudges fresh."""
    settings, store = load_settings(), None
    try:
        store = build_store(settings)
        rows = TokenManager(settings, store).consent_status()
    except Exception as exc:
        return [f"⚠️ Could not check bank consent expiry: {exc}"]

    nudges = state.setdefault("consent_nudges", {})
    now = datetime.now(timezone.utc)
    messages = []

    seen_providers = {provider_id for provider_id, _, _ in rows}
    for stale in list(nudges):
        if stale not in seen_providers:
            del nudges[stale]

    for provider_id, days_left, expires_at in rows:
        if days_left <= CONSENT_RED_DAYS:
            tier = "red"
        elif days_left <= CONSENT_AMBER_DAYS:
            tier = "amber"
        else:
            nudges.pop(provider_id, None)
            continue

        record = nudges.get(provider_id)
        if tier == "amber" and record and record.get("tier") == "amber":
            last_sent = datetime.fromisoformat(record["last_sent"])
            if now - last_sent < CONSENT_AMBER_NUDGE_INTERVAL:
                continue
        # red: nudge every run (once/day, since the script itself is daily)

        icon = "🚨" if tier == "red" else "⚠️"
        messages.append(
            f"{icon} *Bank consent expiring*: {provider_id} — {days_left} day(s) left "
            f"(until {expires_at:%Y-%m-%d}). Run `open-banking-mcp auth` to renew."
        )
        nudges[provider_id] = {"tier": tier, "last_sent": now.isoformat()}

    return messages

# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if not CACHE_DB.exists():
        print("Cache not found — skipping alert check (sync hasn't run yet)")
        return

    state = load_state()
    pending = load_pending()
    now_iso = datetime.now(timezone.utc).isoformat()

    txs = fetch_new_transactions(state.get("last_checked_at"))
    print(f"Checking {len(txs)} new transaction(s) since {state.get('last_checked_at') or 'first run'}")

    tier1_msgs, _ = check_transactions(txs, state)
    tier2_msgs = check_balance_drop(state)

    if tier1_msgs:
        lines = ["🦀 *Banking alert*"] + tier1_msgs
        slack_send("\n".join(lines))
        print(f"Sent {len(tier1_msgs)} Tier 1 alert(s) to Slack")
    else:
        print("No Tier 1 alerts")

    if tier2_msgs:
        pending.extend(tier2_msgs)
        save_pending(pending)
        print(f"Added {len(tier2_msgs)} Tier 2 alert(s) to pending digest")
    else:
        print("No Tier 2 alerts")

    consent_msgs = check_consent_expiry(state)
    if consent_msgs:
        slack_send("\n".join(consent_msgs))
        print(f"Sent {len(consent_msgs)} consent-expiry nudge(s) to Slack")
    else:
        print("No consent-expiry nudges due")

    state["last_checked_at"] = now_iso
    save_state(state)

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Alert check error: {exc}", file=sys.stderr)
        sys.exit(1)
