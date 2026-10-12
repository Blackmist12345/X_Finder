"""N10 採点: AI の読み取り結果と Notion の点数表から、案件の点数とクリティカルを決める。

都道府県が分かった案件は、地名の減点語を使わない(同じ場所で二重に減点しないため)。
都道府県が分からない案件だけ、本文に出てきた地名の減点語で代わりに減点する。
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


def score(result, penalties, text, config):
    """(点数, 内訳 [{label, points}], クリティカルか, クリティカルの理由) を返す。"""
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

    critical = config["critical"]
    reasons = []
    if total >= critical["threshold"]:
        reasons.append(f"{critical['threshold']:g}点以上")
    haystack = " ".join([text or "", result.get("works") or "", result.get("place") or ""])
    for cond in critical["conditions"]:
        if cond["value"] and cond["value"] in haystack:
            reasons.append(f"条件: {cond['name'] or cond['value']}")
    return total, detail, bool(reasons), "、".join(reasons)
