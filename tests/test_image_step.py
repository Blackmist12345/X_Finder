import json

from finder import image_step


class FakeResp:
    def __init__(self, status=200, data=None, content=b"", ctype="image/jpeg"):
        self.status_code, self._data, self.content = status, data, content
        self.headers = {"content-type": ctype}
        self.text = json.dumps(data) if data is not None else ""

    def json(self):
        return self._data


class FakeSession:
    """画像の取得(GET)と Claude(POST)の代わり。answers[ポストの画像URL] = AI が返すアカウント。"""

    def __init__(self, accounts, broken=()):
        self.accounts, self.broken, self.gets, self.posts = accounts, set(broken), [], []

    def get(self, url, timeout=None):
        self.gets.append(url)
        if any(b in url for b in self.broken):
            return FakeResp(404)
        return FakeResp(content=b"img")

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append(json)
        return FakeResp(data={"content": [{"type": "tool_use", "input": {"accounts": self.accounts}}],
                              "usage": {"input_tokens": 1500, "output_tokens": 80}})


CONFIG = {"basic": {"判定に使うAI": "claude-haiku-5-5", "為替レート(円/ドル)": 150}}


def add_post(d1, pid, status="judged", media=("https://pbs.twimg.com/media/a.jpg",)):
    d1.query("INSERT OR IGNORE INTO users (id, username, first_seen_at, last_seen_at) VALUES ('100', 'client', 'x', 'x')")
    d1.query("INSERT INTO posts (id, author_id, source, text, created_at, status, collected_at, media_json) VALUES (?, '100', 'search', 't', 'x', ?, ?, ?)",
             [pid, status, f"2026-10-12T00:00:0{pid}+00:00", json.dumps(list(media))])


def test_reads_accounts_from_images_once(d1):
    add_post(d1, "1")
    add_post(d1, "2", status="rejected")  # 対象外は読まない
    add_post(d1, "3", media=())  # 画像なし
    s = FakeSession([{"handle": "@cos_a", "name": "A"}, {"handle": "COS_A", "name": "重複"}, {"handle": "client", "name": "本人"},
                     {"handle": "bad id!", "name": ""}, {"handle": "cam_b", "name": ""}])
    lines = image_step.run(d1, CONFIG, True, {"ANTHROPIC_API_KEY": "k"}, session=s)
    assert s.gets == ["https://pbs.twimg.com/media/a.jpg?name=large"]
    rows = d1.query("SELECT post_id, images, accounts_json, error FROM image_reads")
    assert [(r["post_id"], r["images"], r["error"]) for r in rows] == [("1", 1, None)]
    assert json.loads(rows[0]["accounts_json"]) == [{"handle": "cos_a", "name": "A"}, {"handle": "cam_b", "name": ""}]
    assert d1.query("SELECT service, units FROM costs") == [{"service": "claude", "units": 1580}]
    assert any("見つかったアカウント 2人" in l for l in lines)
    # 2 回目は読まない
    image_step.run(d1, CONFIG, True, {"ANTHROPIC_API_KEY": "k"}, session=s)
    assert len(s.posts) == 1


def test_unreachable_image_is_recorded_and_not_retried(d1):
    add_post(d1, "1", media=("https://pbs.twimg.com/media/gone.jpg",))
    s = FakeSession([], broken=["gone"])
    image_step.run(d1, CONFIG, True, {"ANTHROPIC_API_KEY": "k"}, session=s)
    assert s.posts == []
    assert d1.query("SELECT error FROM image_reads") == [{"error": "画像を取得できませんでした"}]


def test_needs_key_and_production(d1):
    add_post(d1, "1")
    s = FakeSession([])
    assert image_step.run(d1, CONFIG, False, {"ANTHROPIC_API_KEY": "k"}, session=s) == []
    assert any("登録されていない" in l for l in image_step.run(d1, CONFIG, True, {}, session=s))
    assert s.gets == [] and d1.query("SELECT * FROM image_reads") == []
