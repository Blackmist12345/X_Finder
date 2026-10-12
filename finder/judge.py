"""N4 AI 判定: ポストの文字だけを、まとめて AI(Claude または Gemini)に読ませる。

画像は読まない(費用を抑えるため。画像は点数の高い案件だけ、参加者情報 N7 で読む)。
AI には事実の読み取りだけを頼み、対象外にするかどうかは reject_reason() がルールで決める。
"""
import json
import time

import requests

from finder.tables import TYPES

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
            "prefecture_source": {"type": "STRING", "enum": ["本文", "プロフィール", UNKNOWN_PLACE]},
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
- excluded_work: works が下の「除外作品」のどれかに当たれば、その除外作品名(略称・キャラクター名からの判断も含む)。当たらなければ空文字。
- prefecture: 撮影する場所の都道府県。本文から分からなければ、投稿者プロフィールの活動地域から分かる場合だけそれを使う。どちらでも分からなければ「不明」。
- prefecture_source: prefecture をどこから判断したか(本文 / プロフィール / 不明)。
- place: 会場名や地域(本文に書かれていれば。なければ空文字)。
- dates: 撮影日や候補日(本文の書き方のまま短く。なければ空文字)。
- summary: 何の撮影の募集か、30文字以内の日本語。
"""


def build_prompt(items, excluded_works):
    lines = [RULES, "除外作品: " + ("、".join(excluded_works) if excluded_works else "なし"), ""]
    for it in items:
        lines.append(f"### id: {it['id']}")
        if it.get("author_location") or it.get("author_description"):
            lines.append(f"投稿者プロフィール: 地域={it.get('author_location') or ''} / 自己紹介={(it.get('author_description') or '')[:160]}")
        lines.append("本文: " + (it.get("text") or ""))
        if it.get("quoted_text"):
            lines.append("引用元の本文: " + it["quoted_text"])
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
    body = {"model": model, "max_tokens": 16000, "temperature": 0, "tools": [tool],
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


def judge(items, api_key, model, excluded_works, batch_size, session=None):
    """items をまとめて判定する。(結果 {id: 判定}, 入力トークン, 出力トークン, 呼び出し回数) を返す。

    AI が返し忘れたポストは 1 件ずつ聞き直す。それでも返らなければ結果に入れない。
    """
    results, tokens_in, tokens_out, calls = {}, 0, 0, 0

    def run(batch):
        nonlocal tokens_in, tokens_out, calls
        out, t_in, t_out = call(api_key, model, build_prompt(batch, excluded_works), session)
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
