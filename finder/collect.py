"""N2 収集: TwitterAPI.io の advanced_search でポストを集める。

途中で失敗したら CollectError を投げる。呼び出し側は検索時間枠を進めない。
"""
import time

import requests

URL = "https://api.twitterapi.io/twitter/tweet/advanced_search"
MAX_QUERY_LENGTH = 440
MAX_PAGES = 50
CREDITS_PER_TWEET = 15
CREDITS_PER_USD = 100_000


class CollectError(Exception):
    pass


def build_query(group_a, group_b, search_excludes, max_length=MAX_QUERY_LENGTH):
    """(撮影者語 OR ...) (募集語 OR ...) -"除外語" ... を組み立てる。

    除外語は観測スコアの高い順に、文字数の上限まで入れる。
    入りきらなかった語は返り値の overflow に入れ、収集後に同じ基準で取り除く。
    """
    base = "((" + " OR ".join(f'"{w}"' for w in group_a) + ") (" + " OR ".join(f'"{w}"' for w in group_b) + "))"
    query, used, overflow = base, [], []
    for item in sorted(search_excludes, key=lambda e: e.get("score", 0), reverse=True):
        candidate = f'{query} -"{item["word"]}"'
        if len(candidate) <= max_length:
            query = candidate
            used.append(item["word"])
        else:
            overflow.append(item["word"])
    return query, used, overflow


def cost_usd(tweets_returned, calls):
    """料金ページの表記: 返ってきたポスト1件 15 クレジット、1回の呼び出しの最低料金 15 クレジット。"""
    credits = max(CREDITS_PER_TWEET * tweets_returned, CREDITS_PER_TWEET * calls)
    return credits / CREDITS_PER_USD


def fetch(api_key, query, since_ts, until_ts, session=None):
    """時間枠 [since_ts, until_ts) のポストをすべて取る。返り値は (tweets, calls)。"""
    if not api_key:
        raise CollectError("TWITTERAPI_KEY が設定されていません")
    http = session or requests.Session()
    headers = {"X-API-Key": api_key}
    full_query = f"{query} since_time:{int(since_ts)} until_time:{int(until_ts)}"
    tweets, seen, cursor, calls = [], set(), None, 0

    for page in range(1, MAX_PAGES + 1):
        params = {"query": full_query, "queryType": "Latest"}
        if cursor:
            params["cursor"] = cursor
        data = None
        for attempt in range(4):
            calls += 1
            try:
                res = http.get(URL, headers=headers, params=params, timeout=30)
            except requests.RequestException as e:
                if attempt == 3:
                    raise CollectError(f"{page}ページ目で接続に失敗しました: {e}") from e
                time.sleep(2 ** (attempt + 1))
                continue
            if res.status_code == 200:
                data = res.json()
                break
            if res.status_code == 429 or res.status_code >= 500:
                time.sleep(2 ** (attempt + 1))
                continue
            raise CollectError(f"{page}ページ目で TwitterAPI.io がエラーを返しました ({res.status_code}): {res.text[:200]}")
        if data is None:
            raise CollectError(f"{page}ページ目が再試行後も取得できませんでした")

        batch = data.get("tweets", []) if isinstance(data, dict) else []
        for tw in batch:
            tid = str(tw.get("id"))
            if tid not in seen:
                seen.add(tid)
                tweets.append(tw)
        cursor = data.get("next_cursor")
        if not data.get("has_next_page") or not cursor or not batch:
            return tweets, calls
        time.sleep(0.5)

    raise CollectError(f"{MAX_PAGES}ページを超えました。時間枠を短くして再実行してください")
