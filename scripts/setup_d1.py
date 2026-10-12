"""D1 にテーブルを作る(schema.sql を流す)。何度実行しても既存のデータは消えない。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from finder.d1 import D1, schema_statements, statements  # noqa: E402,F401


def main():
    d1 = D1.from_env()
    stmts = schema_statements()
    for s in stmts:
        d1.query(s)
    tables = [r["name"] for r in d1.query("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE '\\_%' ESCAPE '\\' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    print(f"{len(stmts)}文を実行しました。テーブル: {', '.join(tables)}")


if __name__ == "__main__":
    main()
