import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from finder.d1 import D1, schema_statements  # noqa: E402


class SqliteD1(D1):
    """D1 の代わりにメモリ上の SQLite で同じ SQL を実行する(D1 も中身は SQLite)。"""

    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        for s in schema_statements():
            self.db.execute(s)

    def query(self, sql, params=None):
        if not params and sql.count(";") > 0:  # D1 と同じく、複数の文をまとめて流せるようにする
            self.db.executescript(sql)
            return []
        cur = self.db.execute(sql, list(params or []))
        rows = [dict(r) for r in cur.fetchall()]
        self.db.commit()
        return rows


@pytest.fixture
def d1():
    return SqliteD1()


def tweet(tid, text="カメラマンさん募集します", author_id="100", followers=500, **extra):
    t = {
        "id": tid, "text": text, "url": f"https://x.com/u/status/{tid}", "createdAt": "Sat Oct 10 03:00:00 +0000 2026",
        "author": {"id": author_id, "userName": f"user{author_id}", "name": f"名前{author_id}", "followers": followers,
                   "following": 100, "description": "コスプレ", "location": "東京", "profilePicture": "https://example.com/p.jpg"},
        "entities": {"user_mentions": []},
    }
    t.update(extra)
    return t


@pytest.fixture
def make_tweet():
    return tweet


def dumps(x):
    return json.dumps(x, ensure_ascii=False)
