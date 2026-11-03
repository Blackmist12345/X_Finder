import json
from datetime import datetime, timezone

from finder import account_step

CONFIG = {"basic": {"為替レート(円/ドル)": 150}}
NOW = datetime(2026, 11, 3, tzinfo=timezone.utc)


class FakeResp:
    def __init__(self, data, status=200):
        self.status_code, self._data, self.text = status, data, json.dumps(data)

    def json(self):
        return self._data


class FakeSession:
    """Get User Info の代わり。remote[小文字の ID] = 返す答え。"""

    def __init__(self, remote):
        self.remote, self.calls = remote, []

    def get(self, url, headers=None, params=None, timeout=None):
        h = params["userName"]
        self.calls.append(h)
        return FakeResp(self.remote.get(h, {"status": "error", "msg": "user not found"}))


def ok(uid, name, followers, following):
    return {"status": "success", "data": {"id": uid, "userName": name, "name": f"名前{uid}", "followers": followers,
                                          "following": following, "profilePicture": f"https://pbs.twimg.com/{uid}.jpg"}}


def read(d1, pid, handles, at="2026-11-02T00:00:00+00:00"):
    d1.query("INSERT OR REPLACE INTO image_reads (post_id, read_at, images, accounts_json) VALUES (?, ?, 1, ?)",
             [pid, at, json.dumps([{"handle": h, "name": ""} for h in handles])])


def test_looks_up_new_handles_and_saves_to_ledger(d1, monkeypatch):
    monkeypatch.setattr(account_step.time, "sleep", lambda s: None)
    read(d1, "1", ["Cos_A", "typo_x", "frozen"])
    s = FakeSession({"cos_a": ok("500", "Cos_A", 443, 296),
                     "frozen": {"status": "success", "data": {"unavailable": True, "unavailableReason": "Suspended"}}})
    lines = account_step.run(d1, CONFIG, True, "k", session=s, now=NOW)
    assert sorted(s.calls) == ["cos_a", "frozen", "typo_x"]
    u = d1.query("SELECT username, followers, following, profile_image FROM users WHERE id = '500'")[0]
    assert u == {"username": "Cos_A", "followers": 443, "following": 296, "profile_image": "https://pbs.twimg.com/500.jpg"}
    got = {r["handle"]: r["result"] for r in d1.query("SELECT handle, result FROM handle_lookups")}
    assert got == {"cos_a": "ok", "typo_x": "not_found", "frozen": "unavailable"}
    cost = d1.query("SELECT service, units, usd FROM costs")[0]
    assert cost == {"service": "twitterapi.io", "units": 3, "usd": 3 * 18 / 100_000}
    assert any("取れた 1人" in l and "いない ID 1人" in l and "凍結など 1人" in l for l in lines)

    # 2 回目は調べない(7 日以内)
    s2 = FakeSession({})
    account_step.run(d1, CONFIG, True, "k", session=s2, now=NOW)
    assert s2.calls == []


def test_skips_known_users_and_rechecks_only_when_seen_again_after_7_days(d1):
    d1.query("INSERT INTO users (id, username, followers, following, first_seen_at, last_seen_at, profile_updated_at) "
             "VALUES ('1', 'known', 10, 5, 'x', 'x', '2026-11-01T00:00:00+00:00'), "
             "('2', 'old', 10, 5, 'x', 'x', '2026-10-01T00:00:00+00:00'), "
             "('3', 'old_again', 10, 5, 'x', 'x', '2026-10-01T00:00:00+00:00')")
    read(d1, "1", ["known", "old", "new_one"], at="2026-09-30T00:00:00+00:00")
    read(d1, "2", ["old_again"], at="2026-11-02T00:00:00+00:00")
    assert sorted(account_step.candidates(d1, now=NOW)) == ["new_one", "old_again"]


def test_stops_after_repeated_failures_and_retries_next_time(d1, monkeypatch):
    monkeypatch.setattr(account_step.time, "sleep", lambda s: None)
    read(d1, "1", [f"user_{i}" for i in range(8)])

    class Broken(FakeSession):
        def get(self, url, headers=None, params=None, timeout=None):
            self.calls.append(params["userName"])
            return FakeResp({"error": "bad key"}, status=401)

    s = Broken({})
    lines = account_step.run(d1, CONFIG, True, "k", session=s, now=NOW)
    assert len(s.calls) == account_step.MAX_FAILS
    assert d1.query("SELECT * FROM handle_lookups") == []
    assert any("調べられなかった 8人" in l for l in lines)


def test_does_nothing_without_key_or_in_trial(d1):
    read(d1, "1", ["cos_a"])
    assert account_step.run(d1, CONFIG, False, "k") == []
    assert "TWITTERAPI_KEY" in account_step.run(d1, CONFIG, True, None)[-1]
