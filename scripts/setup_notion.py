"""Notion 8表を親ページの下に作り、表の ID を D1 に保存する。

すでに作ってある場合は何もしない(作り直すときは FORCE=1)。
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
    if d1.get_meta(TABLE_IDS_KEY) and os.environ.get("FORCE") != "1":
        print("Notion 8表はすでに作成済みです。作り直す場合は FORCE=1 で実行してください。")
        return
    notion = Notion(os.environ.get("NOTION_API_KEY"))
    parent = page_id(os.environ.get("NOTION_PARENT_PAGE_ID"))

    ids = {}
    for table in TABLES:
        ids[table["key"]] = notion.create_database(parent, table["title"], table["props"])
        rows = seed_rows(table)
        for props in rows:
            notion.create_page(ids[table["key"]], props)
        print(f"作成しました: {table['title']}(初期値 {len(rows)}行)")
    d1.set_meta(TABLE_IDS_KEY, ids)
    print("表の ID を D1 に保存しました。")


if __name__ == "__main__":
    main()
