"""N10 採点: AI の読み取り結果と Notion の点数表から、案件の点数とクリティカルを決める。

都道府県が分かった案件は、地名の減点語を使わない(同じ場所で二重に減点しないため)。
都道府県が分からない案件だけ、本文に出てきた地名の減点語で代わりに減点する。
そのあと、依頼主の FF率(フォロワー数÷フォロー数)を倍率として掛け(プラスの点のときだけ)、
フォロワー数の 1/N を足す。倍率と加点の幅は【7】基本設定で決める。
"""
from finder.judge import UNKNOWN_PLACE

REGION_CATEGORY = "地方・都市名"
OTHER_PREFECTURES = "その他の道府県"
UNKNOWN_ROW = "場所不明"


def _strip(pref):
    return pref[:-1] if pref and pref[-1] in "都府県" and pref != "北海道" else pref


def _find(rows, kind, name):
    for r in rows:
        if r["kind"] == kind and (r["condition"] == name or _strip(r["condition"]) == _strip(name)):
            return r
    return None


def _clamp(x, low, high):
    if low:
        x = max(x, low)
    if high:
        x = min(x, high)
    return x


def _follower_points(author, basic, base, detail):
    """FF率の倍率とフォロワー数の加点で増える点(内訳にも足す)。フォロワー数が分からなければ 0。"""
    followers = (author or {}).get("followers")
    if followers is None:
        return 0
    following = max((author or {}).get("following") or 0, 1)  # フォロー 0 人は 1 人として割る
    added = 0
    ff = followers / following
    rate = _clamp(ff, basic.get("FF率の倍率の下限", 0.5), basic.get("FF率の倍率の上限", 1.5))
    if base > 0 and rate != 1:
        pts = round(base * rate - base)
        detail.append({"label": f"FF率 {ff:.1f}(×{rate:g})", "points": pts})
        added += pts
    div = basic.get("フォロワー加点の割合(1/何)", 20)
    if div > 0:
        pts = round(_clamp(followers / div, 0, basic.get("フォロワー加点の上限", 30)))
        if pts:
            detail.append({"label": f"フォロワー {followers:,}人(÷{div})", "points": pts})
            added += pts
    return added


def score(result, penalties, text, config, author=None):
    """(点数, 内訳 [{label, points}], クリティカルか, クリティカルの理由) を返す。author は {followers, following}。"""
    rows = config["scores"]
    detail = []

    case_type = result.get("case_type") or "その他"
    row = _find(rows, "種類", case_type)
    if row:
        detail.append({"label": f"種類: {case_type}", "points": row["points"]})

    pref = result.get("prefecture") or UNKNOWN_PLACE
    known = pref != UNKNOWN_PLACE
    if known:
        row = _find(rows, "都道府県", pref) or _find(rows, "都道府県", OTHER_PREFECTURES)
        if row:
            label = pref if row["condition"] != OTHER_PREFECTURES else f"{pref}({OTHER_PREFECTURES})"
            detail.append({"label": f"場所: {label}", "points": row["points"]})
    else:
        row = _find(rows, "都道府県", UNKNOWN_ROW)
        if row:
            detail.append({"label": f"場所: {UNKNOWN_ROW}", "points": row["points"]})

    for p in penalties or []:
        if known and p.get("category") == REGION_CATEGORY:
            continue
        detail.append({"label": f"減点語: {p['word']}", "points": p["points"]})

    total = sum(d["points"] for d in detail)
    total += _follower_points(author, config.get("basic") or {}, total, detail)

    critical = config["critical"]
    reasons = []
    if total >= critical["threshold"]:
        reasons.append(f"{critical['threshold']:g}点以上")
    haystack = " ".join([text or "", result.get("works") or "", result.get("place") or ""])
    for cond in critical["conditions"]:
        if cond["value"] and cond["value"] in haystack:
            reasons.append(f"条件: {cond['name'] or cond['value']}")
    return total, detail, bool(reasons), "、".join(reasons)
