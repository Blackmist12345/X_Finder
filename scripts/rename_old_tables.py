"""使わなくなった Notion の表の名前を【91】からの番号に付け直す(使っている表は【1】〜【7】のまま)。

表は名前ではなく D1 に控えた ID で読んでいるので、名前を変えても動きは変わらない。
何度動かしても同じ名前になるだけ。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from finder import notion as nt  # noqa: E402
from finder.d1 import D1  # noqa: E402
from scripts.merge_points import OLD_TABLES_KEY  # noqa: E402

# 「(旧)」は残す(Latte 2026-10-28)
RETIRED_TITLES = {
    "penalty_words": "(旧)【91】減点語",
    "excluded_works": "(旧)【92】除外作品",
    "scores": "(旧)【93】点数表",
    "critical": "(旧)【94】クリティカル条件",
}


def main():
    old = D1.from_env().get_meta(OLD_TABLES_KEY) or {}
    if not old:
        print("使わなくなった表の控えがありません。何もしません。")
        return
    notion = nt.Notion(os.environ.get("NOTION_API_KEY"))
    lines = ["## 使わない表の名前の付け直し", ""]
    for key, title in RETIRED_TITLES.items():
        if key in old:
            notion.rename_database(old[key], title)
            lines.append(f"- {title}")
    print("\n".join(lines))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
