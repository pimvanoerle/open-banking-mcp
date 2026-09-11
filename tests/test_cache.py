from datetime import datetime, timedelta, timezone

from open_banking_mcp.cache import Cache, parse_ts


def _tx(tid, ts, amount, desc, merchant=None):
    return {
        "transaction_id": tid, "timestamp": ts, "amount": amount,
        "currency": "GBP", "description": desc, "merchant_name": merchant,
    }


def test_snapshot_roundtrip(tmp_path):
    c = Cache(tmp_path / "c.db")
    assert c.get_snapshot("mock", "accounts") is None

    c.put_snapshot("mock", "accounts", [{"account_id": "a1"}])
    payload, synced_at = c.get_snapshot("mock", "accounts")
    assert payload == [{"account_id": "a1"}]
    assert (datetime.now(timezone.utc) - synced_at) < timedelta(seconds=5)


def test_snapshots_are_keyed_per_account(tmp_path):
    c = Cache(tmp_path / "c.db")
    c.put_snapshot("mock", "balance", {"current": 1}, key="a1")
    c.put_snapshot("mock", "balance", {"current": 2}, key="a2")
    assert c.get_snapshot("mock", "balance", "a1")[0] == {"current": 1}
    assert c.get_snapshot("mock", "balance", "a2")[0] == {"current": 2}


def test_transactions_upsert_is_idempotent(tmp_path):
    c = Cache(tmp_path / "c.db")
    rows = [_tx("t1", "2026-09-01T00:00:00Z", -5.0, "COFFEE")]
    c.put_transactions("mock", "account", "a1", rows)
    c.put_transactions("mock", "account", "a1", rows)
    found, _ = c.transactions("mock", "a1")
    assert len(found) == 1


def test_pending_rows_are_replaced_not_merged(tmp_path):
    """Pending ids are unstable; stale ones must not linger as ghosts."""
    c = Cache(tmp_path / "c.db")
    c.put_transactions("mock", "account", "a1",
                       [_tx("p1", "2026-09-01T00:00:00Z", -5.0, "PENDING A")],
                       is_pending=True)
    c.put_transactions("mock", "account", "a1",
                       [_tx("p2", "2026-09-02T00:00:00Z", -6.0, "PENDING B")],
                       is_pending=True)
    found, _ = c.transactions("mock", "a1")
    assert [t["description"] for t in found] == ["PENDING B"]


def test_settled_and_pending_coexist(tmp_path):
    c = Cache(tmp_path / "c.db")
    c.put_transactions("mock", "account", "a1", [_tx("t1", "2026-09-01T00:00:00Z", -5.0, "SETTLED")])
    c.put_transactions("mock", "account", "a1", [_tx("t1", "2026-09-01T00:00:00Z", -5.0, "PENDING")],
                       is_pending=True)
    found, _ = c.transactions("mock", "a1")
    assert {t["is_pending"] for t in found} == {True, False}


def test_date_filter_end_day_is_inclusive(tmp_path):
    c = Cache(tmp_path / "c.db")
    c.put_transactions("mock", "account", "a1", [
        _tx("t1", "2026-09-05T14:30:00Z", -1.0, "ON THE DAY"),
        _tx("t2", "2026-09-06T09:00:00Z", -2.0, "NEXT DAY"),
    ])
    found, _ = c.transactions("mock", "a1", from_date="2026-09-05", to_date="2026-09-05")
    assert [t["description"] for t in found] == ["ON THE DAY"]


def test_search_matches_description_and_merchant(tmp_path):
    c = Cache(tmp_path / "c.db")
    c.put_transactions("mock", "account", "a1", [
        _tx("t1", "2026-09-01T00:00:00Z", -1.0, "CARD PAYMENT", merchant="Tesco"),
        _tx("t2", "2026-09-02T00:00:00Z", -2.0, "TESCO STORES"),
        _tx("t3", "2026-09-03T00:00:00Z", -3.0, "SAINSBURYS"),
    ])
    found, _ = c.transactions("mock", "a1", search="esco")
    assert len(found) == 2


def test_transactions_ordered_newest_first_and_limited(tmp_path):
    c = Cache(tmp_path / "c.db")
    c.put_transactions("mock", "account", "a1", [
        _tx(f"t{i}", f"2026-09-{i:02d}T00:00:00Z", -float(i), f"TX {i}") for i in range(1, 10)
    ])
    found, _ = c.transactions("mock", "a1", limit=3)
    assert [t["description"] for t in found] == ["TX 9", "TX 8", "TX 7"]


def test_transaction_without_id_still_stored(tmp_path):
    """Some providers omit transaction_id; we synthesise a stable key."""
    c = Cache(tmp_path / "c.db")
    row = {"timestamp": "2026-09-01T00:00:00Z", "amount": -5.0, "description": "NO ID"}
    c.put_transactions("mock", "account", "a1", [row])
    c.put_transactions("mock", "account", "a1", [row])
    found, _ = c.transactions("mock", "a1")
    assert len(found) == 1


def test_sync_run_bookkeeping(tmp_path):
    c = Cache(tmp_path / "c.db")
    assert c.last_run("mock") is None
    rid = c.start_run("mock")
    assert c.last_run("mock")["status"] == "running"
    c.finish_run(rid, "ok")
    run = c.last_run("mock")
    assert run["status"] == "ok" and run["finished_at"]


def test_parse_ts_handles_naive_and_none():
    assert parse_ts(None) is None
    assert parse_ts("nonsense") is None
    assert parse_ts("2026-09-01T00:00:00").tzinfo is timezone.utc
