from scripts import rename_old_tables
from tests.test_merge_points import RenamingNotion


def test_renames_retired_tables(d1, monkeypatch):
    d1.set_meta(rename_old_tables.OLD_TABLES_KEY, {"penalty_words": "penalty", "excluded_works": "works", "scores": "scores", "critical": "critical"})
    notion = RenamingNotion({})
    monkeypatch.setattr(rename_old_tables.D1, "from_env", classmethod(lambda cls: d1))
    monkeypatch.setattr(rename_old_tables.nt, "Notion", lambda key: notion)
    rename_old_tables.main()
    assert notion.renamed == {"penalty": "(旧)【91】減点語", "works": "(旧)【92】除外作品", "scores": "(旧)【93】点数表", "critical": "(旧)【94】クリティカル条件"}


def test_does_nothing_without_old_tables(d1, monkeypatch):
    notion = RenamingNotion({})
    monkeypatch.setattr(rename_old_tables.D1, "from_env", classmethod(lambda cls: d1))
    monkeypatch.setattr(rename_old_tables.nt, "Notion", lambda key: notion)
    rename_old_tables.main()
    assert notion.renamed == {}
