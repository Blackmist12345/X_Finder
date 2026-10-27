"""N4 AI 判定: ポストを、まとめて AI(Claude または Gemini)に読ませる。

画像のないポストは文字だけを 20 件ずつ(まとめて判定する件数)、画像のあるポストは画像込みで 5 件ずつ読ませる。
渡す画像は 1 件あたり、本文の画像 4 枚と、ツリー(遡った返信元・引用元)の画像を root に近い方から 4 枚まで。
AI には事実の読み取りだけを頼み、対象外にするかどうかは reject_reason() がルールで決める。
"""
import base64
import json
import re
import time
from datetime import datetime, timedelta, timezone

import requests

from finder.tables import TYPES, VARIES

class JudgeError(Exception):
    pass


GEMINI_API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
CLAUDE_API = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-haiku-5-5"

# 100万トークンあたりの料金(ドル)。(入力, 出力)。考える分のトークンも出力として課金される。
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
}
DEFAULT_PRICE = PRICES["gemini-3.5-flash-lite"]  # 表にない機種は高めの料金で数える

# 使う AI の会社ごとの、鍵の環境変数の名前。判定に使うAI の名前の先頭で決まる。
KEY_NAMES = {"claude": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY"}


def provider(model):
    for name in KEY_NAMES:
        if model.startswith(name):
            return name
    raise JudgeError(f"判定に使うAI「{model}」は使えません。claude- か gemini- で始まる名前にしてください")

PREFECTURES = [
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県", "茨城県", "栃木県", "群馬県",
    "埼玉県", "千葉県", "東京都", "神奈川県", "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県",
    "岐阜県", "静岡県", "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県", "奈良県", "和歌山県",
    "鳥取県", "島根県", "岡山県", "広島県", "山口県", "徳島県", "香川県", "愛媛県", "高知県", "福岡県",
    "佐賀県", "長崎県", "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
]
UNKNOWN_PLACE = "不明"
EVENT_SOURCE = "イベント表"
EVENT_GUESS_SOURCE = "イベント推定"  # イベントの一覧にないイベントの開催地を、AI が知っている範囲で補ったとき
IMAGE_SOURCE = "画像"  # 本文・ツリーの添付画像から読んだとき(判定のときと、判定のあとに image_step.py が読んだとき)
TREE_SOURCE = "ツリー"  # 本文ではなく、遡った返信元・引用元(ツリー)のポストから読んだとき
PROFILE_SOURCE = "プロフィール"  # 前の判定の分だけ。撮影場所はプロフィールから決めない(活動地域は画面で別に読む)
PLACE_SOURCES = ["本文", TREE_SOURCE, IMAGE_SOURCE, EVENT_SOURCE, EVENT_GUESS_SOURCE, UNKNOWN_PLACE]

SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "id": {"type": "STRING"},
            "wants_photographer": {"type": "BOOLEAN"},
            "official_or_job": {"type": "BOOLEAN"},
            "case_type": {"type": "STRING", "enum": TYPES},
            "is_cosplay": {"type": "BOOLEAN"},
            "works": {"type": "STRING"},
            "matched_works": {"type": "ARRAY", "items": {"type": "STRING"}},
            "prefecture": {"type": "STRING", "enum": PREFECTURES + [UNKNOWN_PLACE]},
            "prefecture_source": {"type": "STRING", "enum": PLACE_SOURCES},
            "place": {"type": "STRING"},
            "event": {"type": "STRING"},
            "event_venue": {"type": "STRING"},
            "event_prefecture": {"type": "STRING", "enum": PREFECTURES + [UNKNOWN_PLACE, VARIES]},
            "dates": {"type": "STRING"},
            "summary": {"type": "STRING"},
        },
        "required": ["id", "wants_photographer", "official_or_job", "case_type", "is_cosplay", "works",
                     "matched_works", "prefecture", "prefecture_source", "place", "event", "event_venue", "event_prefecture", "dates", "summary"],
    },
}

RULES = """あなたは、カメラマンを探している X(旧Twitter)のポストを見分ける係です。
下のポストそれぞれについて、書かれている事実を読み取り、指定の JSON 配列で返してください。
ポストの数だけ、id をそのまま付けて返してください。

- ツリー: ポストに「ツリー」が付いていれば、そのポストに至るまでの返信元・引用元を古い順に並べたもの。
  撮影の場所・会場・イベント・日程・作品は、ツリーの最初のポストにだけ書かれていることがあるので、ツリーも読む。
  ただし募集かどうか(wants_photographer)は、本文の投稿者(依頼主)が撮影者を探しているかで決める。
  「別の人」のポストは、依頼主の募集の中身(場所・イベント・日程)として使えるときだけ使う。
- 画像: ポストに画像が付いていれば、「本文の画像」「ツリー・最初の画像」のような名前のあとに続く。
  募集の詳細(日程・場所・会場・イベント・作品・キャラクター・募集の条件)は、メモのスクショや募集テンプレートの画像に
  書かれていることが多いので、画像の文字も読み、本文と同じように使う。どのポストの画像かは、名前のあとの id で分かる。
- wants_photographer: 投稿者(引用ポストなら引用元の募集者を含む)が、撮影してくれるカメラマン・撮影者を探していれば true。
  被写体やモデルだけを募集している、カメラマン自身が被写体を探している、募集の締め切り報告・お礼・雑談なら false。
  他人の募集への返信や質問(「まだ募集していますか」「立候補します」など)も false。
  「撮ってくれる方いませんか」「一緒に撮影に行ってくれる方」「推しカメ募集」のように、言い方が違っても
  自分を撮影してくれる人を探していれば true。迷う場合は true(見落としの方が困るため)。
- official_or_job: 企業や公式イベントの撮影スタッフ募集、仕事としての求人なら true。個人どうしの撮影なら false。
- case_type: 次のどれか1つ。
  コスプレ撮影 = コスプレの写真撮影(併せ・ロケ・スタジオ・イベント会場での個撮を含む)
  コスパフォ = コスプレでのパフォーマンスやダンスを、イベントの出演として撮る
  踊ってみた = コスプレで踊る動画を、自分たちでスタジオなどを借りて YouTube などに載せるために撮る
  ポートレート = コスプレではない人物撮影
  イベント = イベントの記録撮影
  その他 = 上のどれでもない
- is_cosplay: コスプレに関係する撮影なら true。
- works: 作品名・キャラクター名(分かる範囲。なければ空文字)。
  VTuber などはキャラ名の代わりに決まった絵文字(推しマーク)で書かれることがある。ポストに「絵文字の意味」が付いていれば、
  それを使ってキャラクター名を works に書く(例: 「にじさんじ 〇〇」)。同じ絵文字に候補が何人もあるときは、本文の流れから選ぶ。
  ただの飾りとして使われている絵文字なら無視する。
- matched_works: 撮影する作品(works・本文・絵文字の意味から判断。略称・キャラクター名からの判断も含む)が、下の「点数を付ける作品」の
  どれかに当たれば、その作品の名前を一覧の行頭の名前のとおりに書いて並べる。何作品でもよい。当たらなければ空の配列。
- prefecture: 撮影する場所(スタジオ・会場・イベントの開催地)の都道府県。次の順で判断する。
  1. 本文・引用元の本文・ツリーのポスト・画像に書かれた地名・会場名・駅名
  2. ポストに「イベントの意味」が付いていれば、そこに書かれた開催地(「回ごとにちがう」なら本文から判断する)
  3. 「イベントの意味」が付いていないイベントで撮影するなら、下の event_prefecture(そのイベントの開催地として知っている都道府県)
  どれでも分からなければ「不明」。投稿者プロフィールの地域・自己紹介は依頼主の活動地域で、撮影場所とは別なので使わない。
  全国のポストが混ざっているので、推測で東京などにしないこと。
- prefecture_source: prefecture をどこから判断したか(本文 / ツリー / 画像 / イベント表 / イベント推定 / 不明)。
  1 のうち本文・引用元の本文なら「本文」、ツリーのポストの文章から決めたら「ツリー」、画像から決めたら「画像」、
  上の 3 で決めたら「イベント推定」。
- place: 会場名やイベント名・地域(本文に書かれていれば。イベント表から分かった会場や、event_venue でもよい。なければ空文字)。
- event: 撮影するイベント(即売会・コスプレイベント・ライブ・展示会など)の名前を、本文に書かれているとおりに(略称でもよい)。
  「イベント」「併せ」「撮影会」のような一般的な言葉だけなら空文字。イベントでないなら空文字。
- event_venue: event の会場として知っている場所(例: 東京ビッグサイト)。確かでなければ空文字。
- event_prefecture: event の開催地として知っている都道府県。開催地が回ごとに変わるイベントなら「毎回ちがう」。
  確かに知っているときだけ書き、よく知らないイベント・聞いたことのないイベントは推測せず「不明」。event が空文字なら「不明」。
- dates: 撮影日や候補日(なければ空文字)。日付は「10/18(土)」のように月/日(曜日)で書き、何日もあれば「、」で並べ、続く日は「10/18(土)〜19(日)」と書く。
  「明日」「明後日」「今週の土曜」「来週の日曜」「今週末」「来週末」「3日後」のように日付が数字で書かれていないときは、
  そのポストの「投稿日時」を基準に月/日(曜日)に直し、後ろに元の言い方を添える(例: 「10/18(土)(今週の土曜)」)。
  「今週末」「来週末」は土日の 2 日。「今月中」「近いうち」「平日」のように日が決まらない言い方は直さず、そのまま短く書く。
- summary: 何の撮影の募集か、30文字以内の日本語。
"""


VS16 = "\ufe0f"  # 絵文字の見た目を変えるだけの文字。あってもなくても同じ絵文字として扱う


def emoji_index(emojis):
    """[{emoji, name, group}] を {絵文字: ["キャラ名(グループ)", ...]} にする。"""
    index = {}
    for e in emojis or []:
        key = (e.get("emoji") or "").replace(VS16, "").strip()
        if not key or not e.get("name"):
            continue
        label = e["name"] + (f"({e['group']})" if e.get("group") else "")
        if label not in index.setdefault(key, []):
            index[key].append(label)
    return index


def emoji_hints(text, index):
    """本文に出てくる、一覧にある絵文字の意味を返す。長い絵文字(何文字かを組み合わせたもの)から先に探す。"""
    rest = (text or "").replace(VS16, "")
    found = []
    for key in sorted(index, key=len, reverse=True):
        if key in rest:
            found.append(f"{key}={' / '.join(index[key])}")
            rest = rest.replace(key, " ")
    return found


def _alias_pattern(alias):
    """英数字だけの略称(TGS など)は、ほかの単語の一部に当たらないように前後を区切る。"""
    if re.fullmatch(r"[A-Za-z0-9 .!&'-]+", alias):
        return re.compile(r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?![A-Za-z0-9])", re.IGNORECASE)
    return re.compile(re.escape(alias))


def match_events(text, events):
    """本文に略称が出てくるイベントを返す。[(出てきた略称, イベント)]"""
    out = []
    for ev in events or []:
        for alias in ev.get("aliases") or []:
            if alias and _alias_pattern(alias).search(text or ""):
                out.append((alias, ev))
                break
    return out


def event_label(ev):
    """「正式名(会場、都道府県)」の形にする。"""
    where = [x for x in (ev.get("venue"), ev.get("prefecture")) if x]
    if ev.get("prefecture") == VARIES:
        where = [x for x in where if x != VARIES] + ["開催地は回ごとにちがう"]
    return (ev.get("name") or ev["aliases"][0]) + (f"({'、'.join(where)})" if where else "")


def event_hints(text, events):
    return [f"{alias}={event_label(ev)}" for alias, ev in match_events(text, events)]


JST = timezone(timedelta(hours=9))
WEEKDAYS = "月火水木金土日"


def posted_label(created_at):
    """投稿日時を日本時間の「2026/10/15(木) 22:08」にする(「明日」などを日付に直す基準)。読めなければ空文字。"""
    try:
        t = datetime.fromisoformat(str(created_at).replace("Z", "+00:00")).astimezone(JST)
    except ValueError:
        return ""
    return f"{t:%Y/%m/%d}({WEEKDAYS[t.weekday()]}) {t:%H:%M}"


TREE_TEXT_LIMIT = 400  # ツリーのポスト 1 件あたり、AI に渡す文字数の上限


def works_lines(works):
    """点数表の作品の行を「- 名前(別名: 略称、…)」の形で並べる。点数は AI に渡さない(点数を変えても指示文が変わらないように)。"""
    if not works:
        return ["点数を付ける作品: なし"]
    lines = ["点数を付ける作品:"]
    for names in works:
        lines.append(f"- {names[0]}" + (f"(別名: {'、'.join(names[1:])})" if len(names) > 1 else ""))
    return lines


IMAGE_BATCH = 5  # 画像のあるポストを 1 回にまとめる件数
MAX_POST_IMAGES = 4  # 本文の画像(X の 1 ポストの上限)
MAX_TREE_IMAGES = 4  # ツリーの画像(root に近い方から)
MAX_IMAGE_BYTES = 4_500_000  # Claude に渡せる 1 枚の大きさ(5MB)より少し小さく
MAX_REQUEST_BYTES = 20_000_000  # 1 回の呼び出しに載せる画像の合計(上限 32MB より小さく)
MEDIA_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}


def large_url(url):
    """pbs.twimg.com の画像は ?name=large で大きい版になる(小さい文字を読むため)。"""
    if "pbs.twimg.com/media/" in url and "?" not in url:
        return url + "?name=large"
    return url


def fetch_image(s, url):
    """画像を取ってくる。(media_type, base64 の文字列) か、取れなければ None。"""
    try:
        res = s.get(large_url(url), timeout=30)
    except requests.RequestException:
        return None
    if res.status_code != 200 or not res.content or len(res.content) > MAX_IMAGE_BYTES:
        return None
    kind = (res.headers.get("content-type") or "").split(";")[0].strip()
    if kind not in MEDIA_TYPES.values():
        kind = MEDIA_TYPES.get(url.rsplit(".", 1)[-1].split("?")[0].lower(), "image/jpeg")
    return kind, base64.b64encode(res.content).decode()


def image_list(it):
    """ポストに付ける画像を [(名前, URL)] で返す。本文の画像のあとに、ツリーの画像を root に近い方から。"""
    out = [(f"本文の画像{n}", u) for n, u in enumerate((it.get("media") or [])[:MAX_POST_IMAGES], 1)]
    tree = []
    for t in it.get("tree") or []:
        tree += [(f"ツリー・{t['relation']}の画像{n}", u) for n, u in enumerate(t.get("media") or [], 1)]
    return out + tree[:MAX_TREE_IMAGES]


def build_prompt(items, works, emojis=None, events=None, images=None):
    """指示文を作る。images({id: [(名前, media_type, base64)]})があれば、画像を挟んだ部品の配列を返す。

    部品は ("text", 文字) と ("image", 名前, media_type, base64)。images がなければ 1 つの文字列を返す(今までどおり)。
    """
    parts = []
    lines = [RULES] + works_lines(works) + [""]
    index = emoji_index(emojis)
    for it in items:
        pics = (images or {}).get(it["id"]) or []
        lines.append(f"### id: {it['id']}")
        posted = posted_label(it.get("created_at"))
        if posted:
            lines.append("投稿日時: " + posted)
        if it.get("author_location") or it.get("author_description"):
            lines.append(f"投稿者プロフィール: 地域={it.get('author_location') or ''} / 自己紹介={(it.get('author_description') or '')[:160]}")
        lines.append("本文: " + (it.get("text") or ""))
        tree = it.get("tree") or []
        if tree:  # 引用元もツリーに入っている
            lines.append("ツリー(古い順):")
            lines += [f"- [{t['relation']}・{'依頼主' if t['by_client'] else '別の人'}] {(t['text'] or '')[:TREE_TEXT_LIMIT]}"
                      for t in tree]
        elif it.get("quoted_text"):
            lines.append("引用元の本文: " + it["quoted_text"])
        body = "\n".join([it.get("text") or "", it.get("quoted_text") or ""] + [t["text"] or "" for t in tree])
        hints = emoji_hints(body, index)
        if hints:
            lines.append("絵文字の意味: " + "、".join(hints))
        hints = event_hints(body, events)
        if hints:
            lines.append("イベントの意味: " + "、".join(hints))
        if pics:
            lines.append("画像: " + "、".join(name for name, *_ in pics) + "(この下に続く)")
            parts.append(("text", "\n".join(lines)))
            lines = []
            for name, kind, data in pics:
                parts.append(("text", f"{name}(id: {it['id']}):"))
                parts.append(("image", name, kind, data))
            continue
        lines.append("")
    if not images:
        return "\n".join(lines)
    if lines:
        parts.append(("text", "\n".join(lines)))
    return parts


def cost_usd(model, prompt_tokens, output_tokens):
    price_in, price_out = PRICES.get(model, DEFAULT_PRICE)
    return prompt_tokens / 1_000_000 * price_in + output_tokens / 1_000_000 * price_out


def _post(s, url, headers, body):
    """再試行つきで POST する。200 の応答の JSON を返す。"""
    last = ""
    for attempt in range(5):
        try:
            res = s.post(url, headers=headers, json=body, timeout=120)
        except requests.RequestException as e:
            last = str(e)
            time.sleep(2 ** attempt * 3)
            continue
        if res.status_code in (429, 500, 503, 529):
            last = f"{res.status_code}"
            time.sleep(2 ** attempt * 5)
            continue
        if res.status_code != 200:
            raise JudgeError(f"AI の呼び出しが失敗しました ({res.status_code}): {res.text[:300]}")
        return res.json()
    raise JudgeError(f"AI の呼び出しが再試行後も失敗しました ({last})")


def json_schema(schema):
    """Gemini 形式のスキーマ(type が大文字)を、Claude 用の JSON Schema に直す。"""
    out = {k: v for k, v in schema.items() if k not in ("type", "properties", "items")}
    out["type"] = schema["type"].lower()
    if "properties" in schema:
        out["properties"] = {k: json_schema(v) for k, v in schema["properties"].items()}
    if "items" in schema:
        out["items"] = json_schema(schema["items"])
    return out


def _gemini_parts(prompt):
    if isinstance(prompt, str):
        return [{"text": prompt}]
    return [{"text": p[1]} if p[0] == "text" else {"inline_data": {"mime_type": p[2], "data": p[3]}} for p in prompt]


def _claude_content(prompt):
    if isinstance(prompt, str):
        return prompt
    return [{"type": "text", "text": p[1]} if p[0] == "text"
            else {"type": "image", "source": {"type": "base64", "media_type": p[2], "data": p[3]}} for p in prompt]


def _call_gemini(s, api_key, model, prompt):
    body = {
        "contents": [{"role": "user", "parts": _gemini_parts(prompt)}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": SCHEMA, "temperature": 0},
    }
    data = _post(s, GEMINI_API.format(model=model), {"x-goog-api-key": api_key}, body)
    usage = data.get("usageMetadata") or {}
    try:
        result = json.loads(data["candidates"][0]["content"]["parts"][0]["text"])
    except (KeyError, IndexError, ValueError) as e:
        raise JudgeError(f"AI の返事を読めませんでした: {str(data)[:300]}") from e
    return result, usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)


def _call_claude(s, api_key, model, prompt):
    # 決まった形で返させるため、結果を「道具」の引数として出させる。
    tool = {"name": "submit_results", "description": "読み取った結果を提出する",
            "input_schema": {"type": "object", "properties": {"results": json_schema(SCHEMA)}, "required": ["results"]}}
    body = {"model": model, "max_tokens": 16000, "tools": [tool],  # temperature はこの機種では使えない
            "tool_choice": {"type": "tool", "name": "submit_results"},
            "messages": [{"role": "user", "content": _claude_content(prompt)}]}
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    data = _post(s, CLAUDE_API, headers, body)
    usage = data.get("usage") or {}
    blocks = [b for b in data.get("content", []) if b.get("type") == "tool_use"]
    if not blocks or not isinstance(blocks[0].get("input"), dict):
        raise JudgeError(f"AI の返事を読めませんでした: {str(data)[:300]}")
    return blocks[0]["input"].get("results"), usage.get("input_tokens", 0), usage.get("output_tokens", 0)


def call(api_key, model, prompt, session=None):
    """AI を 1 回呼ぶ。prompt は文字列か、build_prompt が返す部品の配列。(結果の配列, 入力トークン, 出力トークン) を返す。"""
    s = session or requests
    fn = _call_claude if provider(model) == "claude" else _call_gemini
    result, tokens_in, tokens_out = fn(s, api_key, model, prompt)
    if not isinstance(result, list):
        raise JudgeError("AI の返事が配列ではありませんでした")
    return result, tokens_in, tokens_out


def judge(items, api_key, model, works, batch_size, session=None, emojis=None, events=None):
    """items をまとめて判定する。(結果 {id: 判定}, 入力トークン, 出力トークン, 呼び出し回数) を返す。

    画像のないポストは batch_size 件ずつ、画像のあるポストは画像を取ってきて IMAGE_BATCH 件ずつ(画像の合計が
    MAX_REQUEST_BYTES を超えるならもっと少なく)判定する。取れなかった画像は外す。
    AI が返し忘れたポストは 1 件ずつ聞き直す。それでも返らなければ結果に入れない。
    """
    s = session or requests
    results, tokens_in, tokens_out, calls = {}, 0, 0, 0

    def run(batch, images=None):
        nonlocal tokens_in, tokens_out, calls
        out, t_in, t_out = call(api_key, model, build_prompt(batch, works, emojis, events, images), s)
        tokens_in, tokens_out, calls = tokens_in + t_in, tokens_out + t_out, calls + 1
        wanted = {it["id"] for it in batch}
        for r in out:
            if isinstance(r, dict) and str(r.get("id")) in wanted:
                results[str(r["id"])] = normalize(r)

    def load(it):
        pics, size = [], 0
        for name, url in image_list(it):
            got = fetch_image(s, url)
            if got and size + len(got[1]) <= MAX_REQUEST_BYTES:
                pics.append((name, *got))
                size += len(got[1])
        return pics

    plain = [it for it in items if not image_list(it)]
    with_images = [it for it in items if image_list(it)]
    for i in range(0, len(plain), batch_size):
        run(plain[i:i + batch_size])
    batch, images, size = [], {}, 0
    for it in with_images:
        pics = load(it)
        bytes_ = sum(len(p[2]) for p in pics)
        if batch and (len(batch) >= IMAGE_BATCH or size + bytes_ > MAX_REQUEST_BYTES):
            run(batch, images)
            batch, images, size = [], {}, 0
        batch.append(it)
        images[it["id"]] = pics
        size += bytes_
    if batch:
        run(batch, images)
    for it in [it for it in items if it["id"] not in results]:
        run([it], {it["id"]: load(it)} if image_list(it) else None)
    return results, tokens_in, tokens_out, calls


def normalize(result):
    """決められた選択肢以外の値を直す(AI が「関東」などを返すことがあるため)。"""
    pref = result.get("prefecture")
    if pref not in PREFECTURES and pref != UNKNOWN_PLACE:
        if pref and not result.get("place"):
            result["place"] = pref  # 「関東」などは場所の欄に残す
        result["prefecture"], result["prefecture_source"] = UNKNOWN_PLACE, UNKNOWN_PLACE
    elif result.get("prefecture_source") == PROFILE_SOURCE:
        result["prefecture"], result["prefecture_source"] = UNKNOWN_PLACE, UNKNOWN_PLACE  # プロフィールからの推定は撮影場所にしない
    elif result.get("prefecture_source") not in PLACE_SOURCES:
        result["prefecture_source"] = "本文" if pref != UNKNOWN_PLACE else UNKNOWN_PLACE
    if result.get("prefecture_source") == EVENT_GUESS_SOURCE and result.get("event_prefecture") != result.get("prefecture"):
        result["prefecture"], result["prefecture_source"] = UNKNOWN_PLACE, UNKNOWN_PLACE  # 推定の根拠と食い違うものは使わない
    if result.get("event_prefecture") not in PREFECTURES + [VARIES]:
        result["event_prefecture"] = UNKNOWN_PLACE
    works = result.get("matched_works")
    result["matched_works"] = [str(w).strip()[:60] for w in works if str(w).strip()][:10] if isinstance(works, list) else []
    result["event"] = str(result.get("event") or "").strip()[:60]
    result["event_venue"] = str(result.get("event_venue") or "").strip()[:60]
    if result.get("case_type") not in TYPES:
        result["case_type"] = "その他"
    return result


def reject_reason(result, cosplay_only):
    """AI の読み取り結果から、対象外にする理由を返す。対象なら None。"""
    if not result.get("wants_photographer"):
        return "カメラマン募集ではない"
    if result.get("official_or_job"):
        return "公式・求人"
    if cosplay_only and not result.get("is_cosplay"):
        return "コスプレ以外"
    return None
