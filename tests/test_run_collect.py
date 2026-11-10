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
