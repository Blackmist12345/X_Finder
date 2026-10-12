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
        "penalty_words": [row(単語=title("大阪"), カテゴリ=sel("地方・都市名"), 減点=num(30), 有効=chk()), row(単語=title("栄"), 減点=num(None), 有効=chk())],
        "excluded_works": [row(**{"作品名 / 略称": title("呪術廻戦"), "有効": chk()})],
        "scores": [row(条件=title("コスプレ撮影"), 区分=sel("種類"), 点数=num(50), 有効=chk())],
        "critical": [row(名前=title("基準点"), 種類=sel("基準点"), 値=rt("70"), 有効=chk())],
        "basic": kv_rows(BASIC),
        "periods": kv_rows(PERIODS),
    }


def setup(d1, tables):
    d1.set_meta(config.TABLE_IDS_KEY, {t["key"]: t["key"] for t in TABLES})
    return FakeNotion(tables)


def test_load_config_reads_all_tables(d1):
    cfg, warnings = config.load_config(setup(d1, good_tables()), d1)
    assert cfg["keywords"] == {"group_a": ["カメラマン"], "group_b": ["募集"]}
    assert cfg["penalty_words"][0] == {"word": "大阪", "category": "地方・都市名", "points": -30.0}
    assert cfg["penalty_words"][1]["points"] == -10
    assert cfg["critical"]["threshold"] == 70
    assert cfg["basic"]["最小フォロワー数"] == 0
    assert cfg["periods"]["自動削除までの日数"] == 14
    assert any("栄" in w for w in warnings)


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
