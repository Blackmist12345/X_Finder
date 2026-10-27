from scripts import merge_points
from tests.test_config import FakeNotion, chk, num, row, rt, sel, title


class RenamingNotion(FakeNotion):
    def __init__(self, tables):
        super().__init__(tables)
        self.renamed, self.dbs = {}, []

    def create_database(self, parent, title, props):
        self.dbs.append(title)
        return "new-points"

    def rename_database(self, db_id, title):
        self.renamed[db_id] = title


def old_tables():
    return {
        "penalty": [row(単語=title("大阪"), カテゴリ=sel("地方・都市名"), 減点=num(30), 有効=chk()),
                    row(単語=title("名古屋"), カテゴリ=sel("地方・都市名"), 減点=num(30), 有効=chk()),
                    row(単語=title("栄"), 減点=num(5), 有効=chk()), row(単語=title("時給"), 減点=num(30), 有効=chk()),
                    row(単語=title("無効"), 減点=num(30), 有効=chk(False))],
        "works": [row(**{"作品名 / 略称": title("呪術廻戦"), "有効": chk()})],
        "scores": [], "critical": [row(名前=title("基準点"), 種類=sel("基準点"), 値=rt("70"), 有効=chk())],
    }


IDS = {"keywords": "kw", "search_excludes": "se", "penalty_words": "penalty", "excluded_works": "works", "scores": "scores",
       "critical": "critical", "basic": "basic", "periods": "periods", "emojis": "emojis", "events": "events"}


def test_plan_rewrites_points_and_drops_prefectures():
    rows, dropped = merge_points.plan(FakeNotion(old_tables()), IDS)
    got = {(c, k): p for c, k, p, _, _ in rows}
    assert dropped == ["大阪"]  # 都道府県の名前は都道府県の行と重なるので外す
    assert got[("名古屋", "語")] == -20 and got[("栄", "語")] == -5 and got[("時給", "語")] == -10
    assert got[("呪術廻戦", "作品")] == -30
    assert got[("コスプレ撮影", "種類")] == 20 and got[("その他の道府県", "都道府県")] == -20
    assert ("無効", "語") not in got


def test_apply_creates_table_and_renumbers(d1, monkeypatch):
    d1.set_meta(merge_points.TABLE_IDS_KEY, dict(IDS))
    notion = RenamingNotion(old_tables())
    monkeypatch.setattr(merge_points.D1, "from_env", classmethod(lambda cls: d1))
    monkeypatch.setattr(merge_points.nt, "Notion", lambda key: notion)
    monkeypatch.setattr(merge_points, "notion_parent", lambda: "parent")
    monkeypatch.setenv("MODE", "apply")
    merge_points.main()
    ids = d1.get_meta(merge_points.TABLE_IDS_KEY)
    assert ids["points"] == "new-points" and "penalty_words" not in ids
    assert d1.get_meta(merge_points.OLD_TABLES_KEY)["excluded_works"] == "works"
    assert notion.dbs == ["【3】点数表"]
    assert notion.renamed["basic"] == "【4】基本設定" and notion.renamed["events"] == "【7】イベントの一覧"
    assert notion.renamed["penalty"] == "(旧)【3】減点語"
    assert len([c for c in notion.created if c[0] == "new-points"]) > 10
    merge_points.main()  # 2 回目は何もしない
    assert notion.dbs == ["【3】点数表"]
