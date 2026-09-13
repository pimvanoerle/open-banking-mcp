#!/usr/bin/env python3
"""Weekly banking summary — runs Sunday mornings.

Reads the SQLite cache and sends a spending breakdown to the group chat:
  - Total spend this week vs 4-week average
  - Top categories and merchants
  - Any new recurring charges spotted
  - Notable incoming payments
"""

from __future__ import annotations

import json
import sqlite3
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

HOME = Path.home()
OB_DIR = HOME / ".open-banking-mcp"
CACHE_DB = OB_DIR / "cache-production.db"

GROUP_CHAT = "C0AJZBKFFSP"
ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"

# ── Slack / Claude ────────────────────────────────────────────────────────────

def _load_env() -> dict[str, str]:
    env_path = HOME / "ipinch-bot" / ".env"
    out: dict[str, str] = {}
    for line in env_path.read_text().splitlines():
        m = line.strip()
        if "=" in m and not m.startswith("#"):
            k, v = m.split("=", 1)
            out[k.strip()] = v.strip().strip("'\"")
    return out

def slack_send(text: str, env: dict) -> None:
    token = env["SLACK_BOT_TOKEN"]
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

def call_claude(prompt: str, env: dict) -> str:
    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 800,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": env["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    if "error" in data:
        raise RuntimeError(f"Anthropic error: {data['error']['message']}")
    return data["content"][0]["text"]

# ── DB helpers ────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(CACHE_DB)
    conn.row_factory = sqlite3.Row
    return conn

def fetch_transactions(from_date: str, to_date: str) -> list[dict]:
    if not CACHE_DB.exists():
        return []
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT payload, amount, currency, description, merchant, category, timestamp"
            " FROM transactions"
            " WHERE timestamp >= ? AND timestamp <= ? AND is_pending = 0"
            " ORDER BY timestamp DESC",
            (from_date, to_date + "T23:59:59Z"),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

# ── Analysis ──────────────────────────────────────────────────────────────────

def week_bounds(weeks_ago: int = 0) -> tuple[str, str]:
    """Return (start, end) YYYY-MM-DD for the week ending last Sunday."""
    now = datetime.now(timezone.utc)
    # Days since last Sunday (Sunday = 6 in weekday())
    days_since_sunday = (now.weekday() + 1) % 7
    last_sunday = now - timedelta(days=days_since_sunday)
    end = last_sunday - timedelta(weeks=weeks_ago)
    start = end - timedelta(days=6)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

def summarise_txs(txs: list[dict]) -> dict:
    total_out = 0.0
    total_in = 0.0
    by_category: dict[str, float] = defaultdict(float)
    by_merchant: dict[str, float] = defaultdict(float)
    large_incoming: list[dict] = []

    for tx in txs:
        amt = tx.get("amount") or 0.0
        cat = tx.get("category") or "Other"
        merchant = tx.get("merchant") or tx.get("description") or "(unknown)"

        if amt < 0:
            total_out += abs(amt)
            by_category[cat] += abs(amt)
            by_merchant[merchant] += abs(amt)
        else:
            total_in += amt
            if amt >= 200:
                large_incoming.append({"merchant": merchant, "amount": amt})

    top_categories = sorted(by_category.items(), key=lambda x: x[1], reverse=True)[:5]
    top_merchants = sorted(by_merchant.items(), key=lambda x: x[1], reverse=True)[:5]

    return {
        "total_out": total_out,
        "total_in": total_in,
        "top_categories": top_categories,
        "top_merchants": top_merchants,
        "large_incoming": large_incoming,
        "tx_count": len(txs),
    }

def build_prompt(this_week: dict, avg_4w: dict, week_label: str) -> str:
    def cat_lines(cats):
        return "\n".join(f"  - {cat}: £{amt:,.2f}" for cat, amt in cats) or "  (none)"

    def merchant_lines(merchants):
        return "\n".join(f"  - {m}: £{amt:,.2f}" for m, amt in merchants) or "  (none)"

    def incoming_lines(items):
        return "\n".join(f"  - £{i['amount']:,.2f} from {i['merchant']}" for i in items) or "  (none)"

    vs_avg = this_week["total_out"] - avg_4w["total_out"]
    vs_label = f"£{abs(vs_avg):,.2f} {'more' if vs_avg > 0 else 'less'} than the 4-week average"

    return f"""You are iPinch, a cheerful kawaii cartoon crab assistant.
Write a concise, friendly weekly spending summary for Pim ({week_label}).
Keep it warm but financially honest — like a helpful friend who tracks the numbers.

This week's spending:
- Total out: £{this_week['total_out']:,.2f} ({vs_label})
- Total in: £{this_week['total_in']:,.2f}
- Transactions: {this_week['tx_count']}

Top categories (this week):
{cat_lines(this_week['top_categories'])}

Top merchants (this week):
{merchant_lines(this_week['top_merchants'])}

Notable incoming payments:
{incoming_lines(this_week['large_incoming'])}

4-week average weekly spend: £{avg_4w['total_out']:,.2f}

Write 3-5 sentences max. Note if spending is unusually high or low in any category.
Highlight any large incoming amounts. End with a brief encouraging note. Sign off as iPinch 🦀"""

# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if not CACHE_DB.exists():
        print("Cache not found — skipping weekly summary")
        return

    env = _load_env()

    # This week (Mon–Sun just ended)
    w_start, w_end = week_bounds(0)
    week_label = f"{w_start} to {w_end}"
    this_week_txs = fetch_transactions(w_start, w_end)
    this_week = summarise_txs(this_week_txs)

    # 4-week average for comparison
    all_4w_txs = []
    for i in range(1, 5):
        s, e = week_bounds(i)
        all_4w_txs.extend(fetch_transactions(s, e))
    avg_4w_raw = summarise_txs(all_4w_txs)
    # Divide by 4 for per-week average
    avg_4w = {**avg_4w_raw, "total_out": avg_4w_raw["total_out"] / 4, "total_in": avg_4w_raw["total_in"] / 4}

    print(f"Week {week_label}: £{this_week['total_out']:,.2f} out, {this_week['tx_count']} transactions")

    if this_week["tx_count"] == 0:
        print("No transactions this week — skipping summary")
        return

    prompt = build_prompt(this_week, avg_4w, week_label)
    summary = call_claude(prompt, env)

    header = f"📊 *Weekly spending summary — {week_label}*\n\n"
    slack_send(header + summary, env)
    print("Weekly summary sent to Slack")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Weekly summary error: {exc}", file=sys.stderr)
        sys.exit(1)
