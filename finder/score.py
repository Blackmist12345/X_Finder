"""N10 採点: AI の読み取り結果と Notion の【3】点数表から、案件の点数を決める。

点数表の区分ごとに当てはめ、当たった行の点を足す。
- 種類・都道府県: AI が読んだ案件の種類と撮影場所(1行ずつ)
- 語: 収集のときに本文の文字で当てた語(当たった語すべて)
- 作品: AI が返した、点数表の作品の名前(当たった作品すべて、同じ作品は1回)
- イベント: AI が読んだイベント名を【7】イベントの一覧で引き、略称か正式名が点数表の条件と同じ行(1行)

撮影場所が分からない案件は、本文に出てくる都道府県名で代わりに都道府県の行を当てる(最初に出てきたもの)。
都道府県が分かった案件は、地名の語(カテゴリ「地方・都市名」)を使わない(同じ場所で二重に数えないため)。
そのあと、依頼主の FF率(フォロワー数÷フォロー数)を倍率として掛ける(プラスの点のときだけ)。
倍率の幅は【4】基本設定で決める(初期値 0.5〜1.5 倍)。
"""
import unicodedata

from finder.judge import PREFECTURES, UNKNOWN_PLACE, match_events
from finder.tables import OTHER_PREFECTURES, UNKNOWN_PLACE_ROW

REGION_CATEGORY = "地方・都市名"
TEXT_PLACE = "本文の地名"


def _strip(pref):
    return pref[:-1] if pref and pref[-1] in "都府県" and pref != "北海道" else pref


def _key(name):
    return unicodedata.normalize("NFKC", name or "").lower().replace(" ", "")


def _rows(config, kind):
    return [r for r in config.get("points") or [] if r["kind"] == kind]


def _find(rows, name):
    for r in rows:
        if r["condition"] == name or _strip(r["condition"]) == _strip(name):
            return r
    return None


def _clamp(x, low, high):
    if low:
        x = max(x, low)
    if high:
        x = min(x, high)
    return x


def _ff_points(author, basic, base, detail):
    """FF率の倍率で増える(減る)点を返し、内訳に足す。フォロワー数が分からないか、点がプラスでなければ 0。"""
    followers = (author or {}).get("followers")
    if followers is None or base <= 0:
        return 0
    following = max((author or {}).get("following") or 0, 1)  # フォロー 0 人は 1 人として割る
    ff = followers / following
    rate = _clamp(ff, basic.get("FF率の倍率の下限", 0.5), basic.get("FF率の倍率の上限", 1.5))
    if rate == 1:
        return 0
    pts = round(base * rate - base)
    detail.append({"label": f"FF率 {ff:.1f}(×{rate:g})", "points": pts})
    return pts


def text_prefecture(text):
    """本文に最初に出てくる都道府県(「大阪」のように都府県を省いた書き方も含む)。なければ None。"""
    masked = (text or "").replace("東京都", "〓〓〓")  # 「東京都」の中の「京都」を数えない
    best = None
    for pref in PREFECTURES:
        names = [pref, "〓〓〓" if pref == "東京都" else _strip(pref)]
        found = [masked.find(n) for n in names if n and masked.find(n) >= 0]
        if found and (best is None or min(found) < best[0]):
            best = (min(found), pref)
    return best[1] if best else None


def _place(result, text, rows, detail):
    """都道府県の行を当てる。都道府県が分かったか(本文の地名を含む)を返す。"""
    pref = result.get("prefecture") or UNKNOWN_PLACE
    label_head = "場所"
    if pref == UNKNOWN_PLACE:
        pref = text_prefecture(text)
        label_head = f"場所({TEXT_PLACE})"
    if not pref:
        row = _find(rows, UNKNOWN_PLACE_ROW)
        if row:
            detail.append({"label": f"場所: {UNKNOWN_PLACE_ROW}", "points": row["points"]})
        return False
    row = _find(rows, pref) or _find(rows, OTHER_PREFECTURES)
    if row:
        label = pref if row["condition"] != OTHER_PREFECTURES else f"{pref}({OTHER_PREFECTURES})"
        detail.append({"label": f"{label_head}: {label}", "points": row["points"]})
    return True


def _works(result, rows, detail):
    """AI が返した作品名を点数表の作品の行に当てる。同じ行は1回だけ。"""
    seen = set()
    for name in result.get("matched_works") or []:
        for r in rows:
            if id(r) not in seen and _key(name) in {_key(n) for n in r["names"]}:
                seen.add(id(r))
                detail.append({"label": f"作品: {r['condition']}", "points": r["points"]})
                break


def _event(result, rows, events, detail):
    """AI が読んだイベント名をイベントの一覧で引き、略称か正式名が点数表の条件と同じ行を1つ当てる。"""
    name = result.get("event") or ""
    if not name or not rows:
        return
    names = {_key(name)}
    for _, ev in match_events(name, events):
        names |= {_key(a) for a in ev.get("aliases") or []}
        if ev.get("name"):
            names.add(_key(ev["name"]))
    for r in rows:
        if names & {_key(n) for n in r["names"]}:
            detail.append({"label": f"イベント: {r['condition']}", "points": r["points"]})
            return


def score(result, words, text, config, author=None):
    """(点数, 内訳 [{label, points}]) を返す。words は収集のときに本文で当たった語、author は {followers, following}。"""
    detail = []

    case_type = result.get("case_type") or "その他"
    row = _find(_rows(config, "種類"), case_type)
    if row:
        detail.append({"label": f"種類: {case_type}", "points": row["points"]})

    known = _place(result, text, _rows(config, "都道府県"), detail)

    for w in words or []:
        if known and w.get("category") == REGION_CATEGORY:
            continue
        detail.append({"label": f"語: {w['word']}", "points": w["points"]})

    _works(result, _rows(config, "作品"), detail)
    _event(result, _rows(config, "イベント"), config.get("events"), detail)

    total = sum(d["points"] for d in detail)
    total += _ff_points(author, config.get("basic") or {}, total, detail)
    return total, detail
