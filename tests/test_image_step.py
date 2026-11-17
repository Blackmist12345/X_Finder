import json

from finder import image_step, store


class FakeResp:
    def __init__(self, status=200, data=None, content=b"", ctype="image/jpeg"):
        self.status_code, self._data, self.content = status, data, content
        self.headers = {"content-type": ctype}
        self.text = json.dumps(data) if data is not None else ""

    def json(self):
        return self._data


class FakeSession:
    """画像の取得(GET)と Claude(POST)の代わり。answers[ポストの画像URL] = AI が返すアカウント。"""

    def __init__(self, accounts, broken=(), place=None, extra=None):
        self.accounts, self.broken, self.gets, self.posts = accounts, set(broken), [], []
        self.extra = extra or {}
        self.place = place or {"prefecture": "不明", "venue": "", "event": ""}

    def get(self, url, timeout=None):
        self.gets.append(url)
        if any(b in url for b in self.broken):
            return FakeResp(404)
        return FakeResp(content=b"img")

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append(json)
        return FakeResp(data={"content": [{"type": "tool_use", "input": {"accounts": self.accounts, "place": self.place, **self.extra}}],
                              "usage": {"input_tokens": 1500, "output_tokens": 80}})


CONFIG = {"basic": {"判定に使うAI": "claude-haiku-5-5", "為替レート(円/ドル)": 150}}


RECENT_DAY = store.days_ago_iso(1)[:10]  # 候補は集めてから 14 日以内だけなので、きのう集めたことにする


def add_post(d1, pid, status="judged", media=("https://pbs.twimg.com/media/a.jpg",), day=RECENT_DAY):
    d1.query("INSERT OR IGNORE INTO users (id, username, first_seen_at, last_seen_at) VALUES ('100', 'client', 'x', 'x')")
    d1.query("INSERT INTO posts (id, author_id, source, text, created_at, status, collected_at, media_json) VALUES (?, '100', 'search', 't', 'x', ?, ?, ?)",
             [pid, status, f"{day}T00:00:0{pid}+00:00", json.dumps(list(media))])


def test_old_posts_are_not_candidates(d1):
    add_post(d1, "1")
    add_post(d1, "2", day=store.days_ago_iso(image_step.LIST_DAYS + 2)[:10])  # 一覧から消えた古いポストは探さない
    assert [r["id"] for r in image_step.candidates(d1)] == ["1"]


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


def add_tree(d1, pid, tree):
    """tree: [(id, media)] を root から順に。"""
    for tid, media in tree:
        d1.query("INSERT INTO posts (id, author_id, source, text, created_at, status, collected_at, media_json) VALUES (?, '100', 'tree', 't', 'x', 'context', 'x', ?)",
                 [tid, json.dumps(list(media))])
    d1.query("INSERT INTO post_trees (post_id, ids_json, updated_at) VALUES (?, ?, 'x')", [pid, json.dumps([t for t, _ in tree])])


def test_reads_tree_images_too(d1):
    add_post(d1, "1")
    add_tree(d1, "1", [("20", [f"https://pbs.twimg.com/media/r{n}.jpg" for n in range(3)]), ("30", ["https://pbs.twimg.com/media/q0.jpg", "https://pbs.twimg.com/media/q1.jpg"])])
    add_post(d1, "2", media=())  # 本文に画像がなくても、ツリーにあれば読む
    add_tree(d1, "2", [("40", ["https://pbs.twimg.com/media/z.jpg"])])
    add_post(d1, "3", media=())  # どちらにもなければ読まない
    add_tree(d1, "3", [("50", [])])
    s = FakeSession([{"handle": "tree_only", "name": "", "from": "ツリー"}, {"handle": "both", "name": "", "from": "ツリー"},
                     {"handle": "both", "name": "B", "from": "本文"}, {"handle": "body", "name": ""}])
    image_step.run(d1, CONFIG, True, {"ANTHROPIC_API_KEY": "k"}, session=s)
    # 本文の画像 → ツリーの画像(root に近い方から 4 枚まで)の順に、名前を付けて渡す
    labels = [c["text"] for c in s.posts[1]["messages"][0]["content"][:-1] if c["type"] == "text"]
    assert labels == ["本文の画像1", "ツリーの画像1", "ツリーの画像2", "ツリーの画像3", "ツリーの画像4"]
    assert [u.split("/")[-1] for u in s.gets[1:]] == ["a.jpg?name=large", "r0.jpg?name=large", "r1.jpg?name=large", "r2.jpg?name=large", "q0.jpg?name=large"]
    rows = {r["post_id"]: r for r in d1.query("SELECT post_id, images, accounts_json FROM image_reads")}
    assert set(rows) == {"1", "2"}
    assert rows["1"]["images"] == 5 and rows["2"]["images"] == 1
    assert json.loads(rows["1"]["accounts_json"]) == [
        {"handle": "both", "name": "B"}, {"handle": "body", "name": ""}, {"handle": "tree_only", "name": "", "from": "tree"}]


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


def add_judgment(d1, pid, pref, source, place=""):
    raw = {"case_type": "コスプレ撮影", "prefecture": pref, "prefecture_source": source, "place": place, "works": ""}
    d1.query("INSERT INTO judgments (post_id, judged_at, model, case_type, prefecture, prefecture_source, place, score, raw_json) "
             "VALUES (?, 'x', 'm', 'コスプレ撮影', ?, ?, ?, 0, ?)", [pid, pref, source, place, json.dumps(raw, ensure_ascii=False)])


SCORE_CONFIG = dict(CONFIG, words=[], events=[
    {"aliases": ["ストフェス"], "name": "日本橋ストリートフェスタ", "venue": "日本橋", "prefecture": "大阪府"}],
    points=[{"condition": c, "names": [c], "kind": k, "points": p} for c, k, p in (
        ("コスプレ撮影", "種類", 50), ("東京都", "都道府県", 0), ("その他の道府県", "都道府県", -30), ("場所不明", "都道府県", 0))])


def test_image_place_replaces_unknown_or_profile_place(d1):
    add_post(d1, "1")
    add_judgment(d1, "1", "東京都", "プロフィール")
    s = FakeSession([], place={"prefecture": "不明", "venue": "", "event": "ストフェス"})
    lines = image_step.run(d1, SCORE_CONFIG, True, {"ANTHROPIC_API_KEY": "k"}, session=s)
    row = d1.query("SELECT prefecture, prefecture_source, place, score, raw_json FROM judgments")[0]
    assert (row["prefecture"], row["prefecture_source"], row["place"], row["score"]) == ("大阪府", "画像", "ストフェス", 20)
    assert json.loads(row["raw_json"])["prefecture"] == "大阪府"
    assert any("場所が分かった 1件" in l for l in lines)


def test_image_place_does_not_override_text(d1):
    add_post(d1, "1")
    add_judgment(d1, "1", "東京都", "本文", "池袋")
    s = FakeSession([], place={"prefecture": "大阪府", "venue": "難波", "event": ""})
    image_step.run(d1, SCORE_CONFIG, True, {"ANTHROPIC_API_KEY": "k"}, session=s)
    assert d1.query("SELECT prefecture, prefecture_source, place FROM judgments") == [
        {"prefecture": "東京都", "prefecture_source": "本文", "place": "池袋"}]


def test_cast_details_are_kept(d1):
    add_post(d1, "1")
    s = FakeSession([{"handle": "_Mikurucos_", "name": "しもつきみくる", "chara": "香菱", "work": "原神", "part": "subject"},
                     {"handle": "cam_b", "name": "", "chara": "", "part": "?"}],
                    extra={"shoot_name": "璃月併せ", "memo": "予算4500〜5000円"})
    image_step.run(d1, CONFIG, True, {"ANTHROPIC_API_KEY": "k"}, session=s)
    row = d1.query("SELECT accounts_json FROM image_reads")[0]
    assert json.loads(row["accounts_json"]) == [
        {"handle": "_Mikurucos_", "name": "しもつきみくる", "chara": "香菱", "work": "原神", "part": "subject"}, {"handle": "cam_b", "name": ""}]
    assert d1.query("SELECT post_id, shoot_name, memo FROM image_details") == [{"post_id": "1", "shoot_name": "璃月併せ", "memo": "予算4500〜5000円"}]


def test_chat_posts_are_read_again_and_first(d1):
    old_day = store.days_ago_iso(image_step.LIST_DAYS + 5)[:10]
    add_post(d1, "1")  # ふつうの新しいポスト
    add_post(d1, "2", day=old_day)  # 古いが、チャットから取り込んだ(前の形の読み取りがある)
    d1.query("INSERT INTO image_reads (post_id, read_at, images, accounts_json) VALUES ('2', 'x', 1, '[]')")
    add_post(d1, "3", day=old_day, media=())  # チャットから取り込んだ、画像のないポスト
    add_post(d1, "4", status="new")  # チャットから取り込んだが、まだ判定前
    for pid in ("2", "3", "4"):
        d1.query("INSERT INTO chat_adds (post_id, requested_at) VALUES (?, ?)", [pid, store.now_iso()])
    assert [r["id"] for r in image_step.candidates(d1)] == ["2", "3", "1"]
    s = FakeSession([{"handle": "cos_a", "name": "A", "chara": "胡桃", "part": "subject"}], extra={"shoot_name": "併せ", "memo": ""})
    image_step.run(d1, CONFIG, True, {"ANTHROPIC_API_KEY": "k"}, session=s)
    assert len(s.posts) == 2  # 画像のない 3 は AI を呼ばない
    rows = {r["post_id"]: r for r in d1.query("SELECT post_id, images, accounts_json, error FROM image_reads")}
    assert json.loads(rows["2"]["accounts_json"]) == [{"handle": "cos_a", "name": "A", "chara": "胡桃", "part": "subject"}]
    assert (rows["3"]["images"], rows["3"]["accounts_json"], rows["3"]["error"]) == (0, "[]", None)
    assert {r["post_id"] for r in d1.query("SELECT post_id FROM image_details")} == {"1", "2", "3"}
    assert image_step.candidates(d1) == []  # 読み直しは 1 回だけ
