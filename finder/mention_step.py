"""N7 参加者情報(の続き): 本文に書かれた X の ID(@abc / ＠abc)を拾い、「関係するアカウント」に出す。

対象は、判定が済んだポストの本文と、root まで遡った引用元・返信元(ツリー)のポストの本文。AI は使わない(無料)。
- 結果は text_mentions に 1 ポスト 1 行で保存する。accounts_json は image_reads と同じ形
  [{"handle": "abc", "name": "ミジ"}]。ツリーの本文だけに書かれていた人には from: "tree" を付ける。
- 「ミジ(@mari_2646)」のように ID の直前にかっこ付きで名前があれば、その名前(キャラ名など)を name に入れる。
- 「ID:abc」のような @ なしの書き方は拾わない(ただの英単語と見分けがつかないため)。メールアドレスの @ も拾わない。
- 返信の先頭に X が自動で付ける @(返信相手)は、ツリーの投稿者そのものなので拾わない。
- 投稿者本人は外す。フォロワー数・アイコンは account_step が取る。
- 遡りがあとから済んだポストは、もう一度拾い直す(post_trees.updated_at が新しければ)。
- 対象は、この仕組みを入れたあとに集めたポストだけ(最初の実行の時刻を meta の text_mentions_since に残す。Latte 2026-11-11)。
"""
import json
import re
from datetime import datetime

from finder import store

MAX_POSTS_PER_RUN = 500  # 1 回で拾うポストの上限。残りは次の収集で
SINCE_KEY = "text_mentions_since"
LIST_DAYS = 14  # X Finderの一覧に出る日数
# 前が英数字・_・.・@ のときはメールアドレスなどの一部なので拾わない。後ろに ID の文字が続くときは 16 文字以上で ID ではない
MENTION = re.compile(r"(?<![A-Za-z0-9_.@＠])[@＠]([A-Za-z0-9_]{1,15})(?![A-Za-z0-9_@＠])")
# 「ミジ(@mari_2646)」「ミジ（＠mari_2646 ）」の名前の部分
NAMED = re.compile(r"([^\s()（）@＠・:：,、。/|｜「」\[\]【】]{1,20})[ \t　]*[(（][ \t　]*[@＠]([A-Za-z0-9_]{1,15})(?![A-Za-z0-9_])")
# 返信の先頭に自動で付く「@a @b 」
LEADING = re.compile(r"^(?:\s*[@＠][A-Za-z0-9_]{1,15})+\s*")


def find(text, is_reply=False):
    """本文から [(handle, name)] を出てきた順に返す(同じ人は 1 回)。"""
    text = text or ""
    if is_reply:
        text = LEADING.sub("", text, count=1)
    names = {}
    for m in NAMED.finditer(text):
        names.setdefault(m.group(2).lower(), m.group(1))
    out, seen = [], set()
    for m in MENTION.finditer(text):
        h = m.group(1)
        if h.lower() in seen:
            continue
        seen.add(h.lower())
        out.append((h, names.get(h.lower(), "")))
    return out


def collect_accounts(post, tree_posts, author_username=None):
    """収集したポストとツリーのポストから、accounts_json の中身を作る。本文とツリーの両方にある人は本文の方に入れる。"""
    me = (author_username or "").lower()
    seen, out = {}, []
    sources = [(post, None)] + [(t, "tree") for t in tree_posts]
    for p, origin in sources:
        for h, name in find(p.get("text"), bool(p.get("reply_to_id"))):
            key = h.lower()
            if key == me:
                continue
            if key in seen:
                if name and not seen[key]["name"]:
                    seen[key]["name"] = name
                continue
            a = {"handle": h, "name": name[:60]}
            if origin:
                a["from"] = origin
            seen[key] = a
            out.append(a)
    return out


def candidates(d1, since, limit=MAX_POSTS_PER_RUN):
    """since 以降に集めて判定が済み、まだ拾っていない(または拾ったあとに遡りが済んだ)ポスト。"""
    return d1.query(
        "SELECT p.id, p.text, p.reply_to_id, t.ids_json AS tree_ids, u.username AS author_username FROM posts p "
        "LEFT JOIN users u ON u.id = p.author_id LEFT JOIN post_trees t ON t.post_id = p.id "
        "LEFT JOIN text_mentions m ON m.post_id = p.id "
        "WHERE p.status = 'judged' AND p.collected_at >= ? "
        "AND (m.post_id IS NULL OR (t.updated_at IS NOT NULL AND t.updated_at > m.scanned_at)) "
        "ORDER BY p.collected_at DESC, p.id DESC LIMIT ?", [since, limit])


def _ids(tree_ids):
    try:
        ids = json.loads(tree_ids or "[]")
    except ValueError:
        return []
    return [str(i) for i in ids] if isinstance(ids, list) else []


def load_posts(d1, ids):
    """ID のポストの本文を {id: 行} で返す。"""
    out = {}
    ids = list(dict.fromkeys(ids))
    for i in range(0, len(ids), 90):
        part = ids[i:i + 90]
        rows = d1.query(f"SELECT id, text, reply_to_id FROM posts WHERE id IN ({', '.join('?' for _ in part)})", part)
        out.update({r["id"]: r for r in rows})
    return out


def run(d1, production, now=None):
    """本文の ID を拾って保存し、要約の行を返す。試し実行では何もしない。"""
    if not production:
        return []
    lines = ["", "## 本文から X の ID を拾う", ""]
    now = now or store.now_iso()
    since = d1.get_meta(SINCE_KEY)
    if not since:  # 最初の実行: 今までのポストは拾わず、これから集めるポストだけにする
        d1.set_meta(SINCE_KEY, now)
        return lines + ["- 今回から、新しく集めたポストの本文の ID を拾います(今までのポストは対象外)"]
    # 一覧に出る 14 日より前のポストは探さない(D1 の読み取り行数を増やさないため)
    since = max(since, store.days_ago_iso(LIST_DAYS, datetime.fromisoformat(now)))
    posts = candidates(d1, since)
    if not posts:
        return lines + ["- 新しく拾うポストはありませんでした"]
    trees = {p["id"]: _ids(p.get("tree_ids")) for p in posts}
    texts = load_posts(d1, [i for ids in trees.values() for i in ids])
    rows, found, with_any = [], 0, 0
    for p in posts:
        tree_posts = [texts[i] for i in trees[p["id"]] if i in texts]  # 古い順(root が先頭)
        accounts = collect_accounts(p, tree_posts, p.get("author_username"))
        found += len(accounts)
        with_any += bool(accounts)
        rows.append([p["id"], now, json.dumps(accounts, ensure_ascii=False)])
    d1.insert_many("text_mentions", ["post_id", "scanned_at", "accounts_json"], rows, conflict="OR REPLACE")
    lines.append(f"- 拾ったポスト: {len(posts)}件(ID があった {with_any}件)、見つかった ID {found}個")
    return lines
