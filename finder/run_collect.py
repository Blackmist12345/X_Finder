"""収集の 1 回分: N1 設定読込 → N2 収集 → N3 一次フィルタ → N12/N15 保存 → N4 AI 判定 → N10 採点。

環境変数:
  MODE   production(保存して検索時間枠を進める)/ dry_run(何も保存しない試し)
  HOURS  dry_run と初回の実行で、何時間さかのぼるか
"""
import os
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from finder import collect, filters, image_step, judge, judge_step, store
from finder.config import load_config
from finder.d1 import D1
from finder.notion import Notion

OVERLAP_SECONDS = 180       # 前回の終わりから 3 分重ねて取る(取りこぼし防止。重なった分も課金されるので短めに)
MAX_WINDOW_HOURS = 48       # 長く止まっていた場合の上限
WINDOW_KEY = "last_window_end"
JST = timezone(timedelta(hours=9))


def jst(ts):
    return datetime.fromtimestamp(ts, JST).strftime("%m/%d %H:%M")


def write_summary(lines):
    text = "\n".join(lines) + "\n"
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)


def month_cost_lines(d1, basic):
    start = datetime.now(JST).replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
    rows = d1.query("SELECT service, SUM(usd) AS usd FROM costs WHERE at >= ? GROUP BY service ORDER BY service", [start])
    rate, goal = basic["為替レート(円/ドル)"], basic["月の費用の目標(円)"]
    total = sum(r["usd"] or 0 for r in rows) * rate
    parts = "、".join(f"{r['service']} 約{(r['usd'] or 0) * rate:.0f}円" for r in rows)
    return ["", f"今月の費用(保存した分): 約{total:.0f}円 / 目標 {goal}円" + (f"({parts})" if parts else "")]


def main():
    mode = os.environ.get("MODE", "production")
    hours = float(os.environ.get("HOURS") or 1)
    production = mode == "production"

    d1 = D1.from_env()
    notion = Notion(os.environ.get("NOTION_API_KEY"))
    config, warnings = load_config(notion, d1)
    kw, basic = config["keywords"], config["basic"]
    rate = basic["為替レート(円/ドル)"]

    query, used, overflow = collect.build_query(kw["group_a"], kw["group_b"], config["search_excludes"])

    until_ts = time.time() - 30
    last_end = d1.get_meta(WINDOW_KEY) if production else None
    since_ts = (last_end - OVERLAP_SECONDS) if last_end else until_ts - hours * 3600
    if until_ts - since_ts > MAX_WINDOW_HOURS * 3600:
        warnings.append(f"前回から{MAX_WINDOW_HOURS}時間以上空いていたので、直近{MAX_WINDOW_HOURS}時間だけ取りました")
        since_ts = until_ts - MAX_WINDOW_HOURS * 3600

    started = store.now_iso()
    run_id = None
    if production:
        try:
            d1.ensure_schema()
        except Exception as e:  # 表がそろっていれば収集は続けられる
            warnings.append(f"D1 の表の確認に失敗しました: {e}")
        try:
            purged = store.purge_deleted(d1)
            if purged:
                warnings.append(f"「削除予定」の案件のうち投稿から{store.DELETE_AFTER_DAYS}日たった {purged}件を消しました")
        except Exception as e:  # 消せなくても収集は続ける(次の実行でもう一度試す)
            warnings.append(f"「削除予定」の案件を消せませんでした: {e}")
        run_id = d1.query(
            "INSERT INTO runs (started_at, window_start, window_end, status) VALUES (?, ?, ?, 'running') RETURNING id",
            [started, datetime.fromtimestamp(since_ts, timezone.utc).isoformat(), datetime.fromtimestamp(until_ts, timezone.utc).isoformat()],
        )[0]["id"]

    title = "## 収集の結果" + ("" if production else "(試し実行: 何も保存していません)")
    head = [title, "", f"- 検索時間枠: {jst(since_ts)} 〜 {jst(until_ts)}(日本時間)",
            f"- 検索除外語: 検索に入れた {len(used)}語 / 入りきらず収集後に除いた {len(overflow)}語(クエリ {len(query)}文字)"]

    try:
        tweets, calls = collect.fetch(os.environ.get("TWITTERAPI_KEY"), query, since_ts, until_ts)
    except collect.CollectError as e:
        if production:
            d1.query("UPDATE runs SET finished_at = ?, status = 'failed', note = ? WHERE id = ?", [store.now_iso(), str(e), run_id])
        write_summary(head + ["", f"**収集に失敗しました。検索時間枠は進めていません。** {e}"] + [f"- ⚠️ {w}" for w in warnings])
        sys.exit(1)

    usd = collect.cost_usd(len(tweets), calls)
    existing = store.existing_post_ids(d1, [str(t.get("id")) for t in tweets])
    fresh = [t for t in tweets if str(t.get("id")) not in existing]

    try:
        blocked = store.blacklisted_ids(d1)
    except Exception as e:  # 読めなくても収集は続ける(その回はブラックリストの人も判定に回る)
        blocked = set()
        warnings.append(f"ブラックリストを読めませんでした: {e}")

    decisions, reasons, penalty_count = {}, Counter(), Counter()
    min_followers = basic["最小フォロワー数"]
    for tw in fresh:
        in_blacklist = str((tw.get("author") or {}).get("id")) in blocked
        reason = store.BLACKLIST_REASON if in_blacklist else filters.skip_reason(tw, overflow, min_followers)
        hits = filters.penalty_hits(tw.get("text", ""), config["penalty_words"])
        decisions[str(tw.get("id"))] = ("skipped" if reason else "new", reason, hits)
        if reason:
            reasons[reason.split(":")[0]] += 1
        for h in hits:
            penalty_count[h["word"]] += 1

    posts, full_users, partial_users, links = store.extract(fresh, decisions)
    if production:
        store.save(d1, posts, full_users, partial_users, links)
        store.record_cost(d1, "twitterapi.io", len(tweets), usd, f"収集 run {run_id}")
        d1.set_meta(WINDOW_KEY, until_ts)
        d1.query("UPDATE runs SET finished_at = ?, status = 'ok', fetched = ?, new_posts = ?, api_calls = ?, cost_usd = ? WHERE id = ?",
                 [store.now_iso(), len(tweets), len(fresh), calls, usd, run_id])

    kept = [t for t in fresh if decisions[str(t.get("id"))][0] == "new"]
    lines = head + [
        f"- 取得: {len(tweets)}件(うち新しいポスト {len(fresh)}件、AI 判定待ち {len(kept)}件)",
        f"- ユーザー台帳: プロフィールあり {len(full_users)}人 / ID と名前だけ {len(partial_users)}人",
        f"- 費用: ${usd:.4f}(約{usd * rate:.2f}円、呼び出し {calls}回)",
    ]
    if reasons:
        lines.append("- 除いた理由: " + "、".join(f"{k} {v}件" for k, v in reasons.most_common()))
    if penalty_count:
        lines.append("- 減点語に当たった数: " + "、".join(f"{k} {v}件" for k, v in penalty_count.most_common(10)))
    for w in warnings:
        lines.append(f"- ⚠️ {w}")
    lines += judge_step.run(d1, config, production, {k: os.environ.get(k) for k in judge.KEY_NAMES.values()},
                            tweets=fresh, decisions=decisions)
    try:
        lines += image_step.run(d1, config, production, {k: os.environ.get(k) for k in judge.KEY_NAMES.values()})
    except Exception as e:  # 画像が読めなくても収集・判定の結果は残す
        lines += ["", f"- ⚠️ 画像からアカウントを読めませんでした: {e}"]
    lines += month_cost_lines(d1, basic)
    write_summary(lines)


if __name__ == "__main__":
    main()
