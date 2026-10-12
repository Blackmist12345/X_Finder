"""今の Notion 4表の内容を、新しい 8表へ移す。

MODE=preview(初期値): 振り分け案を表示するだけで、何も書き込まない。
MODE=apply: 振り分け案のとおりに新しい表へ書き込む(1回だけ。やり直すときは FORCE=1)。

今の表2(除外語)は次のルールで振り分ける:
  - カテゴリが「地方・都市名」の語と、1文字の語 → 【3】減点語(取り除かずに点数を下げる)
  - それ以外 → 【2】検索除外語(検索の時点で取り除く)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from finder import notion as nt  # noqa: E402
from finder.config import TABLE_IDS_KEY  # noqa: E402
from finder.d1 import D1  # noqa: E402

MIGRATED_KEY = "migrated_from_old"
# 関東の近くの県は減点を小さめにする(仮の値。Notion で変えられる)。
NEAR = {"茨城", "栃木", "群馬", "山梨", "静岡"}
NEAR_POINTS, FAR_POINTS, ONE_CHAR_POINTS = -15, -30, -10


def is_penalty(word, category):
    return category == "地方・都市名" or len(word) == 1


def penalty_points(word, category):
    if category != "地方・都市名":
        return ONE_CHAR_POINTS
    return NEAR_POINTS if word in NEAR else FAR_POINTS


def read_old(notion):
    env = {k: os.environ.get(k) for k in ("NOTION_KEYWORDS_DB_ID", "NOTION_EXCLUDES_DB_ID", "NOTION_GENRES_DB_ID", "NOTION_CONFIG_DB_ID")}
    missing = [k for k, v in env.items() if not v]
    if missing:
        raise SystemExit(f"今の表の ID が設定されていません: {', '.join(missing)}")

    keywords = []
    for r in notion.query_all(env["NOTION_KEYWORDS_DB_ID"]):
        p = r["properties"]
        if nt.text(p.get("単語")):
            keywords.append((nt.text(p.get("単語")), nt.text(p.get("グループ")), nt.checkbox(p.get("有効")), nt.text(p.get("備考"))))

    excludes = []
    for r in notion.query_all(env["NOTION_EXCLUDES_DB_ID"]):
        p = r["properties"]
        word = nt.text(p.get("単語"))
        if word:
            excludes.append({"word": word, "category": nt.text(p.get("カテゴリ")) or "その他", "enabled": nt.checkbox(p.get("有効")),
                             "score": nt.number(p.get("観測スコア(件/日)")) or 0, "note": nt.text(p.get("備考"))})

    works = []
    for r in notion.query_all(env["NOTION_GENRES_DB_ID"]):
        p = r["properties"]
        name = nt.text(p.get("作品名 / 略称"))
        if name:
            works.append((name, nt.checkbox(p.get("有効")), nt.text(p.get("備考"))))

    configs = {}
    for r in notion.query_all(env["NOTION_CONFIG_DB_ID"]):
        p = r["properties"]
        if nt.checkbox(p.get("有効")):
            configs[nt.text(p.get("設定項目"))] = nt.text(p.get("設定値"))
    return keywords, excludes, works, configs


def summary(lines):
    text = "\n".join(lines) + "\n"
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)


def main():
    mode = os.environ.get("MODE", "preview")
    notion = nt.Notion(os.environ.get("NOTION_API_KEY"))
    keywords, excludes, works, configs = read_old(notion)

    search = [e for e in excludes if not is_penalty(e["word"], e["category"])]
    penalty = [e for e in excludes if is_penalty(e["word"], e["category"])]
    carry = {k: v for k, v in configs.items() if k in ("最小フォロワー数", "AIモデル")}

    lines = ["## 今の表から新しい表への振り分け" + ("(案)" if mode != "apply" else ""), "",
             f"- 【1】検索キーワード: {len(keywords)}語(今の表1をそのまま)",
             f"- 【4】除外作品: {len(works)}作品(今の表3をそのまま)",
             f"- 【7】基本設定に引き継ぐ値: " + ("、".join(f"{k}={v}" for k, v in carry.items()) or "なし"),
             "", f"### 【2】検索除外語へ({len(search)}語、検索の時点で取り除く)", ""]
    by_cat = {}
    for e in search:
        by_cat.setdefault(e["category"], []).append(e["word"] + ("" if e["enabled"] else "(無効)"))
    lines += [f"- {cat}: {'、'.join(words)}" for cat, words in by_cat.items()]
    lines += ["", f"### 【3】減点語へ({len(penalty)}語、取り除かずに点数を下げる)", ""]
    for points in sorted({penalty_points(e["word"], e["category"]) for e in penalty}):
        words = [e["word"] for e in penalty if penalty_points(e["word"], e["category"]) == points]
        lines.append(f"- {int(points)}点: {'、'.join(words)}")

    if mode != "apply":
        lines += ["", "これは案です。まだ何も書き込んでいません。"]
        summary(lines)
        return

    d1 = D1.from_env()
    ids = d1.get_meta(TABLE_IDS_KEY)
    if not ids:
        raise SystemExit("新しい 8表がまだありません。先に notion_tables を実行してください")
    if d1.get_meta(MIGRATED_KEY) and os.environ.get("FORCE") != "1":
        raise SystemExit("移行は実行済みです。二重に書き込まないよう止めました(やり直す場合は FORCE=1)")

    for word, group, enabled, note in keywords:
        notion.create_page(ids["keywords"], {"単語": nt.p_title(word), "グループ": nt.p_select(group), "有効": nt.p_check(enabled), "備考": nt.p_text(note)})
    for e in search:
        notion.create_page(ids["search_excludes"], {"単語": nt.p_title(e["word"]), "カテゴリ": nt.p_select(e["category"]), "有効": nt.p_check(e["enabled"]),
                                                   "観測スコア(件/日)": nt.p_number(e["score"]), "備考": nt.p_text(e["note"])})
    for e in penalty:
        notion.create_page(ids["penalty_words"], {"単語": nt.p_title(e["word"]), "カテゴリ": nt.p_select(e["category"]), "減点": nt.p_number(penalty_points(e["word"], e["category"])),
                                                 "有効": nt.p_check(e["enabled"]), "備考": nt.p_text(e["note"])})
    for name, enabled, note in works:
        notion.create_page(ids["excluded_works"], {"作品名 / 略称": nt.p_title(name), "有効": nt.p_check(enabled), "備考": nt.p_text(note)})

    rename = {"最小フォロワー数": "最小フォロワー数", "AIモデル": "判定に使うAI"}
    for row in notion.query_all(ids["basic"]):
        key = nt.text(row["properties"].get("項目"))
        old = next((k for k, v in rename.items() if v == key), None)
        if old in carry:
            notion.update_page(row["id"], {"値": nt.p_text(carry[old])})

    d1.set_meta(MIGRATED_KEY, {"keywords": len(keywords), "search_excludes": len(search), "penalty_words": len(penalty), "works": len(works)})
    lines += ["", "新しい表へ書き込みました。今の 4表はそのまま残しています(今の仕組みはこれまでどおり動きます)。"]
    summary(lines)


if __name__ == "__main__":
    main()
