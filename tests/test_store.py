from finder import store


def test_save_builds_ledger_and_links(d1, make_tweet):
    quoted = make_tweet("50", text="元の募集", author_id="200")
    t1 = make_tweet("51", text="@friend 一緒にどうですか", author_id="100", quoted_tweet=quoted,
                    entities={"user_mentions": [{"id_str": "300", "screen_name": "friend", "name": "友達"}]})
    t2 = make_tweet("52", author_id="100", inReplyToUserId="400", inReplyToUsername="parent")
    posts, full, partial, links = store.extract([t1, t2], {"51": ("new", None, []), "52": ("skipped", "リポスト", [])}, now="2026-10-12T00:00:00+00:00")
    store.save(d1, posts, full, partial, links)

    rows = {r["id"]: r for r in d1.query("SELECT id, source, status, skip_reason, quoted_id FROM posts")}
    assert rows["51"]["source"] == "search" and rows["51"]["quoted_id"] == "50"
    assert rows["50"]["source"] == "quoted" and rows["50"]["status"] == "context"
    assert rows["52"]["status"] == "skipped"

    users = {r["id"]: r for r in d1.query("SELECT id, username, followers FROM users")}
    assert users["100"]["followers"] == 500
    assert users["300"]["username"] == "friend" and users["300"]["followers"] is None
    assert users["400"]["username"] == "parent"

    roles = {(r["post_id"], r["user_id"], r["role"]) for r in d1.query("SELECT * FROM post_users")}
    assert {("51", "100", "client"), ("51", "200", "quoted"), ("51", "300", "mention"), ("52", "400", "reply_to"), ("50", "200", "client")} <= roles


def test_ledger_updates_counts_but_keeps_first_seen(d1, make_tweet):
    first = store.extract([make_tweet("1", followers=10)], {}, now="2026-10-01T00:00:00+00:00")
    store.save(d1, *first)
    later = store.extract([make_tweet("2", followers=99)], {}, now="2026-10-12T00:00:00+00:00")
    store.save(d1, *later)
    row = d1.query("SELECT followers, first_seen_at, last_seen_at FROM users WHERE id = '100'")[0]
    assert row["followers"] == 99
    assert row["first_seen_at"].startswith("2026-10-01")
    assert row["last_seen_at"].startswith("2026-10-12")


def test_partial_user_does_not_erase_known_profile(d1, make_tweet):
    store.save(d1, *store.extract([make_tweet("1", author_id="300")], {}))
    mention = make_tweet("2", author_id="100", entities={"user_mentions": [{"id_str": "300", "screen_name": "user300"}]})
    store.save(d1, *store.extract([mention], {}))
    assert d1.query("SELECT followers FROM users WHERE id = '300'")[0]["followers"] == 500


def test_quoted_post_is_upgraded_when_later_found_by_search(d1, make_tweet):
    quoted = make_tweet("50", author_id="200")
    store.save(d1, *store.extract([make_tweet("51", quoted_tweet=quoted)], {}))
    store.save(d1, *store.extract([make_tweet("50", author_id="200")], {"50": ("new", None, [])}))
    row = d1.query("SELECT source, status FROM posts WHERE id = '50'")[0]
    assert (row["source"], row["status"]) == ("search", "new")


def test_search_post_found_again_keeps_its_status(d1, make_tweet):
    store.save(d1, *store.extract([make_tweet("1")], {"1": ("new", None, [])}))
    d1.query("UPDATE posts SET status = 'judged' WHERE id = '1'")
    store.save(d1, *store.extract([make_tweet("1")], {"1": ("new", None, [])}))
    assert d1.query("SELECT status FROM posts WHERE id = '1'")[0]["status"] == "judged"


def test_existing_post_ids_and_many_rows(d1, make_tweet):
    tweets = [make_tweet(str(i), author_id=str(1000 + i)) for i in range(120)]
    store.save(d1, *store.extract(tweets, {}))
    assert store.existing_post_ids(d1, [str(i) for i in range(115, 125)]) == {"115", "116", "117", "118", "119"}
    assert d1.query("SELECT COUNT(*) AS n FROM users")[0]["n"] == 120


def test_to_iso():
    assert store.to_iso("Sat Oct 10 03:00:00 +0000 2026") == "2026-10-10T03:00:00+00:00"


def test_purge_deleted_removes_only_old_cases_marked_delete(d1):
    from datetime import datetime, timezone
    now = datetime(2026, 10, 30, tzinfo=timezone.utc)
    for pid, created in (("1", "2026-10-10T00:00:00+00:00"), ("2", "2026-10-20T00:00:00+00:00"),
                         ("3", "2026-10-01T00:00:00+00:00"), ("4", "2026-10-01T00:00:00+00:00")):
        d1.query("INSERT INTO posts (id, author_id, created_at, status, collected_at) VALUES (?, '100', ?, 'judged', ?)",
                 [pid, created, created])
        d1.query("INSERT INTO judgments (post_id, judged_at) VALUES (?, ?)", [pid, created])
        d1.query("INSERT INTO post_users (post_id, user_id, role) VALUES (?, '100', 'client')", [pid])
    d1.query("INSERT INTO users (id, username, status, first_seen_at, last_seen_at) VALUES ('100', 'c', 'confirmed', 'x', 'x')")
    for pid, state in (("1", "delete"), ("2", "delete"), ("3", "hold")):
        d1.query("INSERT INTO reviews (post_id, state, updated_at) VALUES (?, ?, 'x')", [pid, state])

    assert store.purge_deleted(d1, now) == 1
    assert [r["id"] for r in d1.query("SELECT id FROM posts ORDER BY id")] == ["2", "3", "4"]
    assert [r["post_id"] for r in d1.query("SELECT post_id FROM judgments ORDER BY post_id")] == ["2", "3", "4"]
    assert [r["post_id"] for r in d1.query("SELECT post_id FROM reviews ORDER BY post_id")] == ["2", "3"]
    assert len(d1.query("SELECT * FROM post_users")) == 3
    assert d1.query("SELECT id FROM users") == [{"id": "100"}]  # ユーザー台帳は残す
