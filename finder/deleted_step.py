"""削除ポスト確認: X Finderに出ているカードの元のポストが、まだ見られるかを TwitterAPI.io にまとめて問い合わせる。

対象は新着・確認中・保留のカード(受注中と削除予定は確かめない)。
1 回でも返ってこなかったポストは、元のポストが見られない印(post_checks.gone_at)を付けて削除予定へ移す。
印の付いたカードは二度と確かめない(あとから自分で新着などに戻したら、そのまま残る)。
間隔は【5】期間と頻度の「削除確認の頻度(時間)」。0 なら確かめない。
"""
import time

import requests

from finder import collect, store

URL = "https://api.twitterapi.io/twitter/tweets"
MAX_IDS = 50                 # 1 回の問い合わせで送れる ID の上限
LAST_CHECK_KEY = "last_delete_check"
SLACK_SECONDS = 600          # 収集は 10 分ごとなので、10 分早くても次の間隔に入れる
LIST_DAYS = 14               # X Finderの一覧に出る日数(web/src/index.js と同じ)
FREQUENCY_ITEM = "削除確認の頻度(時間)"


class CheckError(Exception):
    pass


def target_ids(d1, now=None):
    """X Finderの一覧に出ている、新着・確認中・保留のカードのポスト ID。"""
    rows = d1.query(
        "SELECT p.id FROM posts p JOIN judgments j ON j.post_id = p.id LEFT JOIN reviews r ON r.post_id = p.id "
        "WHERE p.status = 'judged' "
        "AND (r.state IS NULL OR r.state NOT IN ('accepted', 'delete')) "
        "AND (p.collected_at >= ? OR r.state = 'hold') "
        "AND NOT EXISTS (SELECT 1 FROM blacklist b WHERE b.user_id = p.author_id) "
        "AND NOT EXISTS (SELECT 1 FROM post_checks pc WHERE pc.post_id = p.id AND pc.gone_at IS NOT NULL) "
        "ORDER BY p.id",
        [store.days_ago_iso(LIST_DAYS, now)],
    )
    return [r["id"] for r in rows]


def lookup(api_key, ids, session=None):
    """ids のうち、まだ見られるポストの ID の集合と、呼び出し回数を返す。"""
    http = session or requests.Session()
    headers = {"X-API-Key": api_key}
    found, calls = set(), 0
    for i in range(0, len(ids), MAX_IDS):
        part = ids[i:i + MAX_IDS]
        data = None
        for attempt in range(4):
            calls += 1
            try:
                res = http.get(URL, headers=headers, params={"tweet_ids": ",".join(part)}, timeout=30)
            except requests.RequestException as e:
                if attempt == 3:
                    raise CheckError(f"接続に失敗しました: {e}") from e
                time.sleep(2 ** (attempt + 1))
                continue
            if res.status_code == 200:
                data = res.json()
                break
            if res.status_code == 429 or res.status_code >= 500:
                time.sleep(2 ** (attempt + 1))
                continue
            raise CheckError(f"TwitterAPI.io がエラーを返しました ({res.status_code}): {res.text[:200]}")
        if not isinstance(data, dict) or data.get("status") != "success":
            raise CheckError(f"TwitterAPI.io の答えが読めませんでした: {str(data)[:200]}")
        returned = {str(t.get("id")) for t in data.get("tweets") or [] if t.get("id")}
        # まとめて 1 件も返ってこないのは、消えたのではなく向こうの不調のことが多い。印は付けずにやめる
        if len(part) >= 3 and not returned:
            raise CheckError(f"{len(part)}件まとめて 1 件も返ってきませんでした")
        found |= returned & set(part)
        time.sleep(0.5)
    return found, calls


def mark_gone(d1, ids, now=None):
    """見られなくなったポストに印を付け、削除予定へ移す。"""
    now = now or store.now_iso()
    rows = [[pid, now, now] for pid in ids]
    d1.insert_many("post_checks", ["post_id", "checked_at", "gone_at"], rows, conflict="",
                   upsert="ON CONFLICT(post_id) DO UPDATE SET checked_at = excluded.checked_at, gone_at = excluded.gone_at")
    d1.insert_many("reviews", ["post_id", "state", "opened_at", "updated_at"], [[pid, "delete", None, now] for pid in ids],
                   conflict="", upsert="ON CONFLICT(post_id) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at")


def due(d1, hours, now_ts=None):
    if hours <= 0:
        return False
    last = d1.get_meta(LAST_CHECK_KEY)
    now_ts = now_ts or time.time()
    return not last or now_ts - float(last) >= hours * 3600 - SLACK_SECONDS


def run(d1, config, production, api_key, session=None, now_ts=None):
    """間隔が来ていれば確かめ、Actions の結果に足す行を返す(件数だけ。ポストの中身は書かない)。"""
    hours = config["periods"][FREQUENCY_ITEM]
    if not production or not due(d1, hours, now_ts):
        return []
    if not api_key:
        return ["", "- ⚠️ 削除ポスト確認: TWITTERAPI_KEY が設定されていません"]
    ids = target_ids(d1)
    if not ids:
        d1.set_meta(LAST_CHECK_KEY, now_ts or time.time())
        return ["", "- 削除ポスト確認: 確かめるカードはありませんでした"]
    try:
        found, calls = lookup(api_key, ids, session)
    except CheckError as e:  # 印は付けず、次の収集でもう一度試す
        return ["", f"- ⚠️ 削除ポスト確認をやめました(印は付けていません): {e}"]
    gone = [pid for pid in ids if pid not in found]
    if gone:
        mark_gone(d1, gone)
    usd = collect.cost_usd(len(found), calls)
    store.record_cost(d1, "twitterapi.io", len(found), usd, "削除ポスト確認")
    d1.set_meta(LAST_CHECK_KEY, now_ts or time.time())
    rate = config["basic"]["為替レート(円/ドル)"]
    return ["", f"- 削除ポスト確認: {len(ids)}件を確かめ、見られなくなった {len(gone)}件を削除予定へ移しました"
                f"(費用 約{usd * rate:.2f}円、{hours}時間ごと)"]
