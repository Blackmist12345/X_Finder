import pytest

from finder import judge_step, manual_step, store, tree_step

CONFIG = {"basic": {"為替レート(円/ドル)": 150}, "words": [{"word": "大阪", "category": "地方・都市名", "points": -30}]}
NOW = "2026-11-16T00:00:00+00:00"


class FakeResponse:
    def __init__(self, data, status=200):
        self.status_code, self._data, self.text = status, data, str(data)

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, remote, status=200):
        self.remote, self.status, self.calls = remote, status, []

    def get(self, url, headers=None, params=None, timeout=None):
        ids = params["tweet_ids"].split(",")
        self.calls.append(ids)
        if self.status != 200:
            return FakeResponse({"error": "bad"}, self.status)
        return FakeResponse({"status": "success", "tweets": [self.remote[i] for i in ids if i in self.remote]})


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(tree_step.time, "sleep", lambda s: None)


def request(d1, pid):
    d1.query("INSERT INTO manual_adds (post_id, url, requested_at) VALUES (?, ?, ?)", [pid, f"https://x.com/u/status/{pid}", NOW])


def receipt(d1, pid):
    return d1.query("SELECT status, note FROM manual_adds WHERE post_id = ?", [pid])[0]


def post(d1, pid):
    rows = d1.query("SELECT source, status, skip_reason, collected_at, penalty_json FROM posts WHERE id = ?", [pid])
    return rows[0] if rows else None


def test_adds_post_without_filters(d1, make_tweet):
    request(d1, "10")
    # フォロワーが少なく、除外語の入ったポストでも追加する
    remote = {"10": make_tweet("10", text="時給 大阪で撮影", followers=1)}
    lines = manual_step.run(d1, CONFIG, True, "key", FakeSession(remote), now=NOW)
    p = post(d1, "10")
    assert p["source"] == "manual" and p["status"] == "new" and p["skip_reason"] is None
    assert p["collected_at"] == NOW
    assert "大阪" in p["penalty_json"]  # 点数表の語は点数のために数える
    assert receipt(d1, "10")["status"] == "added"
    assert any("追加 1件" in line for line in lines)
    assert d1.query("SELECT COUNT(*) AS n FROM costs WHERE note = '手動追加'")[0]["n"] == 1


def test_not_found_and_blacklisted(d1, make_tweet):
    request(d1, "11")
    request(d1, "12")
    d1.query("INSERT INTO blacklist (user_id, reason, added_at) VALUES ('900', 'spam', ?)", [NOW])
    manual_step.run(d1, CONFIG, True, "key", FakeSession({"12": make_tweet("12", author_id="900")}), now=NOW)
    assert receipt(d1, "11")["status"] == "not_found"
    assert receipt(d1, "12")["status"] == "blacklisted"
    assert post(d1, "12") is None


def test_visible_post_is_left_alone(d1, make_tweet):
    store.save(d1, *store.extract([make_tweet("13")], {}))
    d1.query("UPDATE posts SET status = 'judged', collected_at = ? WHERE id = '13'", [store.now_iso()])
    d1.query("INSERT INTO reviews (post_id, state, updated_at) VALUES ('13', 'checking', ?)", [NOW])
    request(d1, "13")
    session = FakeSession({})
    manual_step.run(d1, CONFIG, True, "key", session, now=NOW)
    assert session.calls == []
    r = receipt(d1, "13")
    assert r["status"] == "exists" and "checking" in r["note"]
    assert post(d1, "13")["source"] == "search"


def test_skipped_post_is_taken_in_again(d1, make_tweet):
    store.save(d1, *store.extract([make_tweet("14")], {"14": ("skipped", "検索除外語: 時給", [])}))
    request(d1, "14")
    manual_step.run(d1, CONFIG, True, "key", FakeSession({"14": make_tweet("14")}), now=NOW)
    p = post(d1, "14")
    assert p["source"] == "manual" and p["status"] == "new" and p["skip_reason"] is None


def test_fetch_error_retries_then_gives_up(d1):
    request(d1, "15")
    for _ in range(manual_step.MAX_TRIES):
        manual_step.run(d1, CONFIG, True, "key", FakeSession({}, status=400), now=NOW)
    assert receipt(d1, "15")["status"] == "failed"


def test_dry_run_does_nothing(d1):
    request(d1, "16")
    assert manual_step.run(d1, CONFIG, False, "key", FakeSession({}), now=NOW) == []
    assert receipt(d1, "16")["status"] == "pending"


def test_manual_skips_tree_exclude_words(d1, make_tweet):
    request(d1, "21")
    remote = {"21": make_tweet("21", inReplyToId="20"), "20": make_tweet("20", author_id="200", text="時給あり")}
    manual_step.run(d1, CONFIG, True, "key", FakeSession(remote), now=NOW)
    config = dict(CONFIG, search_excludes=[{"word": "時給", "score": 1}])
    tree_step.run(d1, config, True, "key", FakeSession(remote))
    assert post(d1, "21")["status"] == "new"


def test_manual_is_judged_without_waiting_and_shown_even_if_rejected(d1):
    items = [{"id": "1", "source": "manual", "collected_at": store.now_iso()}]
    assert judge_step.should_wait(items, 20, 60) is False
    item = {"id": "1", "source": "manual", "penalties": [], "text": "x"}
    evaluated = [(item, {"case_type": "その他"}, "募集ではない", 5, [])]
    store.save(d1, *store.extract([{"id": "1", "author": {"id": "5"}, "text": "x"}], {}))
    judge_step.save(d1, evaluated, [], "m")
    assert post(d1, "1")["status"] == "judged"
