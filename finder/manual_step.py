"""手動追加: X Finder の設定で URL を貼ったポストを取り込む(Latte 決定 2026-11-16)。

画面(web)は受付の表 manual_adds に 1 行書くだけ(画面の Worker には X の鍵を置かないため)。
収集の実行のたびに、ここで受付のポストを TwitterAPI.io の Get Tweets by IDs で取り、
source='manual'・status='new' で保存する。

  - ブラックリストの除外単語(検索除外語)も、収集時の事前フィルタ(フォロワー数など)もかけない
  - ブラックリストの人のポストは追加しない(一覧に出ない作りのため)。受付に「ブラックリストの人」と返す
  - すでに一覧に出ているポストは何も変えず「すでにあります」と返す
  - 収集はしたが除外した・AI が対象外にした・引用元として保存しただけのポストは、手動追加として取り込み直す
  - 取り込んだあとは、ふつうのポストと同じく 遡り → AI 判定(20件たまるのを待たない)→ 画像・アカウント読み取り と進む
  - 手動追加のポストは、AI の判定に関わらず一覧に出す(judge_step を参照)
"""
import json

from finder import collect, filters, store, tree_step

MAX_PER_RUN = 20   # 1 回の収集で取り込む上限(1 件約 0.02 円)
MAX_TRIES = 3      # 取りに行けなかったとき、この回数で諦める
LIST_DAYS = 14     # 一覧に出るのは集めてから 14 日(web の listCases と同じ)
KEEP_STATES = ("accepted", "hold")  # 14 日を過ぎても一覧に残る状態(web と同じ)
SOURCE = "manual"


def pending(d1):
    return d1.query("SELECT post_id, url, tries FROM manual_adds WHERE status = 'pending' "
                    "ORDER BY requested_at LIMIT ?", [MAX_PER_RUN])


def finish(d1, post_id, status, note, now):
    d1.query("UPDATE manual_adds SET status = ?, note = ?, done_at = ? WHERE post_id = ?", [status, note, now, post_id])


def visible(d1, post_id, now=None):
    """そのポストが今の一覧に出ているなら、その状態(new / checking / …)を返す。出ていなければ None。"""
    rows = d1.query("SELECT p.status, p.collected_at, r.state FROM posts p LEFT JOIN reviews r ON r.post_id = p.id "
                    "WHERE p.id = ?", [post_id])
    if not rows or rows[0]["status"] != "judged":
        return None
    r = rows[0]
    if (r["collected_at"] or "") >= store.days_ago_iso(LIST_DAYS) or r["state"] in KEEP_STATES:
        return r["state"] or "new"
    return None


def take_in(d1, tweet, words, now):
    """取ったポストを手動追加として保存する。もう D1 にある(除外した・対象外・引用元)ポストも新着として取り込み直す。"""
    tid = str(tweet.get("id"))
    hits = filters.word_hits(tweet.get("text", ""), words)  # 点数表の語は点数に使うので数える(除くためではない)
    posts, full, partial, links = store.extract([tweet], {tid: ("new", None, hits)}, now)
    store.save(d1, posts, full, partial, links)
    store.save_links(d1, store.link_rows([tweet], now))
    was_judged = d1.query("SELECT status FROM posts WHERE id = ?", [tid])[0]["status"] == "judged"
    # 一覧から外れていた判定済みのポスト(14 日を過ぎた)は、判定はそのままで集めた日時だけ新しくする
    status = "judged" if was_judged else "new"
    d1.query("UPDATE posts SET source = ?, status = ?, skip_reason = NULL, penalty_json = ?, collected_at = ? WHERE id = ?",
             [SOURCE, status, json.dumps(hits, ensure_ascii=False) if hits else None, now, tid])
    d1.query("DELETE FROM reviews WHERE post_id = ?", [tid])  # 状態は新着から
    return status


def run(d1, config, production, api_key, session=None, now=None):
    """受付にあるポストを取り込み、Actions の結果に足す行を返す。"""
    if not production:
        return []
    items = pending(d1)
    if not items:
        return []
    lines = ["", "## 手動追加", ""]
    if not api_key:
        return lines + ["- ⚠️ TWITTERAPI_KEY が設定されていないので、手動追加のポストは次の収集で取り込みます"]
    now = now or store.now_iso()

    todo = []
    for it in items:
        state = visible(d1, it["post_id"])
        if state:
            finish(d1, it["post_id"], "exists", f"すでに一覧にあります({state})", now)
        else:
            todo.append(it)
    counts = {"added": 0, "exists": len(items) - len(todo), "blacklisted": 0, "not_found": 0, "failed": 0}
    if todo:
        try:
            got, calls, returned = tree_step.fetch(api_key, [it["post_id"] for it in todo], session)
        except tree_step.TreeError as e:  # 次の収集でもう一度(MAX_TRIES 回まで)
            for it in todo:
                tries = (it.get("tries") or 0) + 1
                if tries >= MAX_TRIES:
                    finish(d1, it["post_id"], "failed", f"取りに行けませんでした: {str(e)[:150]}", now)
                    counts["failed"] += 1
                else:
                    d1.query("UPDATE manual_adds SET tries = ?, note = ? WHERE post_id = ?",
                             [tries, f"取りに行けませんでした(次の収集でもう一度): {str(e)[:150]}", it["post_id"]])
            return lines + [f"- ⚠️ 手動追加のポストを取りに行けませんでした: {e}"]
        usd = collect.cost_usd(returned, calls)
        store.record_cost(d1, "twitterapi.io", returned, usd, "手動追加")
        blocked = store.blacklisted_ids(d1)
        words = config.get("words", [])
        for it in todo:
            tw = got.get(it["post_id"])
            if not tw:
                finish(d1, it["post_id"], "not_found", "ポストが見つかりません(削除・非公開・鍵アカウントかもしれません)", now)
                counts["not_found"] += 1
            elif str((tw.get("author") or {}).get("id")) in blocked:
                finish(d1, it["post_id"], "blacklisted", "ブラックリストの人のポストなので追加しませんでした", now)
                counts["blacklisted"] += 1
            else:
                status = take_in(d1, tw, words, now)
                finish(d1, it["post_id"], "added", "追加しました" if status == "new" else "一覧に戻しました", now)
                counts["added"] += 1
        rate = config["basic"]["為替レート(円/ドル)"]
        lines.append(f"- 費用: 約{usd * rate:.2f}円(呼び出し {calls}回)")
    labels = {"added": "追加", "exists": "すでに一覧にあった", "blacklisted": "ブラックリストの人", "not_found": "見つからない", "failed": "諦めた"}
    lines.insert(3, "- 受付 " + f"{len(items)}件: " + "、".join(f"{labels[k]} {v}件" for k, v in counts.items() if v))
    return lines
