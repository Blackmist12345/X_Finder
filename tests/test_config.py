import pytest

from finder import config
from finder.tables import BASIC, PERIODS, TABLES, seed_rows


def title(v):
    return {"type": "title", "title": [{"plain_text": v}]}


def rt(v):
    return {"type": "rich_text", "rich_text": [{"plain_text": v}] if v else []}


def sel(v):
    return {"type": "select", "select": {"name": v} if v else None}


def num(v):
    return {"type": "number", "number": v}


def chk(v=True):
    return {"type": "checkbox", "checkbox": v}


def row(**props):
    return {"id": "page", "properties": props}


def kv_rows(spec, **override):
    return [row(項目=title(k), 値=rt(override.get(k, v))) for k, v, _, _ in spec]


class FakeNotion:
    def __init__(self, tables):
        self.tables = tables

        self.created = []

    def create_page(self, db_id, props):
        self.created.append((db_id, props))

    def query_all(self, db_id):
        value = self.tables[db_id]
        if isinstance(value, Exception):
            raise value
        return value


def good_tables():
    return {
        "keywords": [row(単語=title("カメラマン"), グループ=sel("撮影者(A)"), 有効=chk()), row(単語=title("募集"), グループ=sel("募集語(B)"), 有効=chk()),
                     row(単語=title("無効語"), グループ=sel("募集語(B)"), 有効=chk(False))],
        "search_excludes": [row(単語=title("時給"), カテゴリ=sel("求人・バイト"), 有効=chk(), **{"観測スコア(件/日)": num(3)})],
        "points": [row(条件=title("コスプレ撮影"), 区分=sel("種類"), 点数=num(50), 有効=chk()),
                   row(条件=title("大阪"), 区分=sel("語"), カテゴリ=sel("地方・都市名"), 点数=num(-30), 有効=chk()),
                   row(条件=title("栄"), 区分=sel("語"), 点数=num(None), 有効=chk()),
                   row(条件=title("呪術廻戦, 呪術"), 区分=sel("作品"), 点数=num(-100), 有効=chk()),
                   row(条件=title("ブルーアーカイブ、ブルアカ"), 区分=sel("作品"), 点数=num(300), 有効=chk()),
                   row(条件=title("なぞ"), 区分=sel("なにか"), 点数=num(5), 有効=chk()),
                   row(条件=title("無効"), 区分=sel("語"), 点数=num(5), 有効=chk(False))],
        "basic": kv_rows(BASIC),
        "periods": kv_rows(PERIODS),
        "emojis": [row(絵文字=title("🐑"), キャラ名=rt("ひつじ太郎"), グループ=rt("にじさんじ"), 有効=chk()),
                   row(絵文字=title("🌸"), キャラ名=rt(""), 有効=chk()), row(絵文字=title("🍙"), キャラ名=rt("おにぎり"), 有効=chk(False))],
        "events": [row(略称=title("コミケ、夏コミ, C107"), 正式名=rt("コミックマーケット"), 会場=rt("東京ビッグサイト"), 都道府県=rt("東京都"), 有効=chk()),
                   row(略称=title("無効イベ"), 有効=chk(False))],
    }


def setup(d1, tables):
    d1.set_meta(config.TABLE_IDS_KEY, {t["key"]: t["key"] for t in TABLES})
    return FakeNotion(tables)


def test_load_config_reads_all_tables(d1):
    cfg, warnings = config.load_config(setup(d1, good_tables()), d1)
    assert cfg["keywords"] == {"group_a": ["カメラマン"], "group_b": ["募集"]}
    assert cfg["words"][0] == {"word": "大阪", "category": "地方・都市名", "points": -30.0}
    assert cfg["words"][1] == {"word": "栄", "category": "その他", "points": 0}  # 点数が空欄は 0
    works = [r for r in cfg["points"] if r["kind"] == "作品"]
    assert works[0]["names"] == ["呪術廻戦", "呪術"] and works[0]["condition"] == "呪術廻戦"
    assert works[1]["points"] == 100  # +100 を超えた分は +100 にそろえる
    assert len(cfg["points"]) == 5  # 区分が分からない行・無効の行は使わない
    assert cfg["basic"]["最小フォロワー数"] == 0
    assert cfg["periods"]["自動削除までの日数"] == 14
    assert any("栄" in w for w in warnings)
    assert any("ブルーアーカイブ" in w and "100" in w for w in warnings)
    assert any("なにか" in w for w in warnings)


def test_bad_value_falls_back_to_last_good(d1):
    config.load_config(setup(d1, good_tables()), d1)
    tables = good_tables()
    tables["basic"] = kv_rows(BASIC, 最小フォロワー数="たくさん", クライアント調査="ときどき")
    cfg, warnings = config.load_config(setup(d1, tables), d1)
    assert cfg["basic"]["最小フォロワー数"] == 0
    assert cfg["basic"]["クライアント調査"] == "手動"
    assert len([w for w in warnings if "不正" in w]) == 2


def test_unreadable_table_uses_last_good_or_fails(d1):
    tables = good_tables()
    tables["keywords"] = RuntimeError("Notion が落ちている")
    with pytest.raises(config.ConfigError):
        config.load_config(setup(d1, tables), d1)
    config.load_config(setup(d1, good_tables()), d1)
    cfg, warnings = config.load_config(setup(d1, tables), d1)
    assert cfg["keywords"]["group_a"] == ["カメラマン"]
    assert any("前回の内容" in w for w in warnings)


def test_keywords_need_both_groups(d1):
    tables = good_tables()
    tables["keywords"] = tables["keywords"][:1]
    with pytest.raises(config.ConfigError):
        config.load_config(setup(d1, tables), d1)


def test_seed_rows_match_table_columns():
    for table in TABLES:
        for props in seed_rows(table):
            assert set(props) <= set(table["props"]), table["title"]


def test_missing_key_row_is_added_to_notion(d1):
    tables = good_tables()
    tables["periods"] = [r for r in tables["periods"] if r["properties"]["項目"]["title"][0]["plain_text"] != "自動削除までの日数"]
    notion = setup(d1, tables)
    cfg, warnings = config.load_config(notion, d1)
    assert cfg["periods"]["自動削除までの日数"] == 14
    assert notion.created[0][0] == "periods"
    assert any("行を足しました" in w for w in warnings)


def test_emojis_table_is_parsed(d1):
    cfg, _ = config.load_config(setup(d1, good_tables()), d1)
    assert cfg["emojis"] == [{"emoji": "🐑", "name": "ひつじ太郎", "group": "にじさんじ"}]  # キャラ名が空・無効の行は使わない


def test_emojis_missing_table_id(d1):
    d1.set_meta(config.TABLE_IDS_KEY, {t["key"]: t["key"] for t in TABLES if t["key"] != "emojis"})
    cfg, warnings = config.load_config(FakeNotion(good_tables()), d1)
    assert cfg["emojis"] == [] and not any("絵文字" in w for w in warnings)


def test_emoji_seed_file():
    table = next(t for t in TABLES if t["key"] == "emojis")
    rows = seed_rows(table)
    assert len(rows) > 200
    first = rows[0]
    assert first["絵文字"]["title"][0]["text"]["content"] == "🐰"
    assert first["キャラ名"]["rich_text"][0]["text"]["content"] == "月ノ美兎"


def test_events_table_is_parsed(d1):
    cfg, _ = config.load_config(setup(d1, good_tables()), d1)
    assert cfg["events"] == [{"aliases": ["コミケ", "夏コミ", "C107"], "name": "コミックマーケット", "venue": "東京ビッグサイト", "prefecture": "東京都"}]


def test_event_seed_file():
    rows = seed_rows(next(t for t in TABLES if t["key"] == "events"))
    assert rows and all(r["略称"]["title"][0]["text"]["content"] for r in rows)
