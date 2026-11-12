import pytest

from finder import collect, run_collect
from tests.test_config import good_tables, setup


@pytest.fixture
def wired(d1, monkeypatch, tmp_path):
    notion = setup(d1, good_tables())
    monkeypatch.setattr(run_collect.D1, "from_env", classmethod(lambda cls: d1))
    monkeypatch.setattr(run_collect, "Notion", lambda key: notion)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.delenv("TWITTERAPI_KEY", raising=False)  # 削除ポスト確認が本物の API を呼ばないように
    return d1, summary


def test_production_run_saves_and_advances_window(wired, monkeypatch, make_tweet):
    d1, summary = wired
    tweets = [make_tweet("1", text="大阪でカメラマン募集"), make_tweet("2", followers=0, author_id="101")]
    monkeypatch.setattr(collect, "fetch", lambda *a, **k: (tweets, 1))
    monkeypatch.setenv("MODE", "production")
    run_collect.main()

    assert d1.get_meta(run_collect.WINDOW_KEY) is not None
    posts = {r["id"]: r for r in d1.query("SELECT id, status, penalty_json FROM posts")}
    assert posts["1"]["status"] == "new" and "大阪" in posts["1"]["penalty_json"]
    assert d1.query("SELECT status, new_posts FROM runs")[0] == {"status": "ok", "new_posts": 2}
    assert d1.query("SELECT COUNT(*) AS n FROM costs")[0]["n"] == 1
    assert "取得: 2件" in summary.read_text()

    # 2 回目: 同じポストは新しい扱いにならない
    run_collect.main()
    assert d1.query("SELECT new_posts FROM runs ORDER BY id DESC LIMIT 1")[0]["new_posts"] == 0


def test_failed_fetch_keeps_window(wired, monkeypatch):
    d1, summary = wired

    def boom(*a, **k):
        raise collect.CollectError("401")

    monkeypatch.setattr(collect, "fetch", boom)
    monkeypatch.setenv("MODE", "production")
    with pytest.raises(SystemExit):
        run_collect.main()
    assert d1.get_meta(run_collect.WINDOW_KEY) is None
    assert d1.query("SELECT status FROM runs")[0]["status"] == "failed"
    assert "検索時間枠は進めていません" in summary.read_text()


def test_dry_run_writes_nothing(wired, monkeypatch, make_tweet):
    d1, _ = wired
    monkeypatch.setattr(collect, "fetch", lambda *a, **k: ([make_tweet("1")], 1))
    monkeypatch.setenv("MODE", "dry_run")
    run_collect.main()
    assert d1.query("SELECT COUNT(*) AS n FROM posts")[0]["n"] == 0
    assert d1.get_meta(run_collect.WINDOW_KEY) is None


def test_blacklisted_author_is_skipped_before_judging(wired, monkeypatch, make_tweet):
    d1, summary = wired
    d1.query("INSERT INTO blacklist (user_id, username, reason, added_at) VALUES ('666', 'spam', 'spam', '2026-10-12T00:00:00+00:00')")
    tweets = [make_tweet("1", text="カメラマン募集"), make_tweet("2", text="カメラマン募集", author_id="666")]
    monkeypatch.setattr(collect, "fetch", lambda *a, **k: (tweets, 1))
    monkeypatch.setenv("MODE", "production")
    run_collect.main()

    posts = {r["id"]: r for r in d1.query("SELECT id, status, skip_reason FROM posts")}
    assert posts["1"]["status"] == "new"
    assert posts["2"] == {"id": "2", "status": "skipped", "skip_reason": "ブラックリスト"}
    assert "ブラックリスト 1件" in summary.read_text()


def test_zero_streak_warns_and_fails_once(wired, monkeypatch, make_tweet):
    d1, summary = wired
    monkeypatch.setenv("MODE", "production")
    clock = {"now": 1_800_000_000}
    monkeypatch.setattr(run_collect.time, "time", lambda: clock["now"])

    def zero(*a, diag=None, **k):
        if diag is not None:
            diag.append("1ページ目: HTTP 200・tweets 0件・次のページ なし")
        return [], 1

    monkeypatch.setattr(collect, "fetch", zero)
    run_collect.main()  # 1時間前から0件(初回)
    assert "検索の返事: 1ページ目: HTTP 200・tweets 0件" in summary.read_text()
    assert "1件も取れていません" not in summary.read_text()

    clock["now"] += 6 * 3600
    with pytest.raises(SystemExit):  # 6時間を超えたので1回だけ失敗にしてメールを出す
        run_collect.main()
    assert "1件も取れていません" in summary.read_text()
    assert d1.query("SELECT status FROM runs ORDER BY id DESC LIMIT 1")[0]["status"] == "ok"
    assert d1.get_meta(run_collect.WINDOW_KEY) is not None

    clock["now"] += 600
    run_collect.main()  # 2回目からは警告だけで成功

    monkeypatch.setattr(collect, "fetch", lambda *a, **k: ([make_tweet("1", text="カメラマン募集")], 1))
    clock["now"] += 600
    run_collect.main()  # 取れたら数え直す
    assert d1.get_meta(run_collect.ZERO_SINCE_KEY) is None
    assert d1.get_meta(run_collect.ZERO_ALERTED_KEY) is False


def test_backfill_refetches_from_hours_ago_and_saves(wired, monkeypatch, make_tweet):
    d1, summary = wired
    clock = {"now": 1_800_000_000}
    monkeypatch.setattr(run_collect.time, "time", lambda: clock["now"])
    d1.set_meta(run_collect.WINDOW_KEY, clock["now"] - 600)  # ふだんの収集は10分前まで進んでいる
    seen = {}

    def fetch(api_key, query, since_ts, until_ts, **k):
        seen.update(since=since_ts, until=until_ts)
        return [make_tweet("1", text="カメラマン募集")], 1

    monkeypatch.setattr(collect, "fetch", fetch)
    monkeypatch.setenv("MODE", "backfill")
    monkeypatch.setenv("HOURS", "36")
    run_collect.main()
    assert seen["until"] - seen["since"] == 36 * 3600
    assert d1.query("SELECT COUNT(*) AS n FROM posts")[0]["n"] == 1
    assert d1.get_meta(run_collect.WINDOW_KEY) == seen["until"]
    assert "取り直し: backfill" in summary.read_text()


def test_rows_read_is_counted_per_caller_and_summed_for_the_day(d1):
    from collections import Counter
    from datetime import datetime, timezone

    from finder.d1 import D1

    real = D1("a", "b", "c")
    real._count_reads([{"meta": {"rows_read": 120}}, {"meta": {}}, {}], "image_step.candidates")
    real._count_reads([{"meta": {"rows_read": 30}}], "store.purge_deleted")
    assert real.rows_read == Counter({"image_step.candidates": 120, "store.purge_deleted": 30})

    now = datetime(2026, 11, 12, 3, tzinfo=timezone.utc)
    d1.rows_read = Counter({"image_step.candidates": 120, "store.purge_deleted": 30})
    lines = run_collect.rows_read_lines(d1, True, now)
    assert "今回 150行" in lines[1] and "image_step.candidates 120" in lines[1]
    lines = run_collect.rows_read_lines(d1, True, now)
    assert "合計: 300行" in lines[2] and "2回" in lines[2]
    # 日付(UTC)が変わったら数え直す
    lines = run_collect.rows_read_lines(d1, True, datetime(2026, 11, 13, 0, 5, tzinfo=timezone.utc))
    assert "合計: 150行" in lines[2]
    # 試し実行では保存しない
    assert len(run_collect.rows_read_lines(d1, False, now)) == 2


def test_month_cost_reads_only_new_rows_and_keeps_the_sum(d1):
    from finder import store
    basic = {"為替レート(円/ドル)": 100, "月の費用の目標(円)": 1000}
    plan = " ".join(r["detail"] for r in d1.query(
        "EXPLAIN QUERY PLAN SELECT service, SUM(usd), MAX(id) FROM costs NOT INDEXED WHERE id > ? AND at >= ? GROUP BY service", [0, "x"]))
    assert "rowid>" in plan  # 前回の続きからだけ読む
    store.record_cost(d1, "claude", 1, 1.0, "a")
    store.record_cost(d1, "twitterapi.io", 1, 0.5, "b")
    assert "約150円" in run_collect.month_cost_lines(d1, basic)[1]
    assert d1.get_meta(run_collect.MONTH_COST_KEY)["last_id"] == 2
    d1.query("DELETE FROM costs")  # 読み終えた行は、もう読まなくても合計に入っている
    d1.query("INSERT INTO costs (id, at, service, units, usd, note) VALUES (3, ?, 'claude', 1, 0.25, 'c')", [store.now_iso()])
    line = run_collect.month_cost_lines(d1, basic)[1]
    assert "約175円" in line and "claude 約125円" in line
    # 試し実行では合計を保存しない
    d1.query("INSERT INTO costs (id, at, service, units, usd, note) VALUES (4, ?, 'claude', 1, 1.0, 'd')", [store.now_iso()])
    assert "約275円" in run_collect.month_cost_lines(d1, basic, production=False)[1]
    assert d1.get_meta(run_collect.MONTH_COST_KEY)["last_id"] == 3
