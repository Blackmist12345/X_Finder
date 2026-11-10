from finder import collect, run_collect, search_diag
from tests.test_collect_filters import FakeResponse, FakeSession
from tests.test_run_collect import wired  # noqa: F401  (fixture)


def jst(ts):
    return str(int(ts))


def test_diag_compares_five_conditions_without_leaking_query():
    s = FakeSession([
        FakeResponse(200, {"tweets": [], "has_next_page": False}),
        FakeResponse(200, {"tweets": [], "has_next_page": False}),
        FakeResponse(200, {"tweets": [{"id": 1, "createdAt": "Tue Nov 10 21:00:00 +0000 2026"}], "has_next_page": False}),
        FakeResponse(200, {"status": "error", "msg": "bad query"}),
        FakeResponse(200, {"tweets": [{"id": 2, "createdAt": "Tue Nov 10 22:00:00 +0000 2026"}, {"id": 3}], "has_next_page": True}),
    ])
    lines, usd = search_diag.run("key", "Q_FULL", "Q_BASE", 100, 200, jst, session=s)
    text = "\n".join(lines)
    assert [c["query"] for c in s.calls] == [
        "Q_FULL since_time:100 until_time:200", "Q_BASE since_time:100 until_time:200",
        search_diag.SHORT_QUERY + " since_time:100 until_time:200", "Q_FULL", search_diag.SHORT_QUERY]
    rows = [l for l in lines if l.startswith("| ①") or l.startswith("| ②") or l.startswith("| ③") or l.startswith("| ④") or l.startswith("| ⑤")]
    assert len(rows) == 5
    assert "| 0件 |" in rows[0] and "| 1件 | 2026-11-10T21:00:00" in rows[2]
    assert "msg=bad query" in rows[3] and "| 2件 |" in rows[4]
    assert "Q_FULL" not in text and "key" not in text.replace("キー", "")
    assert usd == collect.cost_usd(3, 5)


def test_diagnose_mode_saves_nothing(wired, monkeypatch):  # noqa: F811
    d1, summary = wired
    monkeypatch.setenv("MODE", "diagnose")
    monkeypatch.setenv("TWITTERAPI_KEY", "k")
    seen = {}

    def fake_run(api_key, query, base, since_ts, until_ts, jst, session=None):
        seen.update(query=query, base=base, hours=(until_ts - since_ts) / 3600)
        return ["## 検索の診断", "- ok"], 0.0

    monkeypatch.setattr(search_diag, "run", fake_run)
    monkeypatch.setattr(collect, "fetch", lambda *a, **k: (_ for _ in ()).throw(AssertionError("収集はしない")))
    run_collect.main()
    assert seen["hours"] == 1 and seen["query"].startswith(seen["base"])
    assert d1.query("SELECT COUNT(*) AS n FROM runs")[0]["n"] == 0
    assert d1.get_meta(run_collect.WINDOW_KEY) is None
    assert "## 検索の診断" in summary.read_text()
