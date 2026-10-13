"""Notion の設定の表を親ページの下に作り、表の ID を D1 に保存する。

すでに作ってある表はそのままにして、まだない表(あとから足した表)だけを作る(全部作り直すときは FORCE=1)。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from finder.config import TABLE_IDS_KEY  # noqa: E402
from finder.d1 import D1  # noqa: E402
from finder.notion import Notion  # noqa: E402
from finder.tables import TABLES, seed_rows  # noqa: E402


def page_id(raw):
    clean = (raw or "").split("?")[0].replace("-", "")
    match = re.search(r"([0-9a-fA-F]{32})$", clean) or re.search(r"([0-9a-fA-F]{32})", clean)
    if not match:
        raise SystemExit("NOTION_PARENT_PAGE_ID から 32 桁のページ ID を読み取れませんでした")
    return match.group(1)


def main():
    d1 = D1.from_env()
    ids = {} if os.environ.get("FORCE") == "1" else dict(d1.get_meta(TABLE_IDS_KEY) or {})
    todo = [t for t in TABLES if t["key"] not in ids]
    if not todo:
        print("Notion の表はすべて作成済みです。作り直す場合は FORCE=1 で実行してください。")
        return
    notion = Notion(os.environ.get("NOTION_API_KEY"))
    parent = page_id(os.environ.get("NOTION_PARENT_PAGE_ID"))

    for table in todo:
        ids[table["key"]] = notion.create_database(parent, table["title"], table["props"])
        rows = seed_rows(table)
        for props in rows:
            notion.create_page(ids[table["key"]], props)
        d1.set_meta(TABLE_IDS_KEY, ids)  # 途中で止まっても、作った表は二重に作らない
        print(f"作成しました: {table['title']}(初期値 {len(rows)}行)")
    print("表の ID を D1 に保存しました。")


if __name__ == "__main__":
    main()
