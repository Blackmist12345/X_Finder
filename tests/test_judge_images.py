import re

from finder import judge, judge_step, store, tree_step
from tests.test_judge_score import FakeResponse


class FakeClaude:
    """画像は urls にあるものだけ返す。判定は届いた id にそのまま答える。"""

    def __init__(self, urls=()):
        self.urls, self.bodies, self.got = set(urls), [], []

    def get(self, url, timeout=None):
        self.got.append(url)
        if url.split("?")[0] not in self.urls:
            return FakeResponse(404, {})
        res = FakeResponse(200, {})
        res.content, res.headers = b"\xff\xd8jpeg", {"content-type": "image/jpeg"}
        return res

    def post(self, url, headers=None, json=None, timeout=None):
        content = json["messages"][0]["content"]
        self.bodies.append(content)
        text = content if isinstance(content, str) else "\n".join(c["text"] for c in content if c["type"] == "text")
        ids = re.findall(r"### id: (\S+)", text)
        results = [{"id": i, "wants_photographer": True, "official_or_job": False, "case_type": "コスプレ撮影",
                    "is_cosplay": True, "works": "", "matched_works": [], "prefecture": "東京都", "prefecture_source": "画像",
                    "place": "", "event": "", "event_venue": "", "event_prefecture": "不明", "dates": "", "summary": "募集"}
                   for i in ids]
        return FakeResponse(200, {"content": [{"type": "tool_use", "input": {"results": results}}],
                                  "usage": {"input_tokens": 10, "output_tokens": 5}})


def img(n):
    return f"https://pbs.twimg.com/media/{n}.jpg"


def test_image_list_caps_post_and_tree_root_first():
    it = {"media": [img(i) for i in range(5)],
          "tree": [{"relation": "最初", "media": [img("r1"), img("r2"), img("r3")]},
                   {"relation": "返信", "media": [img("a1"), img("a2")]}]}
    names = [n for n, _ in judge.image_list(it)]
    assert names == ["本文の画像1", "本文の画像2", "本文の画像3", "本文の画像4",
                     "ツリー・最初の画像1", "ツリー・最初の画像2", "ツリー・最初の画像3", "ツリー・返信の画像1"]


def test_posts_with_images_go_in_small_batches_with_images():
    items = [{"id": str(i), "text": "募集"} for i in range(3)]
    items += [{"id": f"m{i}", "text": "募集", "media": [img(f"m{i}")]} for i in range(7)]
    fake = FakeClaude(urls=[img(f"m{i}") for i in range(7)])
    results, _, _, calls = judge.judge(items, "key", "claude-haiku-5-5", [], 20, session=fake)
    assert len(results) == 10 and calls == 3  # 文字だけ 1 回、画像つき 5 件と 2 件
    assert isinstance(fake.bodies[0], str)
    blocks = fake.bodies[1]
    assert sum(1 for b in blocks if b["type"] == "image") == 5
    # 画像は、そのポストの文章のあとに名前と id を付けて続く
    k = next(n for n, b in enumerate(blocks) if b["type"] == "image")
    assert blocks[k - 1]["text"] == "本文の画像1(id: m0):"
    assert "### id: m0" in blocks[k - 2]["text"] and "画像: 本文の画像1" in blocks[k - 2]["text"]
    assert fake.got[0].endswith("?name=large")
    assert results["m0"]["prefecture_source"] == "画像"


def test_unavailable_image_is_dropped():
    items = [{"id": "1", "text": "募集", "media": [img("gone"), img("ok")]}]
    fake = FakeClaude(urls=[img("ok")])
    results, *_ = judge.judge(items, "key", "claude-haiku-5-5", [], 20, session=fake)
    images = [b for b in fake.bodies[0] if b["type"] == "image"]
    assert len(images) == 1 and "1" in results
    assert "本文の画像2(id: 1):" in [b.get("text") for b in fake.bodies[0]]


def test_gemini_gets_inline_images():
    parts = judge._gemini_parts(judge.build_prompt([{"id": "1", "text": "募集"}], [], images={"1": [("本文の画像1", "image/png", "QUJD")]}))
    assert {"inline_data": {"mime_type": "image/png", "data": "QUJD"}} in parts


def test_items_from_db_carry_post_and_tree_images(d1, make_tweet, monkeypatch):
    monkeypatch.setattr(tree_step.time, "sleep", lambda s: None)
    def media(*urls):
        return {"extendedEntities": {"media": [{"media_url_https": u, "type": "photo"} for u in urls]}}

    store.save(d1, *store.extract([make_tweet("51", inReplyToId="40", **media(img("own")))], {}))
    parent = make_tweet("40", author_id="400", **media(img("root")))

    class Session:
        def get(self, url, headers=None, params=None, timeout=None):
            return FakeResponse(200, {"status": "success", "tweets": [parent]})

    tree_step.run(d1, {"basic": {"為替レート(円/ドル)": 150}, "search_excludes": [], "words": []}, True, "key", Session())
    item = judge_step.items_from_db(d1)[0]
    assert item["media"] == [img("own")]
    assert item["tree"][0]["media"] == [img("root")]
    assert [u for _, u in judge.image_list(item)] == [img("own"), img("root")]
