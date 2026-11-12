"""N7 参加者情報(の続き): 画像から読んだ X のアカウント(image_step)と、本文に書かれた ID(mention_step)のフォロワー数・フォロー数・アイコンを取る。

TwitterAPI.io の Get User Info(GET /twitter/user/info?userName=ID)で 1 人ずつ調べ、結果をユーザー台帳(users)に保存する。
画面の「関係するアカウント」は台帳から数字とアイコンを出す。
- 台帳にフォロワー数が入っている人や、調べてから 7 日以内の人は調べない。
- 7 日より前に調べた人は、そのあと新しく読んだ画像・本文にまた出てきたときだけ調べ直す(同じ人を何度も取らない)。
- いない ID(画像の読み違いなど)・凍結などの人も handle_lookups に記録し、画面に出す。
- 失敗しても収集・判定は止めない(調べられなかった人は次の収集でもう一度)。
料金(docs.twitterapi.io/pricing): 1 人 18 クレジット($0.00018)。
"""
import json
import re
import time
from datetime import datetime, timedelta, timezone

import requests

from finder import store

URL = "https://api.twitterapi.io/twitter/user/info"
CREDITS_PER_USER = 18
CREDITS_PER_USD = 100_000
RECHECK_DAYS = 7
MAX_LOOKUPS = 100  # 1 回の実行で調べる人数の上限(約 0.27 円 × 10)。超えた分は次の収集に回す
MAX_FAILS = 5  # 続けてこれだけ失敗したら、向こうの不調とみなして今回はやめる
HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")


class UserLookupError(Exception):
    pass


def cost_usd(calls):
    return CREDITS_PER_USER * calls / CREDITS_PER_USD


def _ts(iso):
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def candidates(d1, now=None, limit=MAX_LOOKUPS):
    """調べる ID(小文字)を、新しく読んだ画像・本文に出てきた順に返す。"""
    now = now or datetime.now(timezone.utc)
    recent = now - timedelta(days=RECHECK_DAYS)
    seen = {}  # handle -> いちばん新しく画像・本文から読んだ時刻
    # 読んだ記録は直近 RECHECK_DAYS 日ぶんだけ見る(それより前に読んだ ID は、そのときに調べ終わっている。D1 の読み取り行数を増やさないため)
    since = recent.replace(microsecond=0).isoformat()
    for r in d1.query("SELECT read_at, accounts_json FROM image_reads WHERE read_at >= ? "
                      "AND accounts_json IS NOT NULL AND accounts_json NOT IN ('', '[]') "
                      "UNION ALL SELECT scanned_at, accounts_json FROM text_mentions WHERE scanned_at >= ? "
                      "AND accounts_json IS NOT NULL AND accounts_json NOT IN ('', '[]')", [since, since]):
        try:
            accounts = json.loads(r["accounts_json"])
        except ValueError:
            continue
        read_at = _ts(r["read_at"])
        for a in accounts if isinstance(accounts, list) else []:
            h = str((a or {}).get("handle") or "").lower() if isinstance(a, dict) else ""
            if HANDLE.match(h) and read_at and (h not in seen or seen[h] < read_at):
                seen[h] = read_at
    if not seen:
        return []
    last = {}  # handle -> 最後にフォロワー数を取った時刻(台帳か、調べた記録)
    handles = list(seen)
    for i in range(0, len(handles), 90):
        part = handles[i:i + 90]
        marks = ", ".join("?" for _ in part)
        for r in d1.query(f"SELECT lower(username) AS h, profile_updated_at AS at FROM users "
                          f"WHERE followers IS NOT NULL AND lower(username) IN ({marks})", part):
            last[r["h"]] = max(filter(None, [last.get(r["h"]), _ts(r["at"]) or recent]))
        for r in d1.query(f"SELECT handle AS h, looked_at AS at FROM handle_lookups WHERE handle IN ({marks})", part):
            t = _ts(r["at"])
            if t:
                last[r["h"]] = max(filter(None, [last.get(r["h"]), t]))
    out = [h for h in handles if h not in last or (last[h] < recent and last[h] < seen[h])]
    out.sort(key=lambda h: seen[h], reverse=True)
    return out[:limit]


def fetch_user(http, api_key, handle):
    """(result, data, note) を返す。result は ok / not_found / unavailable。調べられなければ UserLookupError。"""
    for attempt in range(3):
        try:
            res = http.get(URL, headers={"X-API-Key": api_key}, params={"userName": handle}, timeout=30)
        except requests.RequestException as e:
            if attempt == 2:
                raise UserLookupError(f"接続に失敗しました: {e}") from e
            time.sleep(2 ** (attempt + 1))
            continue
        if res.status_code == 429 or res.status_code >= 500:
            time.sleep(2 ** (attempt + 1))
            continue
        if res.status_code != 200:
            raise UserLookupError(f"TwitterAPI.io がエラーを返しました ({res.status_code}): {res.text[:200]}")
        try:
            data = res.json()
        except ValueError as e:
            raise UserLookupError(f"答えが読めませんでした: {res.text[:200]}") from e
        # 公式の説明: いつも HTTP 200 で、status を見る。いない ID は status: error / msg: "user not found"
        if not isinstance(data, dict):
            raise UserLookupError(f"答えが読めませんでした: {str(data)[:200]}")
        if data.get("status") != "success":
            msg = str(data.get("msg") or data.get("message") or "")
            if "not found" in msg.lower():
                return "not_found", None, msg[:200]
            raise UserLookupError(f"TwitterAPI.io の答え: {msg[:200]}")
        user = data.get("data") or {}
        # 凍結などの人は unavailable: true で、ほかの項目は入らない
        if user.get("unavailable"):
            return "unavailable", None, str(user.get("unavailableReason") or user.get("message") or "")[:200]
        if not user.get("id") or user.get("followers") is None:
            raise UserLookupError(f"プロフィールが入っていませんでした: {str(data)[:200]}")
        return "ok", user, None
    raise UserLookupError("混んでいて、再試行後も調べられませんでした")


def run(d1, config, production, api_key, session=None, now=None):
    """調べて保存し、要約の行を返す。試し実行では何もしない。"""
    if not production:
        return []
    lines = ["", "## 画像・本文から読んだアカウントのフォロワー数", ""]
    if not api_key:
        return lines + ["- ⚠️ TWITTERAPI_KEY が登録されていないので、調べませんでした"]
    handles = candidates(d1, now=now)
    if not handles:
        return lines + ["- 新しく調べるアカウントはありませんでした"]
    http = session or requests.Session()
    calls = fails = 0
    counts = {"ok": 0, "not_found": 0, "unavailable": 0}
    error = None
    for h in handles:
        calls += 1
        try:
            result, user, note = fetch_user(http, api_key, h)
        except UserLookupError as e:
            fails += 1
            error = str(e)
            if fails >= MAX_FAILS:
                break
            continue
        fails = 0
        at = store.now_iso()
        if user:
            d1.insert_many("users", store.USER_COLUMNS, [store.full_user(user, at)], conflict="", upsert=store.USER_FULL_UPSERT)
        d1.query("INSERT OR REPLACE INTO handle_lookups (handle, looked_at, result, user_id, note) VALUES (?, ?, ?, ?, ?)",
                 [h, at, result, str(user["id"]) if user else None, note])
        counts[result] += 1
        time.sleep(0.2)
    usd = cost_usd(calls)
    store.record_cost(d1, "twitterapi.io", calls, usd, "画像・本文から読んだアカウントのフォロワー数")
    rate = config["basic"]["為替レート(円/ドル)"]
    left = len(handles) - sum(counts.values())
    lines.append(f"- 調べた {sum(counts.values())}人(取れた {counts['ok']}人"
                 + (f"、いない ID {counts['not_found']}人" if counts["not_found"] else "")
                 + (f"、凍結など {counts['unavailable']}人" if counts["unavailable"] else "")
                 + f")、費用 ${usd:.4f}(約{usd * rate:.2f}円)")
    if left:
        lines.append(f"- ⚠️ 調べられなかった {left}人(次の収集でもう一度): {error}")
    return lines
