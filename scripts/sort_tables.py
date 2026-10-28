"""Notion の設定ページで、表を番号の順(使っている【1】〜【7】、そのあと使わない【91】〜)に並べ直す。

API には並び順を変える方法がないので、表を作業用のページへ移してすぐ戻す(戻した表はいちばん下に付く)。
これを番号の順に1つずつ行うと、その順に並ぶ。表の ID も中身も変わらない。作業用のページは最後にごみ箱へ入れる。
MODE=preview(初期値): 今の並びと並べ直したあとの並びを出すだけで、何も動かさない。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from finder import notion as nt  # noqa: E402
from finder.config import TABLE_IDS_KEY  # noqa: E402
from finder.d1 import D1  # noqa: E402
from finder.tables import TABLES  # noqa: E402
from scripts.merge_points import OLD_TABLES_KEY  # noqa: E402
from scripts.rename_old_tables import RETIRED_TITLES  # noqa: E402

TEMP_TITLE = "(並べ替えの作業用。消してよい)"


def wanted_order(d1):
    ids, old = d1.get_meta(TABLE_IDS_KEY) or {}, d1.get_meta(OLD_TABLES_KEY) or {}
    return [ids[t["key"]] for t in TABLES if t["key"] in ids] + [old[k] for k in RETIRED_TITLES if k in old]


def report(lines, title, rows):
    lines += [f"### {title}", ""] + [f"{i}. {name}" for i, (_, name) in enumerate(rows, 1)] + [""]


def main():
    mode = os.environ.get("MODE", "preview")
    from scripts.setup_notion import page_id
    parent = page_id(os.environ.get("NOTION_PARENT_PAGE_ID"))
    notion = nt.Notion(os.environ.get("NOTION_API_KEY"))
    order = wanted_order(D1.from_env())
    now = notion.child_databases(parent)
    names = {i.replace("-", ""): n for i, n in now}
    lines = ["## 表の並べ直し" + ("" if mode == "apply" else "(案。まだ何も動かしていません)"), ""]
    report(lines, "今の並び", now)
    planned = [(i, names.get(i.replace("-", ""), "(このページの下にない表)")) for i in order]
    report(lines, "並べ直したあと", planned)
    if mode == "apply":
        temp = notion.create_child_page(parent, TEMP_TITLE)
        try:
            for db_id in order:
                notion.move_database(db_id, temp)
                notion.move_database(db_id, parent)
        finally:
            notion.archive_page(temp)
        after = notion.child_databases(parent)
        report(lines, "並べ直した結果", after)
        ok = [i.replace("-", "") for i, _ in after][:len(order)] == [i.replace("-", "") for i in order]
        lines.append("番号の順に並びました。" if ok else "思った順になっていません。上の結果を確かめてください。")
    print("\n".join(lines))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
