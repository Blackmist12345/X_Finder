from finder import deleted_step, store

CONFIG = {"periods": {deleted_step.FREQUENCY_ITEM: 4}, "basic": {"為替レート(円/ドル)": 155}}


def add_case(d1, pid, state=None, collected_at=None, author="100"):
    collected_at = collected_at or store.now_iso()
    d1.query("INSERT INTO posts (id, author_id, status, collected_at) VALUES (?, ?, 'judged', ?)", [pid, author, collected_at])
    d1.query("INSERT INTO judgments (post_id, judged_at) VALUES (?, ?)", [pid, collected_at])
    if state:
        d1.query("INSERT INTO reviews (post_id, state, opened_at, updated_at) VALUES (?, ?, 'o', 'u')", [pid, state])


class FakeResponse:
    def __init__(self, data, status=200):
        self.status_code, self._data, self.text = status, data, str(data)

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, alive, status=200):
        self.alive, self.status, self.calls = set(alive), status, []

    def get(self, url, headers=None, params=None, timeout=None):
        ids = params["tweet_ids"].split(",")
        self.calls.append(ids)
        return FakeResponse({"status": "success", "msg": "", "tweets": [{"id": i} for i in ids if i in self.alive] + [{"id": ""}]},
                            self.status)


def states(d1):
    return {r["post_id"]: r["state"] for r in d1.query("SELECT post_id, state FROM reviews")}


def test_targets_new_and_checking_only(d1):
    add_case(d1, "1")
    add_case(d1, "2", "checking")
    add_case(d1, "3", "hold", collected_at="2026-01-01T00:00:00+00:00")  # 受注中(hold)は確かめない
    add_case(d1, "4", "accepted")  # 連絡中(accepted)も確かめない
    add_case(d1, "5", "delete")
    add_case(d1, "6", collected_at="2026-01-01T00:00:00+00:00")  # 一覧から消えた新着
    add_case(d1, "7", author="999")
    d1.query("INSERT INTO blacklist (user_id, reason, added_at) VALUES ('999', 'spam', 'x')")
    assert deleted_step.target_ids(d1) == ["1", "2"]


def test_gone_posts_move_to_delete_once(d1, monkeypatch):
    monkeypatch.setattr(deleted_step.time, "sleep", lambda s: None)
    for pid, state in (("1", None), ("2", "checking"), ("3", None), ("4", None)):
        add_case(d1, pid, state)
    lines = deleted_step.run(d1, CONFIG, True, "k", session=FakeSession({"1", "4"}), now_ts=1000)
    assert "4件を確かめ" in lines[1] and "2件を削除予定" in lines[1]
    assert states(d1) == {"2": "delete", "3": "delete"}
    gone = {r["post_id"] for r in d1.query("SELECT post_id FROM post_checks WHERE gone_at IS NOT NULL")}
    assert gone == {"2", "3"}
    assert d1.query("SELECT opened_at FROM reviews WHERE post_id = '2'")[0]["opened_at"] == "o"
    # 自分で新着に戻したカードは、もう確かめない
    d1.query("UPDATE reviews SET state = 'new' WHERE post_id = '2'")
    assert deleted_step.target_ids(d1) == ["1", "4"]


def test_waits_for_interval_and_zero_turns_off(d1, monkeypatch):
    monkeypatch.setattr(deleted_step.time, "sleep", lambda s: None)
    add_case(d1, "1")
    s = FakeSession({"1"})
    assert deleted_step.run(d1, CONFIG, True, "k", session=s, now_ts=10_000)
    assert deleted_step.run(d1, CONFIG, True, "k", session=s, now_ts=10_000 + 3 * 3600) == []
    assert deleted_step.run(d1, CONFIG, True, "k", session=s, now_ts=10_000 + 4 * 3600 - 300)
    assert len(s.calls) == 2
    off = {**CONFIG, "periods": {deleted_step.FREQUENCY_ITEM: 0}}
    assert deleted_step.run(d1, off, True, "k", session=s, now_ts=10**9) == []
    assert deleted_step.run(d1, CONFIG, False, "k", session=s, now_ts=10**9) == []


def test_batches_of_fifty_and_costs(d1, monkeypatch):
    monkeypatch.setattr(deleted_step.time, "sleep", lambda s: None)
    ids = [f"{i:03d}" for i in range(120)]
    for pid in ids:
        add_case(d1, pid)
    s = FakeSession(ids)
    deleted_step.run(d1, CONFIG, True, "k", session=s, now_ts=1)
    assert [len(c) for c in s.calls] == [50, 50, 20]
    assert states(d1) == {}
    assert d1.query("SELECT units FROM costs")[0]["units"] == 120


def test_empty_answer_or_error_marks_nothing(d1, monkeypatch):
    monkeypatch.setattr(deleted_step.time, "sleep", lambda s: None)
    for pid in ("1", "2", "3"):
        add_case(d1, pid)
    lines = deleted_step.run(d1, CONFIG, True, "k", session=FakeSession(set()), now_ts=1)
    assert "印は付けていません" in lines[1] and states(d1) == {}
    assert d1.get_meta(deleted_step.LAST_CHECK_KEY) is None  # 次の収集でもう一度試す
    lines = deleted_step.run(d1, CONFIG, True, "k", session=FakeSession({"1"}, status=400), now_ts=1)
    assert "印は付けていません" in lines[1] and states(d1) == {}


def test_purge_removes_checks(d1):
    add_case(d1, "1", "delete")
    d1.query("UPDATE posts SET created_at = '2026-01-01T00:00:00+00:00'")
    deleted_step.mark_gone(d1, ["1"])
    assert store.purge_deleted(d1) == 1
    assert d1.query("SELECT * FROM post_checks") == []
