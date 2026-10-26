"""N1 設定読込: Notion の設定の表(【1】〜【7】)から設定を読む。

読めなかった表や、値が不正な項目は、前回正しく読めた値(D1 の meta に保存)を使い、
warnings に理由を残す。
"""
import re

from finder import notion as nt
from finder.tables import BASIC, PERIODS, POINT_KINDS, POINT_MAX, POINT_MIN, TABLES

POINT_KIND_NAMES = [n for n, _ in POINT_KINDS]

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


def split_names(value):
    """「ブルーアーカイブ, ブルアカ」のようにカンマで並べた名前を分ける。"""
    return [a.strip() for a in re.split(r"[,、，]", value or "") if a.strip()]


def parse_points(rows, warnings):
    """【3】点数表。[{condition, names, kind, points, category}]。点数は −100〜+100 にそろえる。"""
    out = []
    for r in rows:
        p = r["properties"]
        cond = nt.text(p.get("条件")).strip()
        if not _enabled(r) or not cond:
            continue
        kind = nt.text(p.get("区分"))
        if kind not in POINT_KIND_NAMES:
            warnings.append(f"点数表「{cond}」の区分「{kind}」が分からないので使いませんでした")
            continue
        points = nt.number(p.get("点数"))
        if points is None:
            warnings.append(f"点数表「{cond}」の点数が空欄なので 0 として扱いました")
            points = 0
        if not POINT_MIN <= points <= POINT_MAX:
            fixed = max(POINT_MIN, min(POINT_MAX, points))
            warnings.append(f"点数表「{cond}」の点数 {points:g} が {POINT_MIN}〜+{POINT_MAX} の外なので {fixed:g} にしました")
            points = fixed
        names = split_names(cond) if kind in ("作品", "イベント") else [cond]
        out.append({"condition": names[0], "names": names, "kind": kind, "points": float(points),
                    "category": nt.text(p.get("カテゴリ")) or "その他"})
    return out


def word_rows(points):
    """区分「語」の行を、収集のときの文字照合(N3)で使う形にする。"""
    return [{"word": r["condition"], "category": r["category"], "points": r["points"]} for r in points if r["kind"] == "語"]


def parse_emojis(rows):
    out = []
    for r in rows:
        p = r["properties"]
        emoji, name = nt.text(p.get("絵文字")).strip(), nt.text(p.get("キャラ名")).strip()
        if _enabled(r) and emoji and name:
            out.append({"emoji": emoji, "name": name, "group": nt.text(p.get("グループ")).strip()})
    return out


def parse_events(rows):
    out = []
    for r in rows:
        p = r["properties"]
        aliases = [a.strip() for a in re.split(r"[,、，]", nt.text(p.get("略称"))) if a.strip()]
        if not _enabled(r) or not aliases:
            continue
        out.append({"aliases": aliases, "name": nt.text(p.get("正式名")).strip(), "venue": nt.text(p.get("会場")).strip(),
                    "prefecture": nt.text(p.get("都道府県")).strip()})
    return out


def _check(value, kind):
    if kind == "int":
        return int(str(value).replace(",", "").strip())
    if kind == "float":
        return float(str(value).replace(",", "").strip())
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
    if "points" not in ids:
        raise ConfigError("【3】点数表がまだありません。先に setup ワークフローの points_merge_apply を実行してください")
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
    load("points", lambda rows: parse_points(rows, warnings))
    load("emojis", parse_emojis)
    load("events", parse_events)
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
    config["words"] = word_rows(config["points"])
    return config, warnings
