from finder import collect, filters


def test_build_query_fills_by_score_and_reports_overflow():
    excludes = [{"word": "時給", "score": 1}, {"word": "VRChat", "score": 9}, {"word": "とても長い除外語" * 30, "score": 5}]
    query, used, overflow = collect.build_query(["カメラマン"], ["募集"], excludes, max_length=100, max_excludes=99)
    assert query.startswith('(("カメラマン") ("募集"))')
    assert used == ["VRChat", "時給"]
    assert overflow == ["とても長い除外語" * 30]
    assert len(query) <= 100


def test_build_query_caps_number_of_excludes():
    excludes = [{"word": "時給", "score": 1}, {"word": "VRChat", "score": 9}, {"word": "有償", "score": 5}]
    query, used, overflow = collect.build_query(["カメラマン"], ["募集"], excludes, max_excludes=1)
    assert used == ["VRChat"] and overflow == ["有償", "時給"]
    # 既定(今は0語): 除外語はすべて収集後に取り除く
    query, used, overflow = collect.build_query(["カメラマン"], ["募集"], excludes)
    assert query == '(("カメラマン") ("募集"))' and used == [] and len(overflow) == 3


def test_cost_uses_per_tweet_price_with_per_call_minimum():
    assert collect.cost_usd(100, 5) == 100 * 15 / 100_000
    assert collect.cost_usd(0, 2) == 2 * 15 / 100_000


def test_penalty_word_does_not_match_inside_tokyo():
    words = [{"word": "京都", "points": -30}, {"word": "大阪", "points": -30}]
    assert filters.word_hits("東京都内のスタジオで撮影", words) == []
    assert filters.word_hits("京都で撮影、東京都も可", words) == [{"word": "京都", "category": "その他", "points": -30}]


def test_skip_reason(make_tweet):
    assert filters.skip_reason(make_tweet("1", followers=5), [], 10) == "フォロワー数が10未満"
    assert filters.skip_reason(make_tweet("2", text="時給1500円 カメラマン募集"), ["時給"], 0) == "検索除外語: 時給"
    assert filters.skip_reason(make_tweet("3", retweeted_tweet={"id": "9"}), [], 0) == "リポスト"
    assert filters.skip_reason(make_tweet("4"), ["時給"], 0) is None


class FakeResponse:
    def __init__(self, status, data):
        self.status_code, self._data, self.text = status, data, str(data)

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(params)
        return self.responses.pop(0)


def test_fetch_pages_until_done(monkeypatch):
    monkeypatch.setattr(collect.time, "sleep", lambda s: None)
    s = FakeSession([
        FakeResponse(200, {"tweets": [{"id": 1}, {"id": 2}], "has_next_page": True, "next_cursor": "c1"}),
        FakeResponse(429, {}),
        FakeResponse(200, {"tweets": [{"id": 2}, {"id": 3}], "has_next_page": False}),
    ])
    tweets, calls = collect.fetch("key", "q", 100, 200, session=s)
    assert [t["id"] for t in tweets] == [1, 2, 3]
    assert calls == 3
    assert s.calls[-1]["cursor"] == "c1"
    assert "since_time:100 until_time:200" in s.calls[0]["query"]


def test_fetch_error_raises_so_window_is_not_advanced(monkeypatch):
    monkeypatch.setattr(collect.time, "sleep", lambda s: None)
    s = FakeSession([FakeResponse(200, {"tweets": [{"id": 1}], "has_next_page": True, "next_cursor": "c"}), FakeResponse(401, {"error": "bad key"})])
    try:
        collect.fetch("key", "q", 0, 1, session=s)
    except collect.CollectError as e:
        assert "401" in str(e)
    else:
        raise AssertionError("CollectError が出るはず")


def test_fetch_records_what_came_back(monkeypatch):
    monkeypatch.setattr(collect.time, "sleep", lambda s: None)
    s = FakeSession([FakeResponse(200, {"tweets": [], "has_next_page": False, "status": "success"})])
    diag = []
    tweets, calls = collect.fetch("key", "q", 0, 1, session=s, diag=diag)
    assert tweets == [] and calls == 1
    assert diag == ["1ページ目: HTTP 200・tweets 0件・次のページ なし・status=success"]


def test_fetch_error_inside_200_raises(monkeypatch):
    monkeypatch.setattr(collect.time, "sleep", lambda s: None)
    for body in ({"status": "error", "msg": "query too long"}, {"msg": "account suspended"}):
        s = FakeSession([FakeResponse(200, body)])
        diag = []
        try:
            collect.fetch("key", "q", 0, 1, session=s, diag=diag)
        except collect.CollectError as e:
            assert body["msg"] in str(e)
        else:
            raise AssertionError("CollectError が出るはず")
        assert body["msg"] in diag[0]


def test_fetch_missing_tweets_without_message_is_zero(monkeypatch):
    monkeypatch.setattr(collect.time, "sleep", lambda s: None)
    s = FakeSession([FakeResponse(200, {"has_next_page": False})])
    diag = []
    assert collect.fetch("key", "q", 0, 1, session=s, diag=diag) == ([], 1)
    assert "tweets なし" in diag[0] and "返事のキー: has_next_page" in diag[0]
