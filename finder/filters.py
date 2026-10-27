"""N3 一次フィルタ(収集と保存の段階で使う部分)。

AI の前に、明らかに対象外のポストに理由を付けて除く。点数表の「語」は除かずに、当たった語を記録だけする。
"""

# 点数表の語の一部を含むが別の意味になる語。照合の前に伏せておく(例: 「東京都」の中の「京都」)。
PROTECTED_TERMS = ["東京都"]


def _mask(text):
    for term in PROTECTED_TERMS:
        text = text.replace(term, "〓" * len(term))
    return text


def find_words(text, words):
    """text に含まれる語を返す。PROTECTED_TERMS の中に現れた分は数えない。"""
    masked = _mask(text or "")
    return [w for w in words if w and w in masked]


def word_hits(text, words):
    """点数表の区分「語」のうち、本文に含まれる語。同じ語は1回だけ数える。"""
    masked = _mask(text or "")
    out, seen = [], set()
    for w in words:
        if w["word"] and w["word"] in masked and w["word"] not in seen:
            seen.add(w["word"])
            out.append({"word": w["word"], "category": w.get("category", "その他"), "points": w["points"]})
    return out


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
