"""N4 AI 判定: ポストの文字だけを、まとめて AI(Claude または Gemini)に読ませる。

画像は読まない(費用を抑えるため。画像は対象になった案件だけ、あとで image_step.py が読む)。
AI には事実の読み取りだけを頼み、対象外にするかどうかは reject_reason() がルールで決める。
"""
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
IMAGE_SOURCE = "画像"  # 判定のあとに image_step.py が画像から読んだとき
PROFILE_SOURCE = "プロフィール"  # 前の判定の分だけ。撮影場所はプロフィールから決めない(活動地域は画面で別に読む)
PLACE_SOURCES = ["本文", EVENT_SOURCE, UNKNOWN_PLACE]

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
            "excluded_work": {"type": "STRING"},
            "prefecture": {"type": "STRING", "enum": PREFECTURES + [UNKNOWN_PLACE]},
            "prefecture_source": {"type": "STRING", "enum": PLACE_SOURCES},
            "place": {"type": "STRING"},
            "dates": {"type": "STRING"},
            "summary": {"type": "STRING"},
        },
        "required": ["id", "wants_photographer", "official_or_job", "case_type", "is_cosplay", "works",
                     "excluded_work", "prefecture", "prefecture_source", "place", "dates", "summary"],
    },
}

RULES = """あなたは、カメラマンを探している X(旧Twitter)のポストを見分ける係です。
下のポストそれぞれについて、書かれている事実を読み取り、指定の JSON 配列で返してください。
ポストの数だけ、id をそのまま付けて返してください。

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
- excluded_work: works が下の「除外作品」のどれかに当たれば、その除外作品名(略称・キャラクター名からの判断も含む)。当たらなければ空文字。
- prefecture: 撮影する場所(スタジオ・会場・イベントの開催地)の都道府県。次の順で判断する。
  1. 本文・引用元の本文に書かれた地名・会場名・駅名
  2. ポストに「イベントの意味」が付いていれば、そこに書かれた開催地(「回ごとにちがう」なら本文から判断する)
  どちらでも分からなければ「不明」。投稿者プロフィールの地域・自己紹介は依頼主の活動地域で、撮影場所とは別なので使わない。
  全国のポストが混ざっているので、推測で東京などにしないこと。
- prefecture_source: prefecture をどこから判断したか(本文 / イベント表 / 不明)。
- place: 会場名やイベント名・地域(本文に書かれていれば。イベント表から分かった会場でもよい。なければ空文字)。
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


def build_prompt(items, excluded_works, emojis=None, events=None):
    lines = [RULES, "除外作品: " + ("、".join(excluded_works) if excluded_works else "なし"), ""]
    index = emoji_index(emojis)
    for it in items:
        lines.append(f"### id: {it['id']}")
        posted = posted_label(it.get("created_at"))
        if posted:
            lines.append("投稿日時: " + posted)
        if it.get("author_location") or it.get("author_description"):
            lines.append(f"投稿者プロフィール: 地域={it.get('author_location') or ''} / 自己紹介={(it.get('author_description') or '')[:160]}")
        lines.append("本文: " + (it.get("text") or ""))
        if it.get("quoted_text"):
            lines.append("引用元の本文: " + it["quoted_text"])
        body = (it.get("text") or "") + "\n" + (it.get("quoted_text") or "")
        hints = emoji_hints(body, index)
        if hints:
            lines.append("絵文字の意味: " + "、".join(hints))
        hints = event_hints(body, events)
        if hints:
            lines.append("イベントの意味: " + "、".join(hints))
        lines.append("")
    return "\n".join(lines)


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


def _call_gemini(s, api_key, model, prompt):
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
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
            "messages": [{"role": "user", "content": prompt}]}
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    data = _post(s, CLAUDE_API, headers, body)
    usage = data.get("usage") or {}
    blocks = [b for b in data.get("content", []) if b.get("type") == "tool_use"]
    if not blocks or not isinstance(blocks[0].get("input"), dict):
        raise JudgeError(f"AI の返事を読めませんでした: {str(data)[:300]}")
    return blocks[0]["input"].get("results"), usage.get("input_tokens", 0), usage.get("output_tokens", 0)


def call(api_key, model, prompt, session=None):
    """AI を 1 回呼ぶ。(結果の配列, 入力トークン, 出力トークン) を返す。"""
    s = session or requests
    fn = _call_claude if provider(model) == "claude" else _call_gemini
    result, tokens_in, tokens_out = fn(s, api_key, model, prompt)
    if not isinstance(result, list):
        raise JudgeError("AI の返事が配列ではありませんでした")
    return result, tokens_in, tokens_out


def judge(items, api_key, model, excluded_works, batch_size, session=None, emojis=None, events=None):
    """items をまとめて判定する。(結果 {id: 判定}, 入力トークン, 出力トークン, 呼び出し回数) を返す。

    AI が返し忘れたポストは 1 件ずつ聞き直す。それでも返らなければ結果に入れない。
    """
    results, tokens_in, tokens_out, calls = {}, 0, 0, 0

    def run(batch):
        nonlocal tokens_in, tokens_out, calls
        out, t_in, t_out = call(api_key, model, build_prompt(batch, excluded_works, emojis, events), session)
        tokens_in, tokens_out, calls = tokens_in + t_in, tokens_out + t_out, calls + 1
        wanted = {it["id"] for it in batch}
        for r in out:
            if isinstance(r, dict) and str(r.get("id")) in wanted:
                results[str(r["id"])] = normalize(r)

    for i in range(0, len(items), batch_size):
        run(items[i:i + batch_size])
    for it in [it for it in items if it["id"] not in results]:
        run([it])
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
    if result.get("case_type") not in TYPES:
        result["case_type"] = "その他"
    return result


def reject_reason(result, cosplay_only):
    """AI の読み取り結果から、対象外にする理由を返す。対象なら None。"""
    if not result.get("wants_photographer"):
        return "カメラマン募集ではない"
    if result.get("official_or_job"):
        return "公式・求人"
    if result.get("excluded_work"):
        return f"除外作品: {result['excluded_work']}"
    if cosplay_only and not result.get("is_cosplay"):
        return "コスプレ以外"
    return None
