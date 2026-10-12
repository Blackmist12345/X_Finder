"""Notion 8表の定義(列と初期値)。初期設定スクリプトと N1 設定読込が共有する。"""
from finder.notion import p_check, p_number, p_select, p_text, p_title

CATEGORIES = [
    ("地方・都市名", "orange"), ("サロン・他撮影", "purple"), ("求人・バイト", "yellow"),
    ("物販・譲渡", "brown"), ("配信・ゲーム", "gray"), ("ネガティブ", "red"),
    ("アイドル・夜職", "pink"), ("イベント", "blue"), ("その他", "default"),
]
_cat = {"select": {"options": [{"name": n, "color": c} for n, c in CATEGORIES]}}

TYPES = ["コスプレ撮影", "コスパフォ", "踊ってみた", "ポートレート", "イベント", "その他"]

# key は D1 の meta に保存する ID の名前。順番が Notion 上の並び順になる。
TABLES = [
    {
        "key": "keywords", "title": "【1】検索キーワード",
        "props": {
            "単語": {"title": {}},
            "グループ": {"select": {"options": [{"name": "撮影者(A)", "color": "blue"}, {"name": "募集語(B)", "color": "green"}]}},
            "有効": {"checkbox": {}},
            "備考": {"rich_text": {}},
        },
    },
    {
        "key": "search_excludes", "title": "【2】検索除外語",
        "props": {
            "単語": {"title": {}},
            "カテゴリ": _cat,
            "有効": {"checkbox": {}},
            "観測スコア(件/日)": {"number": {"format": "number"}},
            "備考": {"rich_text": {}},
        },
    },
    {
        "key": "penalty_words", "title": "【3】減点語",
        "props": {
            "単語": {"title": {}},
            "カテゴリ": _cat,
            "減点": {"number": {"format": "number"}},
            "有効": {"checkbox": {}},
            "備考": {"rich_text": {}},
        },
    },
    {
        "key": "excluded_works", "title": "【4】除外作品",
        "props": {
            "作品名 / 略称": {"title": {}},
            "有効": {"checkbox": {}},
            "備考": {"rich_text": {}},
        },
    },
    {
        "key": "scores", "title": "【5】点数表",
        "props": {
            "条件": {"title": {}},
            "区分": {"select": {"options": [{"name": "種類", "color": "blue"}, {"name": "都道府県", "color": "orange"}, {"name": "その他", "color": "default"}]}},
            "点数": {"number": {"format": "number"}},
            "有効": {"checkbox": {}},
            "説明": {"rich_text": {}},
        },
        "seed": (
            [(t, "種類", {"コスプレ撮影": 50, "ポートレート": 30}.get(t, 0), "仮の値") for t in TYPES]
            + [
                ("東京都", "都道府県", 0, "仮の値"),
                ("神奈川県", "都道府県", -5, "仮の値"),
                ("埼玉県", "都道府県", -5, "仮の値"),
                ("千葉県", "都道府県", -5, "仮の値"),
                ("その他の道府県", "都道府県", -30, "上の行にない道府県すべて。仮の値"),
                ("場所不明", "都道府県", 0, "開催場所が分からない案件。点数は変えず「場所不明」と表示"),
            ]
        ),
        "seed_props": lambda r: {"条件": p_title(r[0]), "区分": p_select(r[1]), "点数": p_number(r[2]), "有効": p_check(True), "説明": p_text(r[3])},
    },
    {
        "key": "critical", "title": "【6】クリティカル条件",
        "props": {
            "名前": {"title": {}},
            "種類": {"select": {"options": [{"name": "基準点", "color": "red"}, {"name": "条件", "color": "purple"}]}},
            "値": {"rich_text": {}},
            "有効": {"checkbox": {}},
            "説明": {"rich_text": {}},
        },
        "seed": [
            ("クリティカルの基準点", "基準点", "70", True, "合計点がこれ以上ならクリティカル。仮の値"),
            ("例: 特定の作品", "条件", "", False, "値に書いた語が本文・作品名・場所に含まれていれば、点数に関係なくクリティカルにする"),
        ],
        "seed_props": lambda r: {"名前": p_title(r[0]), "種類": p_select(r[1]), "値": p_text(r[2]), "有効": p_check(r[3]), "説明": p_text(r[4])},
    },
    {
        "key": "basic", "title": "【7】基本設定",
        "props": {"項目": {"title": {}}, "値": {"rich_text": {}}, "説明": {"rich_text": {}}},
        "seed": "BASIC",
    },
    {
        "key": "periods", "title": "【8】期間と頻度",
        "props": {"項目": {"title": {}}, "値": {"rich_text": {}}, "説明": {"rich_text": {}}},
        "seed": "PERIODS",
    },
]

# 1つの値だけの設定。(項目名, 初期値, 種類, 説明)。種類は int / choice:A/B / text。
BASIC = [
    ("コスプレに絞る", "オフ", "choice:オン/オフ", "オンにするとコスプレ以外の募集を対象外にする"),
    ("最小フォロワー数", "0", "int", "クライアントのフォロワー数がこれ未満なら対象外"),
    ("判定に使うAI", "gemini-3.5-flash-lite", "text", "N4 で使う AI。比較テストの結果で決める"),
    ("まとめて判定する件数", "20", "int", "1回の AI 呼び出しで判定するポスト数"),
    ("調査する最低点", "50", "int", "仮の点数がこれ以上の案件だけ、参加者の実在確認とクライアント調査を行う。仮の値"),
    ("クライアント調査", "手動", "choice:手動/自動", "手動はファインダーのボタンで調べる。自動は点数の高い案件を自動で調べる"),
    ("月の費用の目標(円)", "1000", "int", "近づいたら知らせ、高い処理を止める"),
    ("知らせる割合(%)", "80", "int", "目標の何割に達したらメールで知らせるか"),
    ("為替レート(円/ドル)", "155", "int", "費用を円に直すときのレート"),
]
PERIODS = [
    ("削除確認の頻度(時間)", "24", "int", "削除されたポストを確認する間隔"),
    ("自動削除までの日数", "14", "int", "収集からこの日数が過ぎた案件を消す"),
    ("再調査しない日数", "90", "int", "前回のクライアント調査からこの日数以内の人は調べない"),
    ("台帳から消すまでの日数", "180", "int", "この期間見かけなかったアカウントを台帳から消す"),
]


def seed_rows(table):
    """初期値を Notion のプロパティ形式で返す。"""
    seed = table.get("seed")
    if seed in ("BASIC", "PERIODS"):
        items = BASIC if seed == "BASIC" else PERIODS
        return [{"項目": p_title(k), "値": p_text(v), "説明": p_text(d)} for k, v, _, d in items]
    if not seed:
        return []
    return [table["seed_props"](r) for r in seed]
