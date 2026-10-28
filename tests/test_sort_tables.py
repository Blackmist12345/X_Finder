from scripts import sort_tables


class MovingNotion:
    """表を移すと、移した先のいちばん下に付く Notion のまね。"""

    def __init__(self, pages):
        self.pages, self.archived = pages, []
        self.names = {i: n for rows in pages.values() for i, n in rows}

    def child_databases(self, page_id):
        return list(self.pages.get(page_id, []))

    def create_child_page(self, parent, title):
        self.pages["temp"] = []
        return "temp"

    def move_database(self, db_id, parent):
        for rows in self.pages.values():
            rows[:] = [r for r in rows if r[0] != db_id]
        self.pages[parent].append((db_id, self.names[db_id]))

    def archive_page(self, page_id):
        self.archived.append(page_id)


def test_sorts_by_number(d1, monkeypatch):
    d1.set_meta(sort_tables.TABLE_IDS_KEY, {"keywords": "k1", "search_excludes": "k2", "points": "k3", "basic": "k4",
                                            "periods": "k5", "emojis": "k6", "events": "k7"})
    d1.set_meta(sort_tables.OLD_TABLES_KEY, {"penalty_words": "o91", "excluded_works": "o92", "scores": "o93", "critical": "o94"})
    page = [("k1", "【1】"), ("k2", "【2】"), ("o91", "【91】"), ("o92", "【92】"), ("o93", "【93】"), ("k4", "【4】"),
            ("k5", "【5】"), ("k6", "【6】"), ("k7", "【7】"), ("k3", "【3】"), ("o94", "【94】")]
    parent = "0" * 32
    notion = MovingNotion({parent: list(page)})
    monkeypatch.setattr(sort_tables.D1, "from_env", classmethod(lambda cls: d1))
    monkeypatch.setattr(sort_tables.nt, "Notion", lambda key: notion)
    monkeypatch.setenv("NOTION_PARENT_PAGE_ID", parent)
    monkeypatch.setenv("MODE", "preview")
    sort_tables.main()
    assert notion.pages[parent] == page  # 案だけなら動かさない
    monkeypatch.setenv("MODE", "apply")
    sort_tables.main()
    assert [n for _, n in notion.pages[parent]] == ["【1】", "【2】", "【3】", "【4】", "【5】", "【6】", "【7】",
                                                     "【91】", "【92】", "【93】", "【94】"]
    assert notion.archived == ["temp"]
