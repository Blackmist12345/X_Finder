"""N12 データ保存 と N15 ユーザー台帳への書き込み。"""
import json
import re
from datetime import datetime, timedelta, timezone

USER_COLUMNS = ["id", "username", "name", "followers", "following", "description", "location", "profile_image",
                "status", "first_seen_at", "last_seen_at", "profile_updated_at"]
# プロフィールがそろっている人: 毎回上書きする(初めて登録した日だけは残す)。
USER_FULL_UPSERT = (
    "ON CONFLICT(id) DO UPDATE SET username = excluded.username, name = excluded.name, "
    "followers = excluded.followers, following = excluded.following, description = excluded.description, "
    "location = excluded.location, profile_image = excluded.profile_image, status = 'confirmed', "
    "last_seen_at = excluded.last_seen_at, profile_updated_at = excluded.profile_updated_at"
)
# ID と名前しか分からない人(メンション先、リプライ先): 分かる項目だけ更新する。
USER_PARTIAL_UPSERT = (
    "ON CONFLICT(id) DO UPDATE SET username = COALESCE(excluded.username, users.username), "
    "name = COALESCE(excluded.name, users.name), last_seen_at = excluded.last_seen_at"
)
# 引用元として先に保存されたポストが、あとで検索にかかったときだけ「検索で見つけたポスト」に格上げする。
POST_UPSERT = (
    "ON CONFLICT(id) DO UPDATE SET source = 'search', status = excluded.status, "
    "skip_reason = excluded.skip_reason, penalty_json = excluded.penalty_json "
    "WHERE posts.source = 'quoted' AND excluded.source = 'search'"
)
POST_COLUMNS = ["id", "author_id", "source", "text", "url", "created_at", "quoted_id", "reply_to_id",
                "conversation_id", "media_json", "penalty_json", "status", "skip_reason", "collected_at"]


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def days_ago_iso(days, now=None):
    return ((now or datetime.now(timezone.utc)) - timedelta(days=days)).replace(microsecond=0).isoformat()


def to_iso(created_at):
    """TwitterAPI.io の createdAt("Sat Oct 12 10:00:00 +0000 2026")を ISO 形式にする。"""
    if not created_at:
        return None
    for fmt in ("%a %b %d %H:%M:%S %z %Y",):
        try:
            return datetime.strptime(created_at, fmt).astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(created_at.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except ValueError:
        return created_at


def media_urls(tweet):
    ents = tweet.get("extendedEntities") or tweet.get("entities") or {}
    out = []
    for m in ents.get("media", []) or []:
        url = m.get("media_url_https") or m.get("url")
        if url and m.get("type", "photo") == "photo":
            out.append(url)
    return out


def full_user(author, now):
    return [str(author.get("id")), author.get("userName"), author.get("name"), author.get("followers"),
            author.get("following"), author.get("description"), author.get("location"),
            author.get("profilePicture"), "confirmed", now, now, now]


def partial_user(uid, username, name, now):
    return [str(uid), username or None, name or None, None, None, None, None, None, "confirmed", now, now, None]


def extract(tweets, decisions, now=None):
    """検索結果から、保存するポスト・台帳の人・ポストと人の関係を取り出す。

    decisions は {tweet_id: (status, skip_reason, penalty_hits)}。
    """
    now = now or now_iso()
    posts, full, partial, links = {}, {}, {}, set()

    def add_post(tw, source, status, reason, penalties):
        tid = str(tw.get("id"))
        author = tw.get("author") or {}
        quoted = tw.get("quoted_tweet") or None
        posts.setdefault(tid, [
            tid, str(author.get("id")), source, tw.get("text"), tw.get("url"), to_iso(tw.get("createdAt")),
            str(quoted.get("id")) if quoted else None, tw.get("inReplyToId"), tw.get("conversationId"),
            json.dumps(media_urls(tw)), json.dumps(penalties, ensure_ascii=False) if penalties else None,
            status, reason, now,
        ])
        if author.get("id"):
            full[str(author["id"])] = full_user(author, now)
        return tid

    for tw in tweets:  # 検索で見つけたポストを先に登録する
        tid = str(tw.get("id"))
        status, reason, penalties = decisions.get(tid, ("new", None, []))
        add_post(tw, "search", status, reason, penalties)
        links.add((tid, str((tw.get("author") or {}).get("id")), "client"))

    for tw in tweets:
        tid = str(tw.get("id"))
        quoted = tw.get("quoted_tweet")
        if quoted and quoted.get("id"):
            qid = add_post(quoted, "quoted", "context", None, [])
            qa = (quoted.get("author") or {}).get("id")
            if qa:
                links.add((tid, str(qa), "quoted"))
                links.add((qid, str(qa), "client"))

        if tw.get("inReplyToUserId"):
            uid = str(tw["inReplyToUserId"])
            partial.setdefault(uid, partial_user(uid, tw.get("inReplyToUsername"), None, now))
            links.add((tid, uid, "reply_to"))

        for m in ((tw.get("entities") or {}).get("user_mentions") or []):
            uid = m.get("id_str") or m.get("id")
            if not uid:
                continue
            uid = str(uid)
            partial.setdefault(uid, partial_user(uid, m.get("screen_name"), m.get("name"), now))
            links.add((tid, uid, "mention"))

    for uid in full:  # プロフィールがそろっている人は partial から外す
        partial.pop(uid, None)
    return list(posts.values()), list(full.values()), list(partial.values()), sorted(links)


# プロフィールのリンク。リンク先が変わったら、もう一度読むように fetched_at を消す
# (SQLite の UPDATE の右辺は、書き換える前の値で計算される)
USER_LINKS_UPSERT = (
    "ON CONFLICT(user_id) DO UPDATE SET "
    "fetched_at = CASE WHEN user_links.urls_json IS excluded.urls_json THEN user_links.fetched_at ELSE NULL END, "
    "urls_json = excluded.urls_json, updated_at = excluded.updated_at"
)
MAX_PROFILE_LINKS = 5
# 自己紹介に「lit.link/abc」のように http なしで書かれたものも拾う
BARE_LINK = re.compile(r"(?:https?://)?(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}/[A-Za-z0-9_\-./~%?=&#@+]+")
# X 自身のリンクは読まない
SELF_HOSTS = ("x.com", "twitter.com", "t.co")


def _url_entities(node):
    out = []
    for key in ("url", "description"):
        for u in (((node or {}).get(key) or {}).get("urls") or []):
            out.append(u.get("expanded_url") or u.get("url"))
    return out


def profile_links(author):
    """プロフィールの URL 欄と自己紹介にあるリンク(重複なし、書かれた順)。"""
    found = []
    found += _url_entities(author.get("entities"))
    found += _url_entities((author.get("profile_bio") or {}).get("entities"))
    found += BARE_LINK.findall(author.get("description") or "")
    out = []
    for u in found:
        if not u or not isinstance(u, str):
            continue
        u = u.strip().rstrip(".,、。)）")
        if not u.startswith(("http://", "https://")):
            u = "https://" + u
        host = u.split("/")[2].lower().removeprefix("www.")
        if host in SELF_HOSTS or u in out:
            continue
        out.append(u)
    return out[:MAX_PROFILE_LINKS]


def link_rows(tweets, now=None):
    """投稿者(と引用元の投稿者)のうち、プロフィールにリンクがある人の user_links の行。"""
    now = now or now_iso()
    rows = {}
    for tw in tweets:
        for t in (tw, tw.get("quoted_tweet") or {}):
            author = t.get("author") or {}
            if not author.get("id"):
                continue
            urls = profile_links(author)
            if urls:
                rows[str(author["id"])] = [str(author["id"]), json.dumps(urls, ensure_ascii=False), now]
    return list(rows.values())


def save_links(d1, rows):
    d1.insert_many("user_links", ["user_id", "urls_json", "updated_at"], rows, conflict="", upsert=USER_LINKS_UPSERT)


def existing_post_ids(d1, ids):
    found = set()
    ids = list(ids)
    for i in range(0, len(ids), 90):
        part = ids[i:i + 90]
        rows = d1.query(f"SELECT id FROM posts WHERE id IN ({', '.join('?' for _ in part)})", part)
        found.update(r["id"] for r in rows)
    return found


def save(d1, posts, full_users, partial_users, links):
    d1.insert_many("users", USER_COLUMNS, full_users, conflict="", upsert=USER_FULL_UPSERT)
    d1.insert_many("users", USER_COLUMNS, partial_users, conflict="", upsert=USER_PARTIAL_UPSERT)
    d1.insert_many("posts", POST_COLUMNS, posts, conflict="", upsert=POST_UPSERT)
    d1.insert_many("post_users", ["post_id", "user_id", "role"], links)


def record_cost(d1, service, units, usd, note):
    d1.query("INSERT INTO costs (at, service, units, usd, note) VALUES (?, ?, ?, ?, ?)", [now_iso(), service, units, usd, note])


DELETE_AFTER_DAYS = 14  # X Finderで「削除予定」にした案件は、投稿日からこの日数で消す


def purge_deleted(d1, now=None):
    """「削除予定」の案件のうち投稿日から 14 日たったものを消し、消した件数を返す。ユーザー台帳は残す。"""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=DELETE_AFTER_DAYS)).replace(microsecond=0).isoformat()
    rows = d1.query("SELECT p.id FROM reviews r JOIN posts p ON p.id = r.post_id "
                    "WHERE r.state = 'delete' AND p.created_at < ?", [cutoff])
    ids = [r["id"] for r in rows]
    for i in range(0, len(ids), 90):
        part = ids[i:i + 90]
        marks = ", ".join("?" for _ in part)
        for table, col in (("post_users", "post_id"), ("image_reads", "post_id"), ("post_checks", "post_id"), ("judgments", "post_id"), ("reviews", "post_id"), ("posts", "id")):
            d1.query(f"DELETE FROM {table} WHERE {col} IN ({marks})", part)
    return len(ids)


BLACKLIST_REASON = "ブラックリスト"


def blacklisted_ids(d1):
    """ブラックリストに載っている人の数字の ID。"""
    return {r["user_id"] for r in d1.query("SELECT user_id FROM blacklist")}
