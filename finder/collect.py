"""N2 収集: TwitterAPI.io の advanced_search でポストを集める。

途中で失敗したら CollectError を投げる。呼び出し側は検索時間枠を進めない。
"""
import time

import requests

URL = "https://api.twitterapi.io/twitter/tweet/advanced_search"
MAX_QUERY_LENGTH = 440
# 検索式に入れる除外語の数の上限。2026-11-10 ごろから、除外語(-"語")を37語入れた式だと
# TwitterAPI.io が時間の指定なしでも0件を返すようになった(診断モードで確認)。
# 何語までなら返るか分かるまで 0 にして、除外語はすべて収集後に取り除く。
MAX_QUERY_EXCLUDES = 0
MAX_PAGES = 50
BACKFILL_MAX_PAGES = 150  # 取り直しは時間枠が長いので多めに(最大3,000件、約69円)
CREDITS_PER_TWEET = 15
CREDITS_PER_USD = 100_000


class CollectError(Exception):
    pass


def base_query(group_a, group_b):
    """除外語を入れる前の (撮影者語 OR ...) (募集語 OR ...)。"""
    return "((" + " OR ".join(f'"{w}"' for w in group_a) + ") (" + " OR ".join(f'"{w}"' for w in group_b) + "))"


def build_query(group_a, group_b, search_excludes, max_length=MAX_QUERY_LENGTH, max_excludes=None):
    """(撮影者語 OR ...) (募集語 OR ...) -"除外語" ... を組み立てる。

    除外語は観測スコアの高い順に、文字数の上限と語数の上限(max_excludes、省略時は MAX_QUERY_EXCLUDES)まで入れる。
    入りきらなかった語は返り値の overflow に入れ、収集後に同じ基準で取り除く。
    """
    limit = MAX_QUERY_EXCLUDES if max_excludes is None else max_excludes
    base = base_query(group_a, group_b)
    query, used, overflow = base, [], []
    for item in sorted(search_excludes, key=lambda e: e.get("score", 0), reverse=True):
        candidate = f'{query} -"{item["word"]}"'
        if len(used) < limit and len(candidate) <= max_length:
            query = candidate
            used.append(item["word"])
        else:
            overflow.append(item["word"])
    return query, used, overflow


def cost_usd(tweets_returned, calls):
    """料金ページの表記: 返ってきたポスト1件 15 クレジット、1回の呼び出しの最低料金 15 クレジット。"""
    credits = max(CREDITS_PER_TWEET * tweets_returned, CREDITS_PER_TWEET * calls)
    return credits / CREDITS_PER_USD


def describe_response(page, status_code, data):
    """返事の中身を 1 行にまとめる(ログ用)。キーやクエリは入れない。"""
    if not isinstance(data, dict):
        return f"{page}ページ目: HTTP {status_code}・JSON がオブジェクトではありません({type(data).__name__})"
    parts = [f"{page}ページ目: HTTP {status_code}"]
    tweets = data.get("tweets")
    parts.append(f"tweets {len(tweets)}件" if isinstance(tweets, list) else "tweets なし")
    parts.append(f"次のページ {'あり' if data.get('has_next_page') else 'なし'}")
    for key in ("status", "msg", "message", "error"):
        if data.get(key) not in (None, ""):
            parts.append(f"{key}={str(data[key])[:150]}")
    if not isinstance(tweets, list):
        parts.append("返事のキー: " + ", ".join(sorted(map(str, data.keys()))[:15]))
    return "・".join(parts)


def response_error(data):
    """HTTP 200 でも中身がエラーなら、その説明を返す。問題なければ None。"""
    if not isinstance(data, dict):
        return "返事が JSON のオブジェクトではありません"
    if str(data.get("status", "")).lower() == "error":
        return str(data.get("msg") or data.get("message") or data.get("error") or "status=error")[:200]
    if not isinstance(data.get("tweets"), list):
        note = data.get("msg") or data.get("message") or data.get("error")
        if note:  # tweets が無く説明の文だけがある → エラーとみなす
            return f"tweets がなく、説明の文がありました: {str(note)[:200]}"
    return None


def fetch(api_key, query, since_ts, until_ts, session=None, diag=None, max_pages=MAX_PAGES):
    """時間枠 [since_ts, until_ts) のポストをすべて取る。返り値は (tweets, calls)。

    diag にリストを渡すと、各ページの返事の中身(HTTP 状態・件数・エラーの文)を 1 行ずつ足す。
    HTTP 200 でも中身がエラーなら CollectError を投げる(0件として扱って時間枠を進めないため)。
    """
    if not api_key:
        raise CollectError("TWITTERAPI_KEY が設定されていません")
    http = session or requests.Session()
    headers = {"X-API-Key": api_key}
    full_query = f"{query} since_time:{int(since_ts)} until_time:{int(until_ts)}"
    tweets, seen, cursor, calls = [], set(), None, 0

    for page in range(1, max_pages + 1):
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
                try:
                    data = res.json()
                except ValueError as e:
                    raise CollectError(f"{page}ページ目の返事が JSON ではありません: {res.text[:200]}") from e
                break
            if res.status_code == 429 or res.status_code >= 500:
                time.sleep(2 ** (attempt + 1))
                continue
            raise CollectError(f"{page}ページ目で TwitterAPI.io がエラーを返しました ({res.status_code}): {res.text[:200]}")
        if data is None:
            raise CollectError(f"{page}ページ目が再試行後も取得できませんでした")
        if diag is not None:
            diag.append(describe_response(page, 200, data))
        problem = response_error(data)
        if problem:
            raise CollectError(f"{page}ページ目で TwitterAPI.io が成功(200)の形でエラーを返しました: {problem}")

        batch = data.get("tweets") or []
        for tw in batch:
            tid = str(tw.get("id"))
            if tid not in seen:
                seen.add(tid)
                tweets.append(tw)
        cursor = data.get("next_cursor")
        if not data.get("has_next_page") or not cursor or not batch:
            return tweets, calls
        time.sleep(0.5)

    raise CollectError(f"{max_pages}ページを超えました。時間枠を短くして再実行してください")
