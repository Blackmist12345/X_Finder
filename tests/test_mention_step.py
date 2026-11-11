import json

from finder import account_step, mention_step

MEMBERS = """束の間の幸せなアナクトガーデン併せもカメラマンさんを募集しております

参加メンバー
・ミジ(@mari_2646 )
・スア(@byocos )
・イヴァン(鍵垢のため記載控えます)
・ヒョナ（＠spp_kanoe）
・ルカ(@RuRaRuRa_819 )
連絡は me@example.com まで、@client へ"""


def test_find_picks_ids_and_written_names():
    got = mention_step.find(MEMBERS)
    assert got == [("mari_2646", "ミジ"), ("byocos", "スア"), ("spp_kanoe", "ヒョナ"), ("RuRaRuRa_819", "ルカ"), ("client", "")]


def test_find_skips_emails_long_ids_and_ids_without_at():
    assert mention_step.find("ID:mari_2646 / a@b.jp / @abcdefghijklmnopq / x@ok") == []
    assert mention_step.find("＠ok_1、@ok_2。") == [("ok_1", ""), ("ok_2", "")]


def test_find_skips_leading_reply_mentions_only_for_replies():
    assert mention_step.find("@parent @other よろしくお願いします @friend") == [("parent", ""), ("other", ""), ("friend", "")]
    assert mention_step.find("@parent @other よろしくお願いします @friend", is_reply=True) == [("friend", "")]


def add(d1, pid, text, source="search", status="judged", reply_to=None, author="100"):
    d1.query("INSERT OR IGNORE INTO users (id, username, first_seen_at, last_seen_at) VALUES ('100', 'client', 'x', 'x')")
    d1.query("INSERT INTO posts (id, author_id, source, text, created_at, status, collected_at, reply_to_id) VALUES (?, ?, ?, ?, 'x', ?, ?, ?)",
             [pid, author, source, text, status, f"2026-11-10T00:00:0{pid[-1]}+00:00", reply_to])


def start(d1, at="2026-11-01T00:00:00+00:00"):
    """最初の実行(この時刻より前に集めたポストは対象外になる)。"""
    assert any("今までのポストは対象外" in l for l in mention_step.run(d1, True, now=at))


def accounts(d1, pid):
    rows = d1.query("SELECT accounts_json FROM text_mentions WHERE post_id = ?", [pid])
    return json.loads(rows[0]["accounts_json"]) if rows else None


def test_run_collects_from_post_and_whole_tree(d1):
    start(d1)
    add(d1, "11", MEMBERS, source="tree", status="context")
    add(d1, "12", "@client 追記です @byocos も一緒です", source="tree", status="context", reply_to="11")
    add(d1, "1", "@client こちら相互優先です @new_one", reply_to="12")
    add(d1, "2", "@skip_me", status="rejected")  # 判定で外れたものは拾わない
    d1.query("INSERT INTO post_trees (post_id, ids_json, updated_at) VALUES ('1', '[\"11\", \"12\"]', '2026-11-10T00:00:00+00:00')")
    lines = mention_step.run(d1, True, now="2026-11-10T01:00:00+00:00")
    assert accounts(d1, "1") == [
        {"handle": "new_one", "name": ""},
        {"handle": "mari_2646", "name": "ミジ", "from": "tree"},
        {"handle": "byocos", "name": "スア", "from": "tree"},
        {"handle": "spp_kanoe", "name": "ヒョナ", "from": "tree"},
        {"handle": "RuRaRuRa_819", "name": "ルカ", "from": "tree"},
    ]  # 投稿者本人(client)は出さない
    assert accounts(d1, "2") is None
    assert any("見つかった ID 5個" in l for l in lines)
    # 2 回目は拾い直さない
    assert mention_step.run(d1, True) == ["", "## 本文から X の ID を拾う", "", "- 新しく拾うポストはありませんでした"]


def test_run_rescans_after_tree_is_updated(d1):
    start(d1)
    add(d1, "1", "募集します")
    mention_step.run(d1, True, now="2026-11-10T01:00:00+00:00")
    assert accounts(d1, "1") == []
    add(d1, "11", "メンバー @late_one", source="tree", status="context")
    d1.query("INSERT INTO post_trees (post_id, ids_json, updated_at) VALUES ('1', '[\"11\"]', '2026-11-10T02:00:00+00:00')")
    mention_step.run(d1, True, now="2026-11-10T03:00:00+00:00")
    assert accounts(d1, "1") == [{"handle": "late_one", "name": "", "from": "tree"}]


def test_first_run_skips_posts_collected_before(d1):
    add(d1, "1", "@old_one")  # 2026-11-10 に集めたポスト
    start(d1, at="2026-11-10T12:00:00+00:00")
    add(d1, "22", "@new_one")
    d1.query("UPDATE posts SET collected_at = '2026-11-10T13:00:00+00:00' WHERE id = '22'")
    mention_step.run(d1, True, now="2026-11-10T13:10:00+00:00")
    assert accounts(d1, "1") is None
    assert accounts(d1, "22") == [{"handle": "new_one", "name": ""}]


def test_does_nothing_in_trial(d1):
    add(d1, "1", "@someone")
    assert mention_step.run(d1, False) == []
    assert accounts(d1, "1") is None


def test_account_step_also_looks_up_ids_from_text(d1):
    d1.query("INSERT INTO text_mentions (post_id, scanned_at, accounts_json) VALUES ('1', '2026-11-02T00:00:00+00:00', ?)",
             [json.dumps([{"handle": "From_Text", "name": "ミジ", "from": "tree"}])])
    assert account_step.candidates(d1) == ["from_text"]
