"""検索の診断(MODE=diagnose): 検索の条件を変えて1ページずつ投げ、件数を比べる。何も保存しない。

収集が0件続きのとき、どの条件で0件になるのか(時間の指定・長い検索式・除外語)を見分けるために使う。
ログにはキーの値・検索語・ポストの本文を出さず、件数・いちばん新しいポストの投稿日時・返事の要約だけを出す。
"""
import requests

from finder import collect, store

SHORT_QUERY = '"カメラマン" "募集"'


EXCLUDE_COUNTS = (5, 10, 20, 30)


def probes(query, base, since_ts, until_ts, excluded_queries=()):
    """(名前, 検索式) の一覧。base は除外語を入れる前の式。

    excluded_queries は (除外語の数, 検索式) の一覧で、時間の指定なしで投げる(何語まで入れても返るかを見る)。
    """
    window = f" since_time:{int(since_ts)} until_time:{int(until_ts)}"
    extra = [(f"⑥ 除外語{n}語入りの式・時間の指定なし", q) for n, q in excluded_queries]
    return [
        ("① 今の検索式+時間の指定(ふだんの収集と同じ)", query + window),
        ("② 除外語なしの検索式+時間の指定", base + window),
        ("③ 短い式「カメラマン 募集」+時間の指定", SHORT_QUERY + window),
        ("④ 今の検索式・時間の指定なし", query),
        ("⑤ 短い式「カメラマン 募集」・時間の指定なし", SHORT_QUERY),
    ] + extra


def probe(api_key, full_query, session):
    """1ページだけ取る。返り値は (件数, いちばん新しい投稿日時, 返事の要約, 呼び出し回数)。失敗しても例外は出さない。"""
    try:
        res = session.get(collect.URL, headers={"X-API-Key": api_key},
                          params={"query": full_query, "queryType": "Latest"}, timeout=30)
    except requests.RequestException as e:
        return 0, None, f"接続に失敗しました: {type(e).__name__}", 1
    try:
        data = res.json()
    except ValueError:
        return 0, None, f"HTTP {res.status_code}・JSON ではない返事", 1
    tweets = data.get("tweets") if isinstance(data, dict) else None
    tweets = tweets if isinstance(tweets, list) else []
    times = sorted(t for t in (store.to_iso(tw.get("createdAt")) for tw in tweets) if t)
    return len(tweets), (times[-1] if times else None), collect.describe_response(1, res.status_code, data), 1


def run(api_key, query, base, since_ts, until_ts, jst, session=None, excluded_queries=()):
    """診断を行い、まとめ欄の行と費用(ドル)を返す。"""
    if not api_key:
        return ["## 検索の診断", "", "- TWITTERAPI_KEY が設定されていません"], 0.0
    http = session or requests.Session()
    lines = ["## 検索の診断(試し実行: 何も保存していません)", "",
             f"- 時間の指定: {jst(since_ts)} 〜 {jst(until_ts)}(日本時間)", "",
             "| 条件 | 件数 | いちばん新しいポスト(UTC) | 返事 |", "|---|---|---|---|"]
    total_tweets = total_calls = 0
    for name, q in probes(query, base, since_ts, until_ts, excluded_queries):
        count, newest, note, calls = probe(api_key, q, http)
        total_tweets += count
        total_calls += calls
        lines.append(f"| {name} | {count}件 | {newest or '-'} | {note.replace('|', '/')} |")
    usd = collect.cost_usd(total_tweets, total_calls)
    lines += ["", f"- 費用: ${usd:.4f}(呼び出し {total_calls}回、返ってきたポスト {total_tweets}件)"]
    return lines, usd
