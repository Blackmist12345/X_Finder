"""N7 参加者情報(の一部): 対象になった案件の添付画像から、メンバー表などに載っている X のアカウントを AI で読む。
同じ呼び出しで、画像に書かれた開催場所(都道府県・会場・イベント名)も読む。

判定(N4)で対象になったポストだけを読む。判定でも画像は読む(募集の中身のため)が、アカウントはここで読む。
本文の画像に加えて、遡った引用元・返信元(ツリー)の画像も root に近い方から読む。ツリーの画像から読んだ人には from: "tree" を付ける。
読んだポストは image_reads に 1 行残し、二度は読まない。失敗しても収集・判定は止めない。
判定で場所が本文からもイベント表からも分からなかった(プロフィールから推定・不明)案件は、
画像から分かった都道府県に置き換えて、点数を計算し直す。
"""
import json
import re

import requests

from finder import judge, store
from finder.score import score

MAX_POSTS_PER_RUN = 30  # 1 回の実行で読むポストの上限(たまっていた分で費用が跳ねないように)
MAX_IMAGES = 4  # X の 1 ポストの画像の上限
MAX_TREE_IMAGES = judge.MAX_TREE_IMAGES  # ツリーの画像(判定と同じく root に近い方から)
HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")

PROMPT = """これは X(旧Twitter)の撮影募集ポストに添付された画像です。
画像の中に書かれている X のアカウント(@ で始まる ID。メンバー表・参加者一覧・キャスト表・連絡先などに載っているもの)を、全部読み取ってください。
- handle は @ を除いた ID だけ(英数字と _、15文字まで)。画像に書かれているとおりに読み、推測で補わないでください。
- name はその ID の近くに書かれている名前やキャラクター名(なければ空文字)。
- X 以外(Instagram・TikTok など)だと分かるものは入れないでください。
- from は、そのアカウントが「本文の画像」と「ツリーの画像」のどちらに書かれていたか(各画像の前に名前を付けてあります)。両方にあれば「本文」。
- アカウントが 1 つもなければ、空の配列を返してください。

あわせて、撮影やイベントの開催場所が画像に書かれていれば読み取ってください(place)。
- prefecture: 開催場所の都道府県。地名・会場名・駅名から分かる場合だけ。分からなければ「不明」。
- venue: 会場名・地名(書かれているとおりに。なければ空文字)。
- event: イベント名(書かれているとおりに。略称でもよい。なければ空文字)。
"""

TOOL = {
    "name": "submit_accounts",
    "description": "画像から読み取った X のアカウントを提出する",
    "input_schema": {
        "type": "object",
        "properties": {
            "accounts": {"type": "array", "items": {
                "type": "object",
                "properties": {"handle": {"type": "string"}, "name": {"type": "string"},
                               "from": {"type": "string", "enum": ["本文", "ツリー"]}},
                "required": ["handle", "name"]}},
            "place": {"type": "object", "properties": {
                "prefecture": {"type": "string", "enum": judge.PREFECTURES + [judge.UNKNOWN_PLACE]},
                "venue": {"type": "string"}, "event": {"type": "string"}},
                "required": ["prefecture", "venue", "event"]},
        },
        "required": ["accounts", "place"],
    },
}


HAS_MEDIA = "{0}.media_json IS NOT NULL AND {0}.media_json NOT IN ('', '[]')"


def candidates(d1, limit=MAX_POSTS_PER_RUN):
    """判定で対象になり、本文かツリーに画像があり、まだ読んでいないポスト(新しい順)。"""
    return d1.query(
        "SELECT p.id, p.media_json, t.ids_json AS tree_ids, u.username AS author_username FROM posts p "
        "LEFT JOIN users u ON u.id = p.author_id LEFT JOIN post_trees t ON t.post_id = p.id "
        f"WHERE p.status = 'judged' AND ({HAS_MEDIA.format('p')} OR EXISTS (SELECT 1 FROM json_each(t.ids_json) j "
        f"JOIN posts tp ON tp.id = j.value WHERE {HAS_MEDIA.format('tp')})) "
        "AND NOT EXISTS (SELECT 1 FROM image_reads i WHERE i.post_id = p.id) "
        "ORDER BY p.collected_at DESC, p.id DESC LIMIT ?", [limit])


def _urls(media_json):
    try:
        urls = json.loads(media_json or "[]")
    except ValueError:
        return []
    return [u for u in urls if isinstance(u, str)] if isinstance(urls, list) else []


def tree_urls(d1, tree_ids):
    """ツリーの画像 URL を root に近い方から MAX_TREE_IMAGES 枚まで。"""
    ids = _urls(tree_ids)  # 遡ったポストの ID(古い順)
    if not ids:
        return []
    rows = d1.query(f"SELECT id, media_json FROM posts WHERE id IN ({', '.join('?' for _ in ids)})", ids)
    media = {r["id"]: _urls(r["media_json"]) for r in rows}
    return [u for i in ids for u in media.get(i, [])][:MAX_TREE_IMAGES]


def fetch_image(s, url):
    """画像を取ってきて Claude に渡す形にする。取れなければ None。"""
    got = judge.fetch_image(s, url)
    if not got:
        return None
    return {"type": "image", "source": {"type": "base64", "media_type": got[0], "data": got[1]}}


def clean(accounts, author_username=None):
    """AI の返事から、正しい形の ID だけを重複なしで残す。投稿者本人は外す。

    本文の画像とツリーの画像の両方にある人は本文の方に入れる。ツリーの画像だけの人には from: "tree" を付ける。
    """
    seen, out = set(), []
    me = (author_username or "").lower()
    accounts = [a for a in accounts if isinstance(a, dict)] if isinstance(accounts, list) else []
    for a in sorted(accounts, key=lambda a: a.get("from") == "ツリー"):
        handle = str(a.get("handle") or "").strip().lstrip("@＠").strip()
        if not HANDLE.match(handle) or handle.lower() in seen or handle.lower() == me:
            continue
        seen.add(handle.lower())
        out.append({"handle": handle, "name": str(a.get("name") or "").strip()[:60]})
        if a.get("from") == "ツリー":
            out[-1]["from"] = "tree"
    return out


def read_accounts(s, api_key, model, images):
    """画像(と、その前に付けた画像の名前)をまとめて 1 回で AI に読ませる。(アカウントの配列, 場所, 入力トークン, 出力トークン)。"""
    body = {"model": model, "max_tokens": 4000, "tools": [TOOL], "tool_choice": {"type": "tool", "name": "submit_accounts"},
            "messages": [{"role": "user", "content": images + [{"type": "text", "text": PROMPT}]}]}
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    data = judge._post(s, judge.CLAUDE_API, headers, body)
    usage = data.get("usage") or {}
    blocks = [b for b in data.get("content", []) if b.get("type") == "tool_use"]
    if not blocks or not isinstance(blocks[0].get("input"), dict):
        raise judge.JudgeError(f"AI の返事を読めませんでした: {str(data)[:300]}")
    out = blocks[0]["input"]
    place = out.get("place") if isinstance(out.get("place"), dict) else {}
    return out.get("accounts"), place, usage.get("input_tokens", 0), usage.get("output_tokens", 0)


def place_from_image(place, events):
    """画像から読んだ場所を (都道府県, 場所の欄に入れる文字) にする。都道府県が分からなければ None。

    画像に都道府県がなくても、イベント名がイベントの一覧に当たれば、その開催地を使う。
    """
    place = place or {}
    venue, event = str(place.get("venue") or "").strip(), str(place.get("event") or "").strip()
    label = "・".join(x for x in (event, venue) if x)[:60]
    pref = place.get("prefecture")
    if pref in judge.PREFECTURES:
        return pref, label
    for _, ev in judge.match_events(f"{event} {venue}", events):
        if ev.get("prefecture") in judge.PREFECTURES:
            return ev["prefecture"], label or judge.event_label(ev)
    return None


def apply_place(d1, post_id, found, config):
    """判定で場所が分からなかった(プロフィールから推定・不明)案件を、画像の場所に置き換えて採点し直す。置き換えたら True。"""
    rows = d1.query("SELECT j.raw_json, j.prefecture_source, j.place, p.text, p.penalty_json FROM judgments j "
                    "JOIN posts p ON p.id = j.post_id WHERE j.post_id = ? AND j.reject_reason IS NULL", [post_id])
    if not rows or rows[0]["prefecture_source"] not in (judge.PROFILE_SOURCE, judge.UNKNOWN_PLACE, None):
        return False
    row = rows[0]
    result = json.loads(row["raw_json"] or "{}")
    pref, label = found
    result.update(prefecture=pref, prefecture_source=judge.IMAGE_SOURCE)
    if not result.get("place") and label:
        result["place"] = label
    categories = {w["word"]: w.get("category") for w in config.get("words", [])}
    penalties = json.loads(row["penalty_json"] or "[]")
    for p in penalties:
        p.setdefault("category", categories.get(p["word"], "その他"))
    total, detail = score(result, penalties, row["text"], config)
    d1.query("UPDATE judgments SET prefecture = ?, prefecture_source = ?, place = ?, score = ?, score_json = ?, critical = 0, "
             "critical_reason = NULL, raw_json = ? WHERE post_id = ?",
             [pref, judge.IMAGE_SOURCE, result.get("place"), total, json.dumps(detail, ensure_ascii=False),
              json.dumps(result, ensure_ascii=False), post_id])
    return True


def run(d1, config, production, api_keys, session=None):
    """画像を読んで保存し、要約の行を返す。試し実行では何もしない。"""
    if not production:
        return []
    lines = ["", "## 画像からアカウントを読む", ""]
    model = config["basic"]["判定に使うAI"]
    if not model.startswith("claude"):
        model = judge.DEFAULT_MODEL  # 画像は Claude で読む
    api_key = (api_keys or {}).get(judge.KEY_NAMES["claude"])
    if not api_key:
        return lines + [f"- ⚠️ {judge.KEY_NAMES['claude']} が登録されていないので、画像は読みませんでした"]
    try:
        posts = candidates(d1)
    except Exception as e:
        return lines + [f"- ⚠️ 読む画像を探せませんでした: {e}"]
    if not posts:
        return lines + ["- 新しく読む画像はありませんでした"]

    s = session or requests.Session()
    t_in = t_out = found = failed = placed = 0
    for p in posts:
        urls = [(f"本文の画像{n}", u) for n, u in enumerate(_urls(p.get("media_json"))[:MAX_IMAGES], 1)]
        urls += [(f"ツリーの画像{n}", u) for n, u in enumerate(tree_urls(d1, p.get("tree_ids")), 1)]
        images, count = [], 0  # images は AI に渡す部品(画像の名前と画像を交互に)
        for label, u in urls:
            img = fetch_image(s, u)
            if img:
                images += [{"type": "text", "text": label}, img]
                count += 1
        accounts, error = [], None
        if not images:
            error = "画像を取得できませんでした"
        else:
            try:
                raw, place, i, o = read_accounts(s, api_key, model, images)
                t_in, t_out = t_in + i, t_out + o
                accounts = clean(raw, p.get("author_username"))
                where = place_from_image(place, config.get("events"))
                try:
                    if where and "points" in config and apply_place(d1, p["id"], where, config):
                        placed += 1
                except Exception as e:  # 場所の置き換えに失敗しても、アカウントは保存する
                    lines.append(f"- ⚠️ 画像の場所で採点し直せませんでした({p['id']}): {e}")
            except judge.JudgeError as e:
                if "再試行後も" in str(e):  # 混んでいるだけなら次の実行でもう一度読む
                    failed += 1
                    continue
                error = str(e)[:300]
        if error:
            failed += 1
        found += len(accounts)
        d1.query("INSERT OR REPLACE INTO image_reads (post_id, read_at, model, images, accounts_json, error) VALUES (?, ?, ?, ?, ?, ?)",
                 [p["id"], store.now_iso(), model, count, json.dumps(accounts, ensure_ascii=False), error])

    usd = judge.cost_usd(model, t_in, t_out)
    if t_in or t_out:
        store.record_cost(d1, judge.provider(model), t_in + t_out, usd, f"{model} 画像 {len(posts)}件")
    rate = config["basic"]["為替レート(円/ドル)"]
    lines.append(f"- 画像を読んだポスト: {len(posts)}件、見つかったアカウント {found}人"
                 + (f"、画像から場所が分かった {placed}件" if placed else "")
                 + (f"、読めなかった {failed}件" if failed else "")
                 + f"、費用 ${usd:.4f}(約{usd * rate:.2f}円)")
    return lines
