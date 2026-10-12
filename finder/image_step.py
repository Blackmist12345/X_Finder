"""N7 参加者情報(の一部): 対象になった案件の添付画像から、メンバー表などに載っている X のアカウントを AI で読む。

判定(N4)で対象になったポストだけを読む(画像は文字より高いので、絞り込んだあとに行う)。
読んだポストは image_reads に 1 行残し、二度は読まない。失敗しても収集・判定は止めない。
"""
import base64
import json
import re

import requests

from finder import judge, store

MAX_POSTS_PER_RUN = 30  # 1 回の実行で読むポストの上限(たまっていた分で費用が跳ねないように)
MAX_IMAGES = 4  # X の 1 ポストの画像の上限
MAX_IMAGE_BYTES = 4_500_000  # Claude に渡せる 1 枚の大きさ(5MB)より少し小さく
MEDIA_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}
HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")

PROMPT = """これは X(旧Twitter)の撮影募集ポストに添付された画像です。
画像の中に書かれている X のアカウント(@ で始まる ID。メンバー表・参加者一覧・キャスト表・連絡先などに載っているもの)を、全部読み取ってください。
- handle は @ を除いた ID だけ(英数字と _、15文字まで)。画像に書かれているとおりに読み、推測で補わないでください。
- name はその ID の近くに書かれている名前やキャラクター名(なければ空文字)。
- X 以外(Instagram・TikTok など)だと分かるものは入れないでください。
- アカウントが 1 つもなければ、空の配列を返してください。
"""

TOOL = {
    "name": "submit_accounts",
    "description": "画像から読み取った X のアカウントを提出する",
    "input_schema": {
        "type": "object",
        "properties": {"accounts": {"type": "array", "items": {
            "type": "object",
            "properties": {"handle": {"type": "string"}, "name": {"type": "string"}},
            "required": ["handle", "name"]}}},
        "required": ["accounts"],
    },
}


def candidates(d1, limit=MAX_POSTS_PER_RUN):
    """判定で対象になり、画像があり、まだ読んでいないポスト(新しい順)。"""
    return d1.query(
        "SELECT p.id, p.media_json, u.username AS author_username FROM posts p LEFT JOIN users u ON u.id = p.author_id "
        "WHERE p.status = 'judged' AND p.media_json IS NOT NULL AND p.media_json NOT IN ('', '[]') "
        "AND NOT EXISTS (SELECT 1 FROM image_reads i WHERE i.post_id = p.id) "
        "ORDER BY p.collected_at DESC, p.id DESC LIMIT ?", [limit])


def large_url(url):
    """pbs.twimg.com の画像は ?name=large で大きい版になる(小さい文字を読むため)。"""
    if "pbs.twimg.com/media/" in url and "?" not in url:
        return url + "?name=large"
    return url


def fetch_image(s, url):
    """画像を取ってきて Claude に渡す形にする。取れなければ None。"""
    try:
        res = s.get(large_url(url), timeout=30)
    except requests.RequestException:
        return None
    if res.status_code != 200 or not res.content or len(res.content) > MAX_IMAGE_BYTES:
        return None
    kind = (res.headers.get("content-type") or "").split(";")[0].strip()
    if kind not in MEDIA_TYPES.values():
        kind = MEDIA_TYPES.get(url.rsplit(".", 1)[-1].split("?")[0].lower(), "image/jpeg")
    return {"type": "image", "source": {"type": "base64", "media_type": kind, "data": base64.b64encode(res.content).decode()}}


def clean(accounts, author_username=None):
    """AI の返事から、正しい形の ID だけを重複なしで残す。投稿者本人は外す。"""
    seen, out = set(), []
    me = (author_username or "").lower()
    for a in accounts if isinstance(accounts, list) else []:
        if not isinstance(a, dict):
            continue
        handle = str(a.get("handle") or "").strip().lstrip("@＠").strip()
        if not HANDLE.match(handle) or handle.lower() in seen or handle.lower() == me:
            continue
        seen.add(handle.lower())
        out.append({"handle": handle, "name": str(a.get("name") or "").strip()[:60]})
    return out


def read_accounts(s, api_key, model, images):
    """画像をまとめて 1 回で AI に読ませる。(アカウントの配列, 入力トークン, 出力トークン)。"""
    body = {"model": model, "max_tokens": 4000, "tools": [TOOL], "tool_choice": {"type": "tool", "name": "submit_accounts"},
            "messages": [{"role": "user", "content": images + [{"type": "text", "text": PROMPT}]}]}
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    data = judge._post(s, judge.CLAUDE_API, headers, body)
    usage = data.get("usage") or {}
    blocks = [b for b in data.get("content", []) if b.get("type") == "tool_use"]
    if not blocks or not isinstance(blocks[0].get("input"), dict):
        raise judge.JudgeError(f"AI の返事を読めませんでした: {str(data)[:300]}")
    return blocks[0]["input"].get("accounts"), usage.get("input_tokens", 0), usage.get("output_tokens", 0)


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
    t_in = t_out = found = failed = 0
    for p in posts:
        urls = json.loads(p.get("media_json") or "[]")[:MAX_IMAGES]
        images = [img for img in (fetch_image(s, u) for u in urls) if img]
        accounts, error = [], None
        if not images:
            error = "画像を取得できませんでした"
        else:
            try:
                raw, i, o = read_accounts(s, api_key, model, images)
                t_in, t_out = t_in + i, t_out + o
                accounts = clean(raw, p.get("author_username"))
            except judge.JudgeError as e:
                if "再試行後も" in str(e):  # 混んでいるだけなら次の実行でもう一度読む
                    failed += 1
                    continue
                error = str(e)[:300]
        if error:
            failed += 1
        found += len(accounts)
        d1.query("INSERT OR REPLACE INTO image_reads (post_id, read_at, model, images, accounts_json, error) VALUES (?, ?, ?, ?, ?, ?)",
                 [p["id"], store.now_iso(), model, len(images), json.dumps(accounts, ensure_ascii=False), error])

    usd = judge.cost_usd(model, t_in, t_out)
    if t_in or t_out:
        store.record_cost(d1, judge.provider(model), t_in + t_out, usd, f"{model} 画像 {len(posts)}件")
    rate = config["basic"]["為替レート(円/ドル)"]
    lines.append(f"- 画像を読んだポスト: {len(posts)}件、見つかったアカウント {found}人"
                 + (f"、読めなかった {failed}件" if failed else "")
                 + f"、費用 ${usd:.4f}(約{usd * rate:.2f}円)")
    return lines
