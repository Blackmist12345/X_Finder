"""D1 にテーブルを作る(schema.sql を流す)。何度実行しても既存のデータは消えない。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from finder.d1 import D1  # noqa: E402


def statements(sql):
    lines = [line for line in sql.splitlines() if not line.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def main():
    d1 = D1.from_env()
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql")
    with open(path, encoding="utf-8") as f:
        stmts = statements(f.read())
    for s in stmts:
        d1.query(s)
    tables = [r["name"] for r in d1.query("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE '\\_%' ESCAPE '\\' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    print(f"{len(stmts)}文を実行しました。テーブル: {', '.join(tables)}")


if __name__ == "__main__":
    main()
