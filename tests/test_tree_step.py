import json

import pytest

from finder import judge, judge_step, store, tree_step

CONFIG = {
    "basic": {"為替レート(円/ドル)": 150},
    "search_excludes": [{"word": "時給", "score": 3}, {"word": "モデル募集", "score": 1}],
    "penalty_words": [{"word": "大阪", "category": "地方・都市名", "points": -30}],
}


class FakeResponse:
    def __init__(self, data, status=200):
        self.status_code, self._data, self.text = status, data, str(data)

    def json(self):
        return self._data


class FakeSession:
    """Get Tweets by IDs の代わり。remote にあるポストだけ返す。"""

    def __init__(self, remote):
        self.remote, self.calls = remote, []

    def get(self, url, headers=None, params=None, timeout=None):
        ids = params["tweet_ids"].split(",")
        self.calls.append(ids)
        return FakeResponse({"status": "success", "msg": "", "tweets": [self.remote[i] for i in ids if i in self.remote]})


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(tree_step.time, "sleep", lambda s: None)


def collect(d1, tweets):
    store.save(d1, *store.extract(tweets, {}))


def tree_of(d1, pid):
    rows = d1.query("SELECT ids_json FROM post_trees WHERE post_id = ?", [pid])
    return json.loads(rows[0]["ids_json"]) if rows else None


def test_walks_reply_and_quote_up_to_root(d1, make_tweet):
    # 51 は 40 への返信。40 は 30 を引用(30 は 40 に付いてくる)。30 は 20 への返信。20 が root
    collect(d1, [make_tweet("51", inReplyToId="40", createdAt="Mon Oct 26 05:00:00 +0000 2026")])
    q30 = make_tweet("30", author_id="300", inReplyToId="20", createdAt="Mon Oct 26 02:00:00 +0000 2026")
    remote = {
        "40": make_tweet("40", quoted_tweet=q30, createdAt="Mon Oct 26 03:00:00 +0000 2026"),
        "20": make_tweet("20", author_id="200", text="11/3 東京ビッグサイトで併せ", createdAt="Mon Oct 26 01:00:00 +0000 2026"),
    }
    session = FakeSession(remote)
    lines = tree_step.run(d1, CONFIG, True, "key", session)

    assert session.calls == [["40"], ["20"]]  # 30 は 40 に付いてきたので取りに行かない
    assert tree_of(d1, "51") == ["20", "30", "40"]
    sources = {r["id"]: r["source"] for r in d1.query("SELECT id, source FROM posts")}
    assert sources == {"51": "search", "40": "tree", "30": "tree", "20": "tree"}
    assert d1.query("SELECT status FROM posts WHERE id = '51'")[0]["status"] == "new"
    assert any("取りに行ったポスト 2件" in line for line in lines)
    assert d1.query("SELECT units FROM costs WHERE note = '引用元・返信元の遡り'")[0]["units"] == 2


def test_post_without_parents_gets_empty_tree_without_api(d1, make_tweet):
    collect(d1, [make_tweet("1"), make_tweet("2", quoted_tweet=make_tweet("50", author_id="200"))])
    session = FakeSession({})
    tree_step.run(d1, CONFIG, True, None, session)
    assert session.calls == []
    assert tree_of(d1, "1") == [] and tree_of(d1, "2") == ["50"]


def test_tree_exclude_word_skips_post(d1, make_tweet):
    collect(d1, [make_tweet("51", inReplyToId="40")])
    tree_step.run(d1, CONFIG, True, "key", FakeSession({"40": make_tweet("40", author_id="400", text="時給3000円で撮影")}))
    row = d1.query("SELECT status, skip_reason FROM posts WHERE id = '51'")[0]
    assert row == {"status": "skipped", "skip_reason": "ツリーの除外語: 時給"}


def test_penalty_word_counted_once_across_tree(d1, make_tweet):
    collect(d1, [make_tweet("51", text="大阪で撮影", inReplyToId="40")])
    remote = {"40": make_tweet("40", text="大阪のイベント", inReplyToId="30"), "30": make_tweet("30", text="大阪に行きます")}
    tree_step.run(d1, CONFIG, True, "key", FakeSession(remote))
    hits = json.loads(d1.query("SELECT penalty_json FROM posts WHERE id = '51'")[0]["penalty_json"])
    assert hits == [{"word": "大阪", "category": "地方・都市名", "points": -30}]


def test_missing_parent_is_remembered(d1, make_tweet):
    collect(d1, [make_tweet("51", inReplyToId="40")])
    session = FakeSession({})
    tree_step.run(d1, CONFIG, True, "key", session)
    assert tree_of(d1, "51") == []
    assert d1.query("SELECT id FROM missing_posts")[0]["id"] == "40"
    collect(d1, [make_tweet("52", inReplyToId="40")])
    tree_step.run(d1, CONFIG, True, "key", session)
    assert session.calls == [["40"]]  # 見られなかったポストは二度と取りに行かない


def test_depth_is_capped(d1, make_tweet):
    collect(d1, [make_tweet("100", inReplyToId="99")])
    remote = {str(i): make_tweet(str(i), inReplyToId=str(i - 1), createdAt=f"Mon Oct 26 {i - 80:02d}:00:00 +0000 2026")
              for i in range(85, 100)}
    tree_step.run(d1, CONFIG, True, "key", FakeSession(remote))
    assert tree_of(d1, "100") == [str(i) for i in range(90, 100)]


def test_without_key_reply_waits_and_is_not_judged(d1, make_tweet):
    collect(d1, [make_tweet("51", inReplyToId="40"), make_tweet("52")])
    tree_step.run(d1, CONFIG, True, None)
    assert tree_of(d1, "51") is None and tree_of(d1, "52") == []
    assert [it["id"] for it in judge_step.items_from_db(d1)] == ["52"]


def test_judge_prompt_gets_tree_root_first(d1, make_tweet):
    collect(d1, [make_tweet("51", text="カメラマンさん募集", inReplyToId="40")])
    remote = {"40": make_tweet("40", text="11/3 東京ビッグサイトで併せ", author_id="100", inReplyToId="30"),
              "30": make_tweet("30", text="併せしませんか", author_id="300")}
    tree_step.run(d1, CONFIG, True, "key", FakeSession(remote))
    items = judge_step.items_from_db(d1)
    assert [(t["relation"], t["by_client"]) for t in items[0]["tree"]] == [("最初", False), ("返信", True)]
    prompt = judge.build_prompt(items, [])
    assert "ツリー(古い順):\n- [最初・別の人] 併せしませんか\n- [返信・依頼主] 11/3 東京ビッグサイトで併せ" in prompt


def test_tree_post_found_later_by_search_is_upgraded(d1, make_tweet):
    collect(d1, [make_tweet("51", inReplyToId="40")])
    tree_step.run(d1, CONFIG, True, "key", FakeSession({"40": make_tweet("40", author_id="400")}))
    collect(d1, [make_tweet("40", author_id="400")])
    assert d1.query("SELECT source, status FROM posts WHERE id = '40'")[0] == {"source": "search", "status": "new"}
