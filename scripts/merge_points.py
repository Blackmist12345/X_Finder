"""旧【3】減点語・【4】除外作品・【5】点数表・【6】クリティカル条件を、新しい【3】点数表の1つにまとめる。

MODE=preview(初期値): まとめ方の案を表示するだけで、何も書き込まない。
MODE=apply: 新しい【3】点数表を作って行を入れ、残りの表の番号を【4】〜【7】に振り直し、D1 の表の ID を書き換える。
  古い4表は消さずに「(旧)」を付けて残す(新しい表で動くのを確かめてから、Notion で手で消す)。

行の移し方(点数は −100〜+100 の新しい刻みに書き換える):
  - 種類・都道府県: tables.py の初期値(コスプレ撮影 +20、その他の道府県 −20 など)
  - 減点語: 都道府県の名前は外す(都道府県の行と重なるため)。地名(地方・都市名)は −20、それ以外は −5 か −10
  - 除外作品: 区分「作品」で −100(仕分けの「点数以下」で削除予定へ回す)
  - クリティカル条件の「条件」行: 区分「語」で +20(基準点は使わない)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from finder import notion as nt  # noqa: E402
from finder.config import TABLE_IDS_KEY  # noqa: E402
from finder.d1 import D1  # noqa: E402
from finder.judge import PREFECTURES  # noqa: E402
from finder.tables import POINT_SEED, TABLES  # noqa: E402

OLD_KEYS = ["penalty_words", "excluded_works", "scores", "critical"]
OLD_TABLES_KEY = "notion_tables_old"
REGION_POINTS, WORD_POINTS, LIGHT_WORD_POINTS = -20, -10, -5
EXCLUDED_WORK_POINTS = -100
CRITICAL_POINTS = 20
PREF_NAMES = set(PREFECTURES) | {p[:-1] for p in PREFECTURES if p != "北海道" and p[-1] in "都府県"}


def word_points(old, category):
    if category == "地方・都市名":
        return REGION_POINTS
    return LIGHT_WORD_POINTS if old is not None and abs(old) <= 5 else WORD_POINTS


def plan(notion, ids):
    """[(条件, 区分, 点数, カテゴリ, 備考)] と、外した語の一覧を返す。"""
    rows = [(c, k, pts, "", note) for c, k, pts, note in POINT_SEED]
    dropped = []
    if "penalty_words" in ids:
        for r in notion.query_all(ids["penalty_words"]):
            p = r["properties"]
            word, cat = nt.text(p.get("単語")).strip(), nt.text(p.get("カテゴリ")) or "その他"
            if not word or not nt.checkbox(p.get("有効")):
                continue
            if word in PREF_NAMES:
                dropped.append(word)
                continue
            rows.append((word, "語", word_points(nt.number(p.get("減点")), cat), cat, nt.text(p.get("備考"))))
    if "excluded_works" in ids:
        for r in notion.query_all(ids["excluded_works"]):
            p = r["properties"]
            name = nt.text(p.get("作品名 / 略称")).strip()
            if name and nt.checkbox(p.get("有効")):
                rows.append((name, "作品", EXCLUDED_WORK_POINTS, "", "旧【4】除外作品から"))
    if "critical" in ids:
        for r in notion.query_all(ids["critical"]):
            p = r["properties"]
            value = nt.text(p.get("値")).strip()
            if nt.text(p.get("種類")) == "条件" and value and nt.checkbox(p.get("有効")):
                rows.append((value, "語", CRITICAL_POINTS, "その他", "旧【6】クリティカル条件から"))
    return rows, dropped


def props(row):
    cond, kind, pts, cat, note = row
    out = {"条件": nt.p_title(cond), "区分": nt.p_select(kind), "点数": nt.p_number(pts), "有効": nt.p_check(True), "備考": nt.p_text(note)}
    if cat:
        out["カテゴリ"] = nt.p_select(cat)
    return out


def main():
    mode = os.environ.get("MODE", "preview")
    d1 = D1.from_env()
    ids = dict(d1.get_meta(TABLE_IDS_KEY) or {})
    if "points" in ids:
        print("【3】点数表はもうあります。何もしません。")
        return
    notion = nt.Notion(os.environ.get("NOTION_API_KEY"))
    rows, dropped = plan(notion, ids)
    by_kind = {}
    for r in rows:
        by_kind.setdefault(r[1], []).append(r)
    lines = ["## 点数表のまとめ方" + ("" if mode == "apply" else "(案。まだ何も書き込んでいません)"), ""]
    for kind, rs in by_kind.items():
        lines.append(f"### {kind}({len(rs)}行)")
        lines += [f"- {c} {p:+g}" + (f"(カテゴリ {cat})" if cat else "") for c, _, p, cat, _ in rs]
        lines.append("")
    if dropped:
        lines += [f"### 減点語から外す都道府県の名前({len(dropped)}語、都道府県の行と重なるため)", "", "、".join(dropped), ""]
    titles = {t["key"]: t["title"] for t in TABLES}
    lines += ["### 表の名前の振り直し", ""] + [f"- {titles[k]}" for k in titles if k in ids or k == "points"]
    print("\n".join(lines))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    if mode != "apply":
        return

    parent = notion_parent()
    table = next(t for t in TABLES if t["key"] == "points")
    new_id = notion.create_database(parent, table["title"], table["props"])
    for r in rows:
        notion.create_page(new_id, props(r))
    old = {k: ids.pop(k) for k in OLD_KEYS if k in ids}
    ids["points"] = new_id
    d1.set_meta(OLD_TABLES_KEY, old)
    d1.set_meta(TABLE_IDS_KEY, ids)
    for key, db_id in ids.items():
        if key in titles and key != "points":
            notion.rename_database(db_id, titles[key])
    old_titles = {"penalty_words": "【3】減点語", "excluded_works": "【4】除外作品", "scores": "【5】点数表", "critical": "【6】クリティカル条件"}
    for key, db_id in old.items():
        notion.rename_database(db_id, f"(旧){old_titles[key]}")
    print(f"\n新しい【3】点数表を作り、{len(rows)}行を入れました。古い表は「(旧)」を付けて残しています。")


def notion_parent():
    from scripts.setup_notion import page_id
    return page_id(os.environ.get("NOTION_PARENT_PAGE_ID"))


if __name__ == "__main__":
    main()
