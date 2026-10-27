"""遡り: N3 を通ったポストの返信元・引用元を、root まで(最大 10 段)たどって保存し、ツリー込みで N3 をもう一度かける。

返信元は TwitterAPI.io の Get Tweets by IDs で 1 件ずつ取る(50 件まで 1 回にまとめる)。
引用元は検索結果や取ったポストに丸ごと付いてくるので、取りに行かない。
D1 にもうあるポストと、前に見られなかったポスト(missing_posts)は取りに行かない。

2 回目の N3:
  - 除外語(検索に入れた語も、入りきらなかった語も全部)をツリーの文章にかけ、当たれば対象外にする
  - 点数表の区分「語」はツリー全体で数え、同じ語は何度出てきても 1 回
  - フォロワー数とブラックリストは、最初に収集したポストだけで判定済みなので、ここでは見ない

遡りが済んだポストには post_trees の行を作る。判定(judge_step)はこの行があるポストから判定する。
取りに行くのに失敗したら行を作らず、次の収集でもう一度遡る。
"""
import json
import time

import requests

from finder import collect, filters, store

URL = "https://api.twitterapi.io/twitter/tweets"
MAX_IDS = 50          # 1 回の問い合わせで送れる ID の上限
MAX_DEPTH = 10        # 遡る段数の上限(Latte 決定。長いツリーは第三者どうしの雑談のことが多い)
MAX_FETCH = 500       # 1 回の収集で取りに行くポストの上限(約 11 円)。超えた分は次の収集に回す
MAX_POSTS = 200       # 1 回の収集で遡るポストの上限
TREE_SKIP = "ツリーの除外語"


class TreeError(Exception):
    pass


def fetch(api_key, ids, session=None):
    """ids のポストを {id: tweet} で返す(見られないものは入らない)。返り値は (tweets, 呼び出し回数, 返ってきた件数)。"""
    http = session or requests.Session()
    headers = {"X-API-Key": api_key}
    out, calls, returned = {}, 0, 0
    for i in range(0, len(ids), MAX_IDS):
        part = ids[i:i + MAX_IDS]
        data = None
        for attempt in range(4):
            calls += 1
            try:
                res = http.get(URL, headers=headers, params={"tweet_ids": ",".join(part)}, timeout=30)
            except requests.RequestException as e:
                if attempt == 3:
                    raise TreeError(f"接続に失敗しました: {e}") from e
                time.sleep(2 ** (attempt + 1))
                continue
            if res.status_code == 200:
                data = res.json()
                break
            if res.status_code == 429 or res.status_code >= 500:
                time.sleep(2 ** (attempt + 1))
                continue
            raise TreeError(f"TwitterAPI.io がエラーを返しました ({res.status_code}): {res.text[:200]}")
        if not isinstance(data, dict) or data.get("status") != "success":
            raise TreeError(f"TwitterAPI.io の答えが読めませんでした: {str(data)[:200]}")
        tweets = [t for t in data.get("tweets") or [] if t.get("id")]
        # まとめて 1 件も返ってこないのは、消えたのではなく向こうの不調のことが多い。見られない印は付けずにやめる
        if len(part) >= 3 and not tweets:
            raise TreeError(f"{len(part)}件まとめて 1 件も返ってきませんでした")
        returned += len(tweets)
        out.update({str(t["id"]): t for t in tweets})
        time.sleep(0.5)
    return out, calls, returned


def _node(row):
    return {"id": row["id"], "reply_to": row.get("reply_to_id") or None, "quoted": row.get("quoted_id") or None,
            "created_at": row.get("created_at") or "", "text": row.get("text") or ""}


def parents(node):
    return [x for x in (node.get("reply_to"), node.get("quoted")) if x]


def load_nodes(d1, ids):
    out = {}
    ids = list(ids)
    for i in range(0, len(ids), 90):
        part = ids[i:i + 90]
        rows = d1.query("SELECT id, reply_to_id, quoted_id, created_at, text FROM posts "
                        f"WHERE id IN ({', '.join('?' for _ in part)})", part)
        out.update({r["id"]: _node(r) for r in rows})
    return out


def known_missing(d1, ids):
    found = set()
    ids = list(ids)
    for i in range(0, len(ids), 90):
        part = ids[i:i + 90]
        rows = d1.query(f"SELECT id FROM missing_posts WHERE id IN ({', '.join('?' for _ in part)})", part)
        found.update(r["id"] for r in rows)
    return found


def tweet_rows(tweets, now):
    """取ったポスト(と、それに付いてきた引用元)を posts と users の行にする。"""
    posts, users = {}, {}
    for tw in tweets:
        for t, source in ((tw, "tree"), (tw.get("quoted_tweet") or {}, "tree")):
            if not t.get("id"):
                continue
            tid = str(t["id"])
            author = t.get("author") or {}
            quoted = t.get("quoted_tweet") or None
            posts.setdefault(tid, [
                tid, str(author.get("id")), source, t.get("text"), t.get("url"), store.to_iso(t.get("createdAt")),
                str(quoted["id"]) if quoted and quoted.get("id") else None, t.get("inReplyToId") or None,
                t.get("conversationId"), json.dumps(store.media_urls(t)), None, "context", None, now,
            ])
            if author.get("id"):
                users[str(author["id"])] = store.full_user(author, now)
    return list(posts.values()), list(users.values())


def walk(d1, starts, api_key, session=None, now=None):
    """starts(posts の行)から root まで遡る。

    返り値は (nodes, 見られなかった ID, 呼び出し回数, 返ってきた件数, 上限で取り残した ID)。
    取ったポストは D1 に保存し、見られなかったポストは missing_posts に書く。
    """
    now = now or store.now_iso()
    nodes = {r["id"]: _node(r) for r in starts}
    gone, left = set(), set()
    calls = returned = fetched = 0
    level = [r["id"] for r in starts]
    for _ in range(MAX_DEPTH):
        frontier = {p for pid in level if pid in nodes for p in parents(nodes[pid])}
        need = [p for p in frontier if p not in nodes and p not in gone]
        if need:
            nodes.update(load_nodes(d1, need))
            need = [p for p in need if p not in nodes]
            gone |= known_missing(d1, need)
            need = sorted(p for p in need if p not in gone)
        if need and not api_key:  # 鍵がなければ取りに行かず、そのポストは次の収集で遡る
            left |= set(need)
            need = []
        if need:
            room = max(MAX_FETCH - fetched, 0)
            left |= set(need[room:])
            need = need[:room]
        if need:
            got, c, n = fetch(api_key, need, session)
            calls, returned, fetched = calls + c, returned + n, fetched + len(need)
            posts, users = tweet_rows(got.values(), now)
            d1.insert_many("users", store.USER_COLUMNS, users, conflict="", upsert=store.USER_FULL_UPSERT)
            d1.insert_many("posts", store.POST_COLUMNS, posts)
            for row in posts:
                nodes.setdefault(row[0], {"id": row[0], "reply_to": row[7], "quoted": row[6],
                                          "created_at": row[5] or "", "text": row[3] or ""})
            missing = [p for p in need if p not in got]
            gone |= set(missing)
            d1.insert_many("missing_posts", ["id", "checked_at"], [[p, now] for p in missing])
        level = [p for p in frontier if p in nodes]
        if not level:
            break
    return nodes, gone, calls, returned, left


def ancestors(post_id, nodes):
    """post_id から MAX_DEPTH 段までの返信元・引用元の ID を、古い順(root が先頭)に返す。"""
    seen, level = {post_id}, [post_id]
    for _ in range(MAX_DEPTH):
        nxt = [p for pid in level if pid in nodes for p in parents(nodes[pid]) if p not in seen]
        if not nxt:
            break
        seen.update(nxt)
        level = nxt
    found = [nodes[p] for p in seen if p != post_id and p in nodes]
    return [n["id"] for n in sorted(found, key=lambda n: (n["created_at"], n["id"]))]


def recheck(row, tree_text, exclude_words, words):
    """2 回目の N3。(対象外の理由 or None, 点数表の語の当たり) を返す。"""
    hits = filters.find_words(tree_text, exclude_words)
    if hits:
        return f"{TREE_SKIP}: " + "、".join(hits), json.loads(row.get("penalty_json") or "[]")
    # 本文とツリーをまとめて照合する(word_hits は 1 語につき 1 回しか数えない)
    return None, filters.word_hits((row.get("text") or "") + "\n" + tree_text, words)


def pending(d1):
    """N3 を通って判定を待っている、まだ遡っていないポスト。"""
    return d1.query(
        "SELECT p.id, p.text, p.reply_to_id, p.quoted_id, p.created_at, p.penalty_json FROM posts p "
        "WHERE p.status = 'new' AND NOT EXISTS (SELECT 1 FROM post_trees t WHERE t.post_id = p.id) "
        "ORDER BY p.collected_at, p.id LIMIT ?", [MAX_POSTS])


def run(d1, config, production, api_key, session=None):
    """遡りと 2 回目の N3 を行い、Actions の結果に足す行を返す(件数だけ。ポストの中身は書かない)。"""
    lines = ["", "## 引用元・返信元を遡る" + ("" if production else "(試し実行: 遡っていません)"), ""]
    if not production:
        return lines + ["- 試し実行では遡らず、本文だけで判定します"]
    starts = pending(d1)
    if not starts:
        return lines + ["- 遡るポストはありませんでした"]
    try:
        nodes, gone, calls, returned, left = walk(d1, starts, api_key, session)
    except TreeError as e:  # 印は付けず、次の収集でもう一度遡る
        return lines + [f"- ⚠️ 遡りをやめました(次の収集でもう一度遡ります): {e}"]

    exclude_words = [e["word"] for e in config.get("search_excludes", []) if e.get("word")]
    words = config.get("words", [])
    now = store.now_iso()
    trees, skipped, depths = [], [], []
    for row in starts:
        ids = ancestors(row["id"], nodes)
        # 上限で取り残した親があるポストは、次の収集で続きを遡る
        if any(p in left for p in ids + parents(nodes[row["id"]])) or any(
                p in left for i in ids for p in parents(nodes[i])):
            continue
        trees.append([row["id"], json.dumps(ids), now])
        depths.append(len(ids))
        if not ids:
            continue
        tree_text = "\n".join(nodes[i]["text"] for i in ids)
        reason, hits = recheck(row, tree_text, exclude_words, words)
        if reason:
            skipped.append(reason)
            d1.query("UPDATE posts SET status = 'skipped', skip_reason = ? WHERE id = ?", [reason, row["id"]])
        else:
            d1.query("UPDATE posts SET penalty_json = ? WHERE id = ?",
                     [json.dumps(hits, ensure_ascii=False) if hits else None, row["id"]])
    d1.insert_many("post_trees", ["post_id", "ids_json", "updated_at"], trees, conflict="OR REPLACE")

    usd = collect.cost_usd(returned, calls) if calls else 0
    if calls:
        store.record_cost(d1, "twitterapi.io", returned, usd, "引用元・返信元の遡り")
    rate = config["basic"]["為替レート(円/ドル)"]
    with_tree = sum(1 for d in depths if d)
    lines.append(f"- 遡ったポスト {len(trees)}件(ツリーあり {with_tree}件、最大 {max(depths or [0])}件のツリー)、"
                 f"取りに行ったポスト {returned}件(見られなかった {len(gone)}件)、費用 約{usd * rate:.2f}円")
    if skipped:
        lines.append(f"- ツリーに除外語があり対象外にした: {len(skipped)}件")
    if left and not api_key:
        lines.append("- ⚠️ TWITTERAPI_KEY が設定されていないので、返信元を取りに行けないポストは遡りを待っています")
    elif left:
        lines.append(f"- 1 回の上限({MAX_FETCH}件)に達したので、残りは次の収集で遡ります")
    return lines
