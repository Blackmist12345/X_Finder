"""Notion API の薄いラッパー(N1 設定読込と初期設定スクリプトで使う)。"""
import time

import requests

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"


class NotionError(Exception):
    pass


class Notion:
    def __init__(self, api_key):
        if not api_key:
            raise NotionError("NOTION_API_KEY が設定されていません")
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Notion-Version": VERSION,
            "Content-Type": "application/json",
        }

    def _request(self, method, path, payload=None):
        for attempt in range(4):
            res = requests.request(method, f"{API}{path}", headers=self.headers, json=payload, timeout=30)
            if res.status_code == 429 or res.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if res.status_code != 200:
                raise NotionError(f"{method} {path} が失敗しました ({res.status_code}): {res.text[:300]}")
            return res.json()
        raise NotionError(f"{method} {path} が再試行後も失敗しました")

    def query_all(self, database_id):
        rows, cursor = [], None
        while True:
            payload = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            data = self._request("POST", f"/databases/{database_id}/query", payload)
            rows.extend(data.get("results", []))
            if not data.get("has_more"):
                return rows
            cursor = data.get("next_cursor")

    def create_database(self, parent_page_id, title, properties):
        payload = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": title}}],
            "properties": properties,
        }
        return self._request("POST", "/databases", payload)["id"]

    def rename_database(self, database_id, title):
        self._request("PATCH", f"/databases/{database_id}", {"title": [{"type": "text", "text": {"content": title}}]})

    def update_page(self, page_id, properties):
        self._request("PATCH", f"/pages/{page_id}", {"properties": properties})
        time.sleep(0.35)

    def create_page(self, database_id, properties):
        self._request("POST", "/pages", {"parent": {"database_id": database_id}, "properties": properties})
        time.sleep(0.35)  # Notion は 1 秒に 3 回まで
        time.sleep(0.35)  # Notion の上限は平均 3 リクエスト/秒


# ---- プロパティの読み取り ----

def text(prop):
    if not prop:
        return ""
    kind = prop.get("type")
    if kind in ("title", "rich_text"):
        return "".join(t.get("plain_text", "") for t in prop.get(kind, [])).strip()
    if kind == "select":
        return (prop.get("select") or {}).get("name", "").strip()
    if kind == "number":
        value = prop.get("number")
        return "" if value is None else str(value)
    return ""


def number(prop):
    if not prop or prop.get("type") != "number":
        return None
    return prop.get("number")


def checkbox(prop):
    return bool(prop and prop.get("checkbox"))


# ---- プロパティの書き込み ----

def p_title(value):
    return {"title": [{"text": {"content": str(value)}}]}


def p_text(value):
    return {"rich_text": [{"text": {"content": str(value)}}]} if value else {"rich_text": []}


def p_select(value):
    return {"select": {"name": value}} if value else {"select": None}


def p_number(value):
    return {"number": value}


def p_check(value):
    return {"checkbox": bool(value)}
