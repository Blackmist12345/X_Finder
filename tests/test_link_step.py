import json

from finder import link_step, store


def test_profile_links_from_url_field_and_description():
    author = {"description": "撮影依頼は lit.link/abc_1 まで。https://x.com/foo",
              "entities": {"url": {"urls": [{"url": "https://t.co/z", "expanded_url": "https://lit.link/abc_1"}]}}}
    assert store.profile_links(author) == ["https://lit.link/abc_1"]
    bio = {"profile_bio": {"entities": {"url": {"urls": [{"expanded_url": "https://potofu.me/zzz"}]}}}}
    assert store.profile_links(bio) == ["https://potofu.me/zzz"]
    assert store.profile_links({"description": "東京在住"}) == []


def test_save_links_resets_fetch_only_when_links_change(d1, make_tweet):
    tw = make_tweet("1")
    tw["author"]["entities"] = {"url": {"urls": [{"expanded_url": "https://lit.link/a"}]}}
    store.save_links(d1, store.link_rows([tw], now="t1"))
    d1.query("UPDATE user_links SET fetched_at = 'f1', found_json = '{}'")
    store.save_links(d1, store.link_rows([tw], now="t2"))
    assert d1.query("SELECT fetched_at FROM user_links")[0]["fetched_at"] == "f1"
    tw["author"]["entities"] = {"url": {"urls": [{"expanded_url": "https://lit.link/b"}]}}
    store.save_links(d1, store.link_rows([tw], now="t3"))
    row = d1.query("SELECT fetched_at, urls_json FROM user_links")[0]
    assert row["fetched_at"] is None and json.loads(row["urls_json"]) == ["https://lit.link/b"]


def test_pick_url_prefers_link_pages_and_skips_sns():
    assert link_step.pick_url(["https://instagram.com/a", "https://example.jp/me", "https://lit.link/a"]) == "https://lit.link/a"
    assert link_step.pick_url(["https://www.youtube.com/@a", "https://example.jp/me"]) == "https://example.jp/me"
    assert link_step.pick_url(["https://tiktok.com/@a"]) is None


NEXT_PAGE = """<html><head><meta name="description" content="コスプレイヤーのリンクまとめ"><style>.a{}</style></head>
<body><div><h1>あいう</h1><p>20代/関西在住</p></div><script>var x="18↓";</script>
<script id="__NEXT_DATA__" type="application/json">{"props":{"profile":{"text":"活動地域: 大阪・京都\\n成人済み","id":"abc_1","url":"https://x.com/a"}}}</script>
</body></html>"""


def test_page_text_keeps_visible_text_meta_and_embedded_data():
    text = link_step.page_text(NEXT_PAGE)
    assert "コスプレイヤーのリンクまとめ" in text
    assert "20代/関西在住" in text
    assert "活動地域: 大阪・京都" in text and "成人済み" in text
    assert "18↓" not in text and ".a{}" not in text and "abc_1" not in text and "https://x.com/a" not in text


class FakeResponse:
    def __init__(self, url, body, kind="text/html; charset=utf-8", status=200):
        self.url, self._body, self.status_code = url, body.encode("utf-8"), status
        self.headers = {"content-type": kind}
        self.encoding = "utf-8"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, n):
        yield self._body

    def close(self):
        pass


class FakeSession:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, **kw):
        self.calls.append(url)
        return self.pages[url]


def test_read_found_uses_the_page_rules_in_node():
    found = link_step.read_found({"100": link_step.page_text(NEXT_PAGE)})
    assert found["100"] == {"age": {"label": "20代", "minor": False}, "areas": ["関西", "大阪府", "京都府"], "gender": None}
    assert link_step.read_found({}) == {}


def test_run_saves_only_the_results_not_the_page_text(d1, make_tweet):
    for uid, status in (("100", "judged"), ("200", "rejected"), ("300", "judged")):
        tw = make_tweet(uid, author_id=uid)
        tw["author"]["entities"] = {"url": {"urls": [{"expanded_url": f"https://lit.link/u{uid}"}]}}
        posts, full, partial, links = store.extract([tw], {uid: (status, None, [])})
        store.save(d1, posts, full, partial, links)
        store.save_links(d1, store.link_rows([tw]))
    s = FakeSession({"https://lit.link/u100": FakeResponse("https://lit.link/u100", NEXT_PAGE),
                     "https://lit.link/u300": FakeResponse("https://lit.link/u300", "", status=404)})
    lines = link_step.run(d1, True, session=s)
    assert sorted(s.calls) == ["https://lit.link/u100", "https://lit.link/u300"]
    rows = {r["user_id"]: r for r in d1.query("SELECT * FROM user_links")}
    assert "text" not in rows["100"]
    found = json.loads(rows["100"]["found_json"])
    assert "大阪府" in found["areas"] and found["age"]["label"] and rows["100"]["fetched_at"]
    assert rows["300"]["found_json"] is None and "404" in rows["300"]["error"] and rows["300"]["fetched_at"]
    assert rows["200"]["fetched_at"] is None
    assert "リンク先を読んだ人: 1人(年齢・活動地域・性別のどれかが分かった 1人)、読めなかった 1人" in lines[-1]
    assert link_step.run(d1, True, session=s)[-1] == "- 新しく読むリンクはありませんでした"
    assert link_step.run(d1, False) == []


def test_run_retries_later_when_reading_fails(d1, make_tweet):
    tw = make_tweet("1")
    tw["author"]["entities"] = {"url": {"urls": [{"expanded_url": "https://lit.link/a"}]}}
    posts, full, partial, links = store.extract([tw], {"1": ("judged", None, [])})
    store.save(d1, posts, full, partial, links)
    store.save_links(d1, store.link_rows([tw]))
    s = FakeSession({"https://lit.link/a": FakeResponse("https://lit.link/a", NEXT_PAGE)})

    def broken(texts):
        raise RuntimeError("node がない")

    assert "読み取れませんでした" in link_step.run(d1, True, session=s, reader=broken)[-1]
    assert d1.query("SELECT fetched_at FROM user_links")[0]["fetched_at"] is None
