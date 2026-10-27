"""Notion の設定の表の定義(列と初期値)。初期設定スクリプトと N1 設定読込が共有する。"""
import csv
import os

from finder.notion import p_check, p_number, p_select, p_text, p_title

CATEGORIES = [
    ("地方・都市名", "orange"), ("サロン・他撮影", "purple"), ("求人・バイト", "yellow"),
    ("物販・譲渡", "brown"), ("配信・ゲーム", "gray"), ("ネガティブ", "red"),
    ("アイドル・夜職", "pink"), ("イベント", "blue"), ("その他", "default"),
]
_cat = {"select": {"options": [{"name": n, "color": c} for n, c in CATEGORIES]}}

TYPES = ["コスプレ撮影", "コスパフォ", "踊ってみた", "ポートレート", "イベント", "その他"]

# 点数表の区分。種類と都道府県は AI の読み取り、語は本文の文字、作品は AI、イベントは AI が読んだ名前とイベントの一覧で当てる。
POINT_KINDS = [("種類", "blue"), ("都道府県", "orange"), ("語", "green"), ("作品", "purple"), ("イベント", "pink")]
POINT_MIN, POINT_MAX = -100, 100
OTHER_PREFECTURES = "その他の道府県"
UNKNOWN_PLACE_ROW = "場所不明"
TYPE_POINTS = {"コスプレ撮影": 20, "ポートレート": 10}
PREFECTURE_POINTS = [("東京都", 0), ("神奈川県", -5), ("埼玉県", -5), ("千葉県", -5), (OTHER_PREFECTURES, -20), (UNKNOWN_PLACE_ROW, 0)]
POINT_SEED = (
    [(t, "種類", TYPE_POINTS.get(t, 0), "") for t in TYPES]
    + [(p, "都道府県", pts, "上の行にない道府県すべて" if p == OTHER_PREFECTURES else "") for p, pts in PREFECTURE_POINTS]
)

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
        # 点数の付け方をこの1つの表にまとめる(旧【3】減点語・【4】除外作品・【5】点数表・【6】クリティカル条件)。
        "key": "points", "title": "【3】点数表",
        "props": {
            "条件": {"title": {}},  # 作品・イベントは略称をカンマ区切りで並べてよい
            "区分": {"select": {"options": [{"name": n, "color": c} for n, c in POINT_KINDS]}},
            "点数": {"number": {"format": "number"}},
            "カテゴリ": _cat,  # 区分「語」だけ使う
            "有効": {"checkbox": {}},
            "備考": {"rich_text": {}},
        },
        "seed": POINT_SEED,
        "seed_props": lambda r: {"条件": p_title(r[0]), "区分": p_select(r[1]), "点数": p_number(r[2]), "有効": p_check(True), "備考": p_text(r[3])},
    },
    {
        "key": "basic", "title": "【4】基本設定",
        "props": {"項目": {"title": {}}, "値": {"rich_text": {}}, "説明": {"rich_text": {}}},
        "seed": "BASIC",
    },
    {
        "key": "periods", "title": "【5】期間と頻度",
        "props": {"項目": {"title": {}}, "値": {"rich_text": {}}, "説明": {"rich_text": {}}},
        "seed": "PERIODS",
    },
    {
        # VTuber などは、キャラ名の代わりに決まった絵文字(推しマーク)で書かれることがある。判定のときに読み替える。
        "key": "emojis", "title": "【6】絵文字の一覧",
        "optional": True,  # あとから足した表。まだ作っていなくても設定は読める
        "props": {
            "絵文字": {"title": {}},
            "キャラ名": {"rich_text": {}},
            "グループ": {"rich_text": {}},
            "有効": {"checkbox": {}},
            "備考": {"rich_text": {}},
        },
        "seed": "EMOJIS",
    },
    {
        # イベントは略称で書かれることが多い。判定のときに、略称から開催地を補う。
        "key": "events", "title": "【7】イベントの一覧",
        "optional": True,
        "props": {
            "略称": {"title": {}},  # 何通りもあるときはカンマ区切り
            "正式名": {"rich_text": {}},
            "会場": {"rich_text": {}},
            "都道府県": {"rich_text": {}},  # 回ごとに変わるイベントは「毎回ちがう」
            "有効": {"checkbox": {}},
            "備考": {"rich_text": {}},
        },
        "seed": "EVENTS",
    },
]

# 1つの値だけの設定。(項目名, 初期値, 種類, 説明)。種類は int / float / choice:A/B / text。
BASIC = [
    ("コスプレに絞る", "オフ", "choice:オン/オフ", "オンにするとコスプレ以外の募集を対象外にする"),
    ("最小フォロワー数", "0", "int", "クライアントのフォロワー数がこれ未満なら対象外"),
    ("FF率の倍率の下限", "0.5", "float", "点数に依頼主の FF率(フォロワー数÷フォロー数)を掛けるときの、いちばん小さい倍率。0 で下限なし"),
    ("FF率の倍率の上限", "1.5", "float", "点数に FF率を掛けるときの、いちばん大きい倍率。0 で上限なし。下限と上限を両方 1 にすると掛けない"),
    ("判定に使うAI", "claude-haiku-5-5", "text", "N4 で使う AI。claude- で始まれば Claude、gemini- で始まれば Gemini を使う"),
    ("まとめて判定する件数", "20", "int", "1回の AI 呼び出しで判定するポスト数"),
    ("調査する最低点", "50", "int", "仮の点数がこれ以上の案件だけ、参加者の実在確認とクライアント調査を行う。仮の値"),
    ("クライアント調査", "手動", "choice:手動/自動", "手動はX Finderのボタンで調べる。自動は点数の高い案件を自動で調べる"),
    ("月の費用の目標(円)", "1000", "int", "近づいたら知らせ、高い処理を止める"),
    ("知らせる割合(%)", "80", "int", "目標の何割に達したらメールで知らせるか"),
    ("為替レート(円/ドル)", "155", "int", "費用を円に直すときのレート"),
    ("休みとみなす予定の言葉", "休み,有給,休日,公休,代休,振休", "text",
     "Google カレンダーの予定名にこの言葉(カンマ区切り)が入っていれば、予定ありに数えず「休み」として扱う"),
]
PERIODS = [
    ("判定を待つ最長時間(分)", "60", "int", "判定待ちが「まとめて判定する件数」に届かなくても、いちばん古いポストがこの時間待ったら判定する"),
    ("削除確認の頻度(時間)", "4", "int", "新着・確認中・保留のカードの元のポストが消えていないかを確かめる間隔。消えていたら削除予定へ移す。0 で確かめない"),
    ("自動削除までの日数", "14", "int", "収集からこの日数が過ぎた案件を消す"),
    ("再調査しない日数", "90", "int", "前回のクライアント調査からこの日数以内の人は調べない"),
    ("台帳から消すまでの日数", "180", "int", "この期間見かけなかったアカウントを台帳から消す"),
]


DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
EMOJI_SEED_FILE = os.path.join(DATA_DIR, "emojis.tsv")
EVENT_SEED_FILE = os.path.join(DATA_DIR, "events.tsv")
VARIES = "毎回ちがう"


def _read_tsv(path, columns):
    """タブ区切りのファイルを読む(# で始まる行は飛ばす)。1・2列目が空の行は使わない。"""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        rows = [r for r in csv.reader(f, delimiter="\t") if r and not r[0].startswith("#")]
    rows = [r + [""] * (columns - len(r)) for r in rows]
    return [tuple(c.strip() for c in r[:columns]) for r in rows if r[0].strip() and r[1].strip()]


def emoji_seed(path=EMOJI_SEED_FILE):
    """data/emojis.tsv(絵文字・キャラ名・グループ・説明)を読む。"""
    return _read_tsv(path, 4)


def event_seed(path=EVENT_SEED_FILE):
    """data/events.tsv(略称・正式名・会場・都道府県・備考)を読む。"""
    return _read_tsv(path, 5)


def seed_rows(table):
    """初期値を Notion のプロパティ形式で返す。"""
    seed = table.get("seed")
    if seed in ("BASIC", "PERIODS"):
        items = BASIC if seed == "BASIC" else PERIODS
        return [{"項目": p_title(k), "値": p_text(v), "説明": p_text(d)} for k, v, _, d in items]
    if seed == "EMOJIS":
        return [{"絵文字": p_title(e), "キャラ名": p_text(n), "グループ": p_text(g), "有効": p_check(True), "備考": p_text(d)}
                for e, n, g, d in emoji_seed()]
    if seed == "EVENTS":
        return [{"略称": p_title(a), "正式名": p_text(n), "会場": p_text(v), "都道府県": p_text(pref), "有効": p_check(True), "備考": p_text(d)}
                for a, n, v, pref, d in event_seed()]
    if not seed:
        return []
    return [table["seed_props"](r) for r in seed]
