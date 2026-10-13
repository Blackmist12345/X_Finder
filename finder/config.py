"""N1 設定読込: Notion の設定の表(8表+絵文字の一覧)から設定を読む。

読めなかった表や、値が不正な項目は、前回正しく読めた値(D1 の meta に保存)を使い、
warnings に理由を残す。
"""
from finder import notion as nt
from finder.tables import BASIC, PERIODS, TABLES

LAST_GOOD_KEY = "config_last_good"
TABLE_IDS_KEY = "notion_tables"


class ConfigError(Exception):
    pass


def _enabled(row):
    return nt.checkbox(row["properties"].get("有効"))


def parse_keywords(rows):
    a, b = [], []
    for r in rows:
        p = r["properties"]
        if not _enabled(r):
            continue
        word, group = nt.text(p.get("単語")), nt.text(p.get("グループ"))
        if not word:
            continue
        if group == "撮影者(A)":
            a.append(word)
        elif group == "募集語(B)":
            b.append(word)
    if not a or not b:
        raise ConfigError("撮影者(A) と 募集語(B) が1語以上ずつ必要です")
    return {"group_a": a, "group_b": b}


def parse_search_excludes(rows):
    out = []
    for r in rows:
        p = r["properties"]
        word = nt.text(p.get("単語"))
        if not _enabled(r) or not word:
            continue
        score = nt.number(p.get("観測スコア(件/日)"))
        out.append({"page_id": r["id"], "word": word, "category": nt.text(p.get("カテゴリ")) or "その他", "score": float(score or 0)})
    return out


def parse_penalty_words(rows, warnings):
    out = []
    for r in rows:
        p = r["properties"]
        word = nt.text(p.get("単語"))
        if not _enabled(r) or not word:
            continue
        points = nt.number(p.get("減点"))
        if points is None:
            warnings.append(f"減点語「{word}」の減点が空欄なので −10 として扱いました")
            points = -10
        out.append({"word": word, "category": nt.text(p.get("カテゴリ")) or "その他", "points": -abs(float(points))})
    return out


def parse_excluded_works(rows):
    return [nt.text(r["properties"].get("作品名 / 略称")) for r in rows if _enabled(r) and nt.text(r["properties"].get("作品名 / 略称"))]


def parse_scores(rows, warnings):
    out = []
    for r in rows:
        p = r["properties"]
        cond = nt.text(p.get("条件"))
        if not _enabled(r) or not cond:
            continue
        points = nt.number(p.get("点数"))
        if points is None:
            warnings.append(f"点数表「{cond}」の点数が空欄なので 0 として扱いました")
            points = 0
        out.append({"condition": cond, "kind": nt.text(p.get("区分")) or "その他", "points": float(points)})
    return out


def parse_critical(rows):
    threshold, conditions = None, []
    for r in rows:
        p = r["properties"]
        if not _enabled(r):
            continue
        kind, value = nt.text(p.get("種類")), nt.text(p.get("値"))
        if kind == "基準点":
            try:
                threshold = float(value)
            except ValueError as e:
                raise ConfigError(f"クリティカルの基準点「{value}」が数字ではありません") from e
        elif kind == "条件" and value:
            conditions.append({"name": nt.text(p.get("名前")), "value": value})
    if threshold is None:
        raise ConfigError("クリティカルの基準点がありません")
    return {"threshold": threshold, "conditions": conditions}


def parse_emojis(rows):
    out = []
    for r in rows:
        p = r["properties"]
        emoji, name = nt.text(p.get("絵文字")).strip(), nt.text(p.get("キャラ名")).strip()
        if _enabled(r) and emoji and name:
            out.append({"emoji": emoji, "name": name, "group": nt.text(p.get("グループ")).strip()})
    return out


def _check(value, kind):
    if kind == "int":
        return int(str(value).replace(",", "").strip())
    if kind.startswith("choice:"):
        options = kind.split(":", 1)[1].split("/")
        if value not in options:
            raise ValueError(f"{'/'.join(options)} のどれかを入れてください")
        return value
    if not value:
        raise ValueError("空欄です")
    return value


def parse_key_values(rows, spec, last_good, warnings, table_title, missing=None):
    """1つの値だけの設定。不正な値は前回の値(なければ初期値)を使う。

    表に行がない項目は missing に入れる(あとで Notion に行を足すため)。
    """
    found = {nt.text(r["properties"].get("項目")): nt.text(r["properties"].get("値")) for r in rows}
    out = {}
    for key, default, kind, _ in spec:
        fallback = (last_good or {}).get(key, _check(default, kind))
        if key not in found:
            if missing is not None:
                missing.append(key)
            out[key] = fallback
            continue
        try:
            out[key] = _check(found[key], kind)
        except ValueError as e:
            warnings.append(f"{table_title}の「{key}」の値「{found[key]}」が不正です({e})。前回の値 {fallback} を使いました")
            out[key] = fallback
    return out


def load_config(notion, d1):
    """設定を読み、(config, warnings) を返す。"""
    ids = d1.get_meta(TABLE_IDS_KEY)
    if not ids:
        raise ConfigError("Notion の表の ID が見つかりません。先に setup ワークフローの notion_tables を実行してください")
    last_good = d1.get_meta(LAST_GOOD_KEY, {}) or {}
    warnings, config = [], {}
    titles = {t["key"]: t["title"] for t in TABLES}

    def load(key, parse):
        if key not in ids and next(t for t in TABLES if t["key"] == key).get("optional"):
            config[key] = []  # まだ作っていない表(あとから足した表)
            return
        try:
            rows = notion.query_all(ids[key])
            config[key] = parse(rows)
        except Exception as e:  # 表ごとに前回の値へ逃がす
            if key in last_good:
                warnings.append(f"{titles[key]}を読めなかったので前回の内容を使いました: {e}")
                config[key] = last_good[key]
            else:
                raise ConfigError(f"{titles[key]}を読めず、前回の内容もありません: {e}") from e

    load("keywords", parse_keywords)
    load("search_excludes", parse_search_excludes)
    load("penalty_words", lambda rows: parse_penalty_words(rows, warnings))
    load("excluded_works", parse_excluded_works)
    load("scores", lambda rows: parse_scores(rows, warnings))
    load("critical", parse_critical)
    load("emojis", parse_emojis)
    missing = {"basic": [], "periods": []}
    load("basic", lambda rows: parse_key_values(rows, BASIC, last_good.get("basic"), warnings, titles["basic"], missing["basic"]))
    load("periods", lambda rows: parse_key_values(rows, PERIODS, last_good.get("periods"), warnings, titles["periods"], missing["periods"]))
    for key, spec in (("basic", BASIC), ("periods", PERIODS)):
        for item, default, _, desc in spec:
            if item not in missing[key]:
                continue
            try:  # あとから増えた項目は、初期値の行を Notion に足す
                notion.create_page(ids[key], {"項目": nt.p_title(item), "値": nt.p_text(default), "説明": nt.p_text(desc)})
                warnings.append(f"{titles[key]}に「{item}」の行を足しました(初期値 {default})")
            except Exception as e:
                warnings.append(f"{titles[key]}に「{item}」の行がないので {config[key][item]} を使いました(行を足せませんでした: {e})")

    d1.set_meta(LAST_GOOD_KEY, config)
    return config, warnings
