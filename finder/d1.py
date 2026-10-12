"""Cloudflare D1 (N12 データ保存) の HTTP API クライアント。"""
import json
import os
import time

import requests

MAX_PARAMS = 100  # D1 の 1 文あたりのバインド変数の上限


class D1Error(Exception):
    pass


class D1:
    def __init__(self, account_id, database_id, token):
        missing = [n for n, v in (("CLOUDFLARE_ACCOUNT_ID", account_id), ("D1_DATABASE_ID", database_id), ("CLOUDFLARE_API_TOKEN", token)) if not v]
        if missing:
            raise D1Error(f"環境変数が不足しています: {', '.join(missing)}")
        self.url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{database_id}/query"
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("CLOUDFLARE_ACCOUNT_ID"), os.environ.get("D1_DATABASE_ID"), os.environ.get("CLOUDFLARE_API_TOKEN"))

    def query(self, sql, params=None):
        payload = {"sql": sql}
        if params:
            payload["params"] = list(params)
        for attempt in range(4):
            try:
                res = requests.post(self.url, headers=self.headers, json=payload, timeout=60)
            except requests.RequestException as e:
                if attempt == 3:
                    raise D1Error(f"D1 に接続できません: {e}") from e
                time.sleep(2 ** attempt)
                continue
            if res.status_code == 429 or res.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            data = res.json() if res.content else {}
            if res.status_code != 200 or not data.get("success"):
                raise D1Error(f"D1 のクエリが失敗しました ({res.status_code}): {json.dumps(data.get('errors', data), ensure_ascii=False)[:300]}")
            results = data.get("result") or [{}]
            return results[-1].get("results", [])
        raise D1Error("D1 のクエリが再試行後も失敗しました")

    def insert_many(self, table, columns, rows, conflict="OR IGNORE", upsert=""):
        """複数行をまとめて INSERT する。バインド変数の上限に合わせて分割する。"""
        if not rows:
            return
        per_row = len(columns)
        chunk = max(1, MAX_PARAMS // per_row)
        cols = ", ".join(columns)
        one = "(" + ", ".join("?" for _ in columns) + ")"
        for i in range(0, len(rows), chunk):
            part = rows[i:i + chunk]
            sql = f"INSERT {conflict} INTO {table} ({cols}) VALUES " + ", ".join(one for _ in part) + (f" {upsert}" if upsert else "")
            params = [v for row in part for v in row]
            self.query(sql, params)

    # ---- meta テーブル(小さな設定値の置き場) ----

    def get_meta(self, key, default=None):
        rows = self.query("SELECT value FROM meta WHERE key = ?", [key])
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value"])
        except (TypeError, ValueError):
            return default

    def set_meta(self, key, value):
        self.query(
            "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            [key, json.dumps(value, ensure_ascii=False)],
        )
