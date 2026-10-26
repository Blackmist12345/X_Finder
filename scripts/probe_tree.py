"""試作: 引用元・返信元を root まで遡ると、何件取りに行き、いくらかかるかを調べる。

D1 は読むだけで、何も書かない。結果は件数だけを Actions の結果に出す(ポストの本文や名前は出さない)。

調べること:
  1. N3 を通ったポストのうち、返信・引用がどれくらいあるか(D1 だけで分かる。無料)
  2. 引用元(quoted_tweet)の中に、さらに返信先 ID や引用元が入ってくるか
  3. 実際に root まで遡ったときの段数・取りに行った件数・費用・ツリー内の画像の数

環境変数: CLOUDFLARE_ACCOUNT_ID / D1_DATABASE_ID / CLOUDFLARE_API_TOKEN / TWITTERAPI_KEY
          SAMPLE(遡るポストの数、既定 40) / DAYS(調べる日数、既定 7)
"""
import json
import os
import sys
import time
from collections import Counter

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from finder import collect, store  # noqa: E402
from finder.d1 import D1  # noqa: E402

URL = "https://api.twitterapi.io/twitter/tweets"
MAX_IDS = 50
MAX_DEPTH = 10
MAX_FETCH = 400          # 取りに行く件数の上限(約9円)
NESTED_CHECK = 20        # 引用元の入れ子を確かめるため、取り直す引用元の数
YEN_PER_USD = 150        # 目安の為替(費用の表示用)
PASSED = ("new", "judged", "rejected", "judge_error")  # N3 を通ったポストの状態


class Fetcher:
    def __init__(self, api_key):
        self.http = requests.Session()
        self.headers = {"X-API-Key": api_key}
        self.calls = 0
        self.returned = 0

    def get(self, ids):
        """ids のポストを返す({id: tweet})。見られないものは入らない。"""
        out = {}
        for i in range(0, len(ids), MAX_IDS):
            part = ids[i:i + MAX_IDS]
            data = None
            for attempt in range(4):
                self.calls += 1
                try:
                    res = self.http.get(URL, headers=self.headers, params={"tweet_ids": ",".join(part)}, timeout=30)
                except requests.RequestException:
                    time.sleep(2 ** (attempt + 1))
                    continue
                if res.status_code == 200:
                    data = res.json()
                    break
                if res.status_code == 429 or res.status_code >= 500:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise SystemExit(f"TwitterAPI.io がエラーを返しました ({res.status_code})")
            if data is None:
                raise SystemExit("TwitterAPI.io から再試行後も答えがありませんでした")
            for t in data.get("tweets") or []:
                if t.get("id"):
                    out[str(t["id"])] = t
            self.returned += len(data.get("tweets") or [])
            time.sleep(0.5)
        return out

    def usd(self):
        return collect.cost_usd(self.returned, self.calls)


def node_from_tweet(t):
    q = t.get("quoted_tweet") or None
    return {"reply_to": t.get("inReplyToId") or None, "quoted": str(q["id"]) if q and q.get("id") else None,
            "images": len(store.media_urls(t))}


def node_from_row(r):
    return {"reply_to": r.get("reply_to_id") or None, "quoted": r.get("quoted_id") or None,
            "images": len(json.loads(r.get("media_json") or "[]"))}


def load_rows(d1, ids):
    out = {}
    ids = list(ids)
    for i in range(0, len(ids), 90):
        part = ids[i:i + 90]
        rows = d1.query(f"SELECT id, reply_to_id, quoted_id, media_json, source FROM posts "
                        f"WHERE id IN ({', '.join('?' for _ in part)})", part)
        out.update({r["id"]: r for r in rows})
    return out


def share_lines(d1, since):
    marks = ", ".join("?" for _ in PASSED)
    base = f"FROM posts p WHERE p.source = 'search' AND p.status IN ({marks}) AND p.collected_at >= ?"
    args = [*PASSED, since]
    total = d1.query(f"SELECT COUNT(*) AS n {base}", args)[0]["n"]
    reply = d1.query(f"SELECT COUNT(*) AS n {base} AND p.reply_to_id IS NOT NULL", args)[0]["n"]
    quote = d1.query(f"SELECT COUNT(*) AS n {base} AND p.quoted_id IS NOT NULL", args)[0]["n"]
    both = d1.query(f"SELECT COUNT(*) AS n {base} AND p.reply_to_id IS NOT NULL AND p.quoted_id IS NOT NULL", args)[0]["n"]
    q_rows = d1.query("SELECT COUNT(*) AS n, SUM(reply_to_id IS NOT NULL) AS r, SUM(quoted_id IS NOT NULL) AS q "
                      "FROM posts WHERE source = 'quoted' AND collected_at >= ?", [since])[0]
    return total, reply, quote, both, q_rows


def pct(a, b):
    return f"{a / b * 100:.0f}%" if b else "-"


def main():
    sample = int(os.environ.get("SAMPLE") or 40)
    days = int(os.environ.get("DAYS") or 7)
    api_key = os.environ.get("TWITTERAPI_KEY")
    if not api_key:
        raise SystemExit("TWITTERAPI_KEY が設定されていません")
    d1 = D1.from_env()
    fetcher = Fetcher(api_key)
    since = store.days_ago_iso(days)
    lines = [f"# 試作: 引用元・返信元を root まで遡る(直近{days}日、D1 は読むだけ)", ""]

    # 1. 返信・引用の割合(無料)
    total, reply, quote, both, q_rows = share_lines(d1, since)
    lines += ["## 1. N3 を通ったポストの返信・引用の割合", "",
              f"- N3 を通ったポスト: {total}件(1日あたり約{total / days:.0f}件)",
              f"- 返信: {reply}件({pct(reply, total)}) / 引用: {quote}件({pct(quote, total)}) / 両方: {both}件",
              f"- 一緒に届いた引用元 {q_rows['n'] or 0}件のうち、返信先 ID が入っていた {q_rows['r'] or 0}件、"
              f"さらに引用元が入っていた {q_rows['q'] or 0}件", ""]

    # 2. 引用元の入れ子を、取り直して確かめる
    q_sample = d1.query("SELECT id, reply_to_id, quoted_id FROM posts WHERE source = 'quoted' AND collected_at >= ? "
                        "ORDER BY collected_at DESC LIMIT ?", [since, NESTED_CHECK])
    fetched_q = fetcher.get([r["id"] for r in q_sample]) if q_sample else {}
    miss_reply = sum(1 for r in q_sample if r["id"] in fetched_q and fetched_q[r["id"]].get("inReplyToId") and not r["reply_to_id"])
    miss_quote = sum(1 for r in q_sample if r["id"] in fetched_q and (fetched_q[r["id"]].get("quoted_tweet") or {}).get("id")
                     and not r["quoted_id"])
    real_reply = sum(1 for t in fetched_q.values() if t.get("inReplyToId"))
    real_quote = sum(1 for t in fetched_q.values() if (t.get("quoted_tweet") or {}).get("id"))
    lines += ["## 2. 引用元の中の入れ子は、検索結果に付いてくるか", "",
              f"- 引用元 {len(q_sample)}件を取り直した(見られたもの {len(fetched_q)}件)",
              f"- 取り直すと返信だった {real_reply}件のうち、検索結果では返信先 ID が抜けていた {miss_reply}件",
              f"- 取り直すと引用だった {real_quote}件のうち、検索結果では引用元が抜けていた {miss_quote}件", ""]

    # 3. root まで遡る
    starts = d1.query(
        f"SELECT p.id, p.reply_to_id, p.quoted_id, p.media_json FROM posts p "
        f"LEFT JOIN posts q ON q.id = p.quoted_id "
        f"WHERE p.source = 'search' AND p.status IN ({', '.join('?' for _ in PASSED)}) AND p.collected_at >= ? "
        f"AND (p.reply_to_id IS NOT NULL OR q.reply_to_id IS NOT NULL OR q.quoted_id IS NOT NULL OR p.quoted_id IS NOT NULL) "
        f"ORDER BY p.collected_at DESC LIMIT ?", [*PASSED, since, sample])
    eligible = d1.query(
        f"SELECT COUNT(*) AS n FROM posts p LEFT JOIN posts q ON q.id = p.quoted_id "
        f"WHERE p.source = 'search' AND p.status IN ({', '.join('?' for _ in PASSED)}) AND p.collected_at >= ? "
        f"AND (p.reply_to_id IS NOT NULL OR q.reply_to_id IS NOT NULL OR q.quoted_id IS NOT NULL OR p.quoted_id IS NOT NULL)",
        [*PASSED, since])[0]["n"]
    # 検索結果の引用元に入れ子が抜けていなければ D1 の行をそのまま使い、抜けていれば取り直す
    trust_quoted = miss_reply + miss_quote == 0
    nodes = {r["id"]: node_from_row(r) for r in starts}
    # 引用元は検索結果に付いてくる(D1 にある)。入れ子が抜けていることがあるので、取り直した分で上書きする
    for qid, t in fetched_q.items():
        nodes[qid] = node_from_tweet(t)
    known_rows = {}
    gone = set()
    fetched_ids = set()
    budget_hit = False

    def parents(pid):
        n = nodes.get(pid)
        return [x for x in ((n or {}).get("reply_to"), (n or {}).get("quoted")) if x]

    frontier = {p for s in starts for p in parents(s["id"])}
    for _ in range(MAX_DEPTH):
        need = [p for p in frontier if p not in nodes and p not in gone]
        if need:
            rows = load_rows(d1, need)
            for pid, r in rows.items():
                known_rows[pid] = r
                if r["source"] != "quoted" or trust_quoted:
                    nodes[pid] = node_from_row(r)
            need = [p for p in need if p not in nodes]
            room = MAX_FETCH - len(fetched_ids)
            if len(need) > room:
                budget_hit = True
                need = need[:max(room, 0)]
            got = fetcher.get(need) if need else {}
            fetched_ids |= set(need)
            for pid in need:
                if pid in got:
                    nodes[pid] = node_from_tweet(got[pid])
                    q = got[pid].get("quoted_tweet") or None
                    if q and q.get("id") and str(q["id"]) not in nodes:
                        nodes[str(q["id"])] = node_from_tweet(q)  # 同梱の引用元(無料)
                elif pid in known_rows:
                    nodes[pid] = node_from_row(known_rows[pid])
                else:
                    gone.add(pid)
        frontier = {p for pid in frontier if pid in nodes for p in parents(pid)}
        if not frontier:
            break

    depths, endings, tree_sizes, tree_images = Counter(), Counter(), [], []
    for s in starts:
        seen, level, depth, ending = {s["id"]}, [s["id"]], 0, "root"
        while True:
            nxt = [p for pid in level for p in parents(pid) if p not in seen]
            if not nxt:
                break
            depth += 1
            if depth > MAX_DEPTH:
                ending, depth = "上限", MAX_DEPTH
                break
            seen.update(nxt)
            if any(p in gone for p in nxt):
                ending = "見られない親あり"
            if any(p not in nodes and p not in gone for p in nxt):
                ending = "費用の上限で中断"
            level = [p for p in nxt if p in nodes]
        depths[depth] += 1
        endings[ending] += 1
        tree_sizes.append(len(seen) - 1)
        tree_images.append(sum(nodes[p]["images"] for p in seen if p != s["id"] and p in nodes))

    n = len(starts) or 1
    lines += ["## 3. root まで遡った結果", "",
              f"- 遡る対象(返信か引用のある、N3 を通ったポスト): {eligible}件({pct(eligible, total)})、うち新しい順に {len(starts)}件を遡った",
              "- 検索結果の引用元の入れ子: " + ("抜けがないので、D1 の引用元をそのまま使った" if trust_quoted else "抜けがあるので、引用元も取り直した"),
              "- 段数の分布: " + "、".join(f"{d}段 {c}件" for d, c in sorted(depths.items())),
              "- 止まった理由: " + "、".join(f"{k} {v}件" for k, v in endings.most_common()),
              f"- ツリーのポスト数(元のポストを除く): 平均 {sum(tree_sizes) / n:.1f}件、最大 {max(tree_sizes or [0])}件",
              f"- ツリー内の画像: 平均 {sum(tree_images) / n:.1f}枚、最大 {max(tree_images or [0])}枚、"
              f"画像のあるツリー {sum(1 for x in tree_images if x)}件",
              f"- API で取りに行ったポスト: {len(fetched_ids)}件(うち見られなかった {len(gone)}件)"
              + ("(費用の上限に達しました)" if budget_hit else ""), ""]

    usd = fetcher.usd()
    walked_per_post = len(fetched_ids) / n
    month_tweets = eligible / days * 30 * walked_per_post
    lines += ["## 4. 費用", "",
              f"- 今回: 呼び出し {fetcher.calls}回、返ってきたポスト {fetcher.returned}件、約{usd * YEN_PER_USD:.1f}円(1ドル{YEN_PER_USD}円)",
              f"- 遡ったポスト1件あたり新しく取りに行った件数: {walked_per_post:.1f}件",
              f"- 月の見込み(取得のみ): 約{month_tweets * 0.00015 * YEN_PER_USD:.0f}円"
              "(1日の N3 通過件数 × 返信か引用の割合 × 1件あたりの取得件数 × 30日。D1 にもうある親は取らない分、実際は少なめになる)",
              f"- 月の見込み(ツリー内の画像を判定、1枚約0.024円): 約{eligible / days * 30 * (sum(tree_images) / n) * 0.024:.0f}円", ""]

    text = "\n".join(lines) + "\n"
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)


if __name__ == "__main__":
    main()
