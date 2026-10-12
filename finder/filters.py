"""N3 一次フィルタ(収集と保存の段階で使う部分)。

AI の前に、明らかに対象外のポストに理由を付けて除く。減点語は除かずに記録だけする。
"""

# 減点語の一部を含むが別の意味になる語。照合の前に伏せておく(例: 「東京都」の中の「京都」)。
PROTECTED_TERMS = ["東京都"]


def _mask(text):
    for term in PROTECTED_TERMS:
        text = text.replace(term, "〓" * len(term))
    return text


def find_words(text, words):
    """text に含まれる語を返す。PROTECTED_TERMS の中に現れた分は数えない。"""
    masked = _mask(text or "")
    return [w for w in words if w and w in masked]


def penalty_hits(text, penalty_words):
    masked = _mask(text or "")
    return [{"word": p["word"], "category": p.get("category", "その他"), "points": p["points"]}
            for p in penalty_words if p["word"] and p["word"] in masked]


def skip_reason(tweet, overflow_excludes, min_followers):
    """除く理由を返す。除かないなら None。"""
    if tweet.get("retweeted_tweet"):
        return "リポスト"
    author = tweet.get("author") or {}
    followers = author.get("followers") or 0
    if followers < min_followers:
        return f"フォロワー数が{min_followers}未満"
    hits = find_words(tweet.get("text", ""), overflow_excludes)
    if hits:
        return "検索除外語: " + "、".join(hits)
    return None
