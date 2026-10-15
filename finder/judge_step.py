"""収集のあとに続けて行う N4 AI 判定 と N10 採点。

production: D1 で判定待ち(status=new)のポストが「まとめて判定する件数」たまったら判定し、結果を保存する。
  たまらなくても、いちばん古いポストが「判定を待つ最長時間(分)」待ったら判定する。
dry_run: 今回集めたポストを判定して要約に出すだけで、何も保存しない。
"""
import json
from collections import Counter
from datetime import datetime, timedelta, timezone

from finder import judge, store
from finder.score import score

MAX_PER_RUN = 200  # 1 回で判定する上限(長く止まっていたあとに費用が跳ねないように)


def items_from_tweets(tweets, decisions):
    out = []
    for tw in tweets:
        tid = str(tw.get("id"))
        status, _, penalties = decisions.get(tid, ("new", None, []))
        if status != "new":
            continue
        author = tw.get("author") or {}
        out.append({"id": tid, "text": tw.get("text"), "url": tw.get("url"), "penalties": penalties or [],
                    "created_at": store.to_iso(tw.get("createdAt")),
                    "quoted_text": (tw.get("quoted_tweet") or {}).get("text"),
                    "author_location": author.get("location"), "author_description": author.get("description"),
                    "author_followers": author.get("followers"), "author_following": author.get("following")})
    return out[:MAX_PER_RUN]


def items_from_db(d1):
    rows = d1.query(
        "SELECT p.id, p.text, p.url, p.penalty_json, p.collected_at, p.created_at, q.text AS quoted_text, "
        "u.location AS author_location, u.description AS author_description, "
        "u.followers AS author_followers, u.following AS author_following "
        "FROM posts p LEFT JOIN posts q ON q.id = p.quoted_id LEFT JOIN users u ON u.id = p.author_id "
        "WHERE p.status = 'new' ORDER BY p.collected_at, p.id LIMIT ?", [MAX_PER_RUN])
    for r in rows:
        r["penalties"] = json.loads(r.pop("penalty_json") or "[]")
    return rows


JUDGMENT_COLUMNS = ["post_id", "judged_at", "model", "case_type", "is_cosplay", "works", "excluded_work", "prefecture",
                    "prefecture_source", "place", "dates", "summary", "reject_reason", "score", "score_json",
                    "critical", "critical_reason", "raw_json"]


def evaluate(items, results, config):
    """判定結果に対象外の理由と点数を付ける。[(item, result, reject, total, detail, critical, critical_reason)]"""
    cosplay_only = config["basic"]["コスプレに絞る"] == "オン"
    # 第1段階で保存したポストは減点語のカテゴリを持たないので、今の減点語の表から補う
    categories = {p["word"]: p.get("category") for p in config.get("penalty_words", [])}
    for it in items:
        for p in it["penalties"]:
            p.setdefault("category", categories.get(p["word"], "その他"))
    out = []
    for it in items:
        r = results.get(it["id"])
        if r is None:
            continue
        reject = judge.reject_reason(r, cosplay_only)
        total, detail, critical, why = (None, [], False, "") if reject else score(
            r, it["penalties"], it.get("text"), config,
            {"followers": it.get("author_followers"), "following": it.get("author_following")})
        out.append((it, r, reject, total, detail, critical, why))
    return out


def save(d1, evaluated, missing_ids, model):
    now = store.now_iso()
    rows = []
    for it, r, reject, total, detail, critical, why in evaluated:
        rows.append([it["id"], now, model, r.get("case_type"), int(bool(r.get("is_cosplay"))), r.get("works"),
                     r.get("excluded_work"), r.get("prefecture"), r.get("prefecture_source"), r.get("place"),
                     r.get("dates"), r.get("summary"), reject, total,
                     json.dumps(detail, ensure_ascii=False) if detail else None, int(critical), why or None,
                     json.dumps(r, ensure_ascii=False)])
    d1.insert_many("judgments", JUDGMENT_COLUMNS, rows, conflict="OR REPLACE")

    groups = {}
    for it, _, reject, *_ in evaluated:
        groups.setdefault(("rejected", reject) if reject else ("judged", None), []).append(it["id"])
    if missing_ids:
        groups[("judge_error", "AI が判定を返さなかった")] = list(missing_ids)
    for (status, reason), ids in groups.items():
        for i in range(0, len(ids), 90):
            part = ids[i:i + 90]
            d1.query(f"UPDATE posts SET status = ?, skip_reason = ? WHERE id IN ({', '.join('?' for _ in part)})",
                     [status, reason] + part)


def should_wait(items, batch_size, max_wait_minutes, now=None):
    """判定を次回に回すなら True。件数がそろうか、いちばん古いポストが待ちすぎたら判定する。"""
    if len(items) >= batch_size:
        return False
    oldest = min((it.get("collected_at") or "") for it in items)
    now = now or datetime.now(timezone.utc)
    try:
        waited = now - datetime.fromisoformat(oldest)
    except ValueError:
        return False
    return waited < timedelta(minutes=max_wait_minutes)


def run(d1, config, production, api_keys, tweets=None, decisions=None, session=None):
    """判定と採点を行い、要約の行を返す。失敗しても例外は出さない(ポストは判定待ちのまま残る)。

    api_keys は {環境変数の名前: 鍵}。判定に使うAI の会社に合う鍵を使う。
    """
    lines = ["", "## AI 判定と採点" + ("" if production else "(試し実行: 何も保存していません)"), ""]
    basic = config["basic"]
    model = basic["判定に使うAI"]
    try:
        key_name = judge.KEY_NAMES[judge.provider(model)]
    except judge.JudgeError as e:
        return lines + [f"- ⚠️ {e}(ポストは判定待ちのまま残ります)"]
    api_key = (api_keys or {}).get(key_name)
    if not api_key:
        return lines + [f"- ⚠️ {key_name} が登録されていないので、判定は行いませんでした(ポストは判定待ちのまま残ります)"]

    items = items_from_db(d1) if production else items_from_tweets(tweets or [], decisions or {})
    if not items:
        return lines + ["- 判定待ちのポストはありませんでした"]
    batch_size = max(1, basic["まとめて判定する件数"])
    max_wait = config["periods"]["判定を待つ最長時間(分)"]
    if production and should_wait(items, batch_size, max_wait):
        return lines + [f"- 判定待ち {len(items)}件。{batch_size}件たまるか、いちばん古いポストが{max_wait}分待ったら判定します"]

    try:
        results, t_in, t_out, calls = judge.judge(items, api_key, model, config["excluded_works"], batch_size, session,
                                                     config.get("emojis"), config.get("events"))
    except judge.JudgeError as e:
        return lines + [f"- ⚠️ 判定に失敗しました。ポストは判定待ちのまま、次の実行でもう一度判定します。{e}"]

    usd = judge.cost_usd(model, t_in, t_out)
    evaluated = evaluate(items, results, config)
    missing = [it["id"] for it in items if it["id"] not in results]
    if production:
        save(d1, evaluated, missing, model)
        store.record_cost(d1, judge.provider(model), t_in + t_out, usd, f"{model} {len(items)}件")

    kept = sorted([e for e in evaluated if not e[2]], key=lambda e: -e[3])
    reasons = Counter(e[2].split(":")[0] for e in evaluated if e[2])
    rate = basic["為替レート(円/ドル)"]
    lines += [
        f"- 判定: {len(items)}件(対象 {len(kept)}件 / 対象外 {sum(reasons.values())}件"
        + (f" / 返事なし {len(missing)}件" if missing else "") + ")",
        f"- AI: {model}、呼び出し {calls}回、{t_in + t_out:,}トークン、費用 ${usd:.4f}(約{usd * rate:.2f}円)",
    ]
    if reasons:
        lines.append("- 対象外の理由: " + "、".join(f"{k} {v}件" for k, v in reasons.most_common()))
    critical = sum(1 for e in kept if e[5])
    if critical:
        lines.append(f"- クリティカル: {critical}件")
    if kept:
        lines += ["", "### 点数の高い案件(先頭10件)", ""]
        for it, r, _, total, detail, is_critical, _ in kept[:10]:
            mark = "🔴 " if is_critical else ""
            pref = r.get("prefecture") or "不明"
            lines.append(f"- {mark}**{total:g}点** {r.get('case_type')} / {pref} / {r.get('summary') or ''} {it.get('url') or ''}")
            lines.append("  - " + "、".join(f"{d['label']} {d['points']:+g}" for d in detail))
    rejected = [e for e in evaluated if e[2]]
    if rejected:
        lines += ["", "### 対象外にしたポスト(先頭10件、見落としの確認用)", ""]
        for it, r, reject, *_ in rejected[:10]:
            body = (it.get("text") or "").replace("\n", " ")[:40]
            lines.append(f"- {reject} / {body}… {it.get('url') or ''}")
    return lines
