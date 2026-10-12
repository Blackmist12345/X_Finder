import json
import re

import pytest

from finder import collect, judge, judge_step, run_collect
from finder.score import score
from finder.tables import BASIC
from tests.test_config import chk, good_tables, kv_rows, num, row, sel, setup, title


def ai_result(pid, **over):
    r = {"id": pid, "wants_photographer": True, "official_or_job": False, "case_type": "コスプレ撮影", "is_cosplay": True,
         "works": "", "excluded_work": "", "prefecture": "東京都", "prefecture_source": "本文", "place": "",
         "dates": "", "summary": "併せの撮影"}
    r.update(over)
    return r


class FakeResponse:
    def __init__(self, status, data):
        self.status_code, self._data, self.text = status, data, json.dumps(data, ensure_ascii=False)

    def json(self):
        return self._data


class FakeGemini:
    """プロンプトに書かれた id を読み、answers[id] を返す。skip に入れた id は 1 回目だけ返さない。

    呼ばれた URL に合わせて、Claude と Gemini それぞれの返事の形で返す。
    """

    def __init__(self, answers=None, skip=(), status=200):
        self.answers, self.skip, self.status, self.prompts = answers or {}, set(skip), status, []

    def post(self, url, headers=None, json=None, timeout=None):
        claude = "anthropic" in url
        prompt = json["messages"][0]["content"] if claude else json["contents"][0]["parts"][0]["text"]
        self.prompts.append(prompt)
        if self.status != 200:
            return FakeResponse(self.status, {"error": "bad"})
        ids = re.findall(r"### id: (\S+)", prompt)
        out = []
        for pid in ids:
            if pid in self.skip and len(ids) > 1:
                continue
            out.append(self.answers.get(pid, ai_result(pid)))
        if claude:
            return FakeResponse(200, {"content": [{"type": "tool_use", "name": "submit_results", "input": {"results": out}}],
                                      "usage": {"input_tokens": 1000, "output_tokens": 250}})
        return FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": _dumps(out)}]}}],
                                  "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 200, "thoughtsTokenCount": 50}})


def _dumps(x):
    return json.dumps(x, ensure_ascii=False)


def config():
    cfg = {
        "scores": [
            {"condition": "コスプレ撮影", "kind": "種類", "points": 50.0},
            {"condition": "ポートレート", "kind": "種類", "points": 30.0},
            {"condition": "東京都", "kind": "都道府県", "points": 0.0},
            {"condition": "神奈川県", "kind": "都道府県", "points": -5.0},
            {"condition": "その他の道府県", "kind": "都道府県", "points": -30.0},
            {"condition": "場所不明", "kind": "都道府県", "points": 0.0},
        ],
        "critical": {"threshold": 50.0, "conditions": [{"name": "推し作品", "value": "ブルアカ"}]},
        "basic": {"コスプレに絞る": "オフ", "判定に使うAI": "gemini-3.5-flash-lite", "まとめて判定する件数": 2, "為替レート(円/ドル)": 155},
        "excluded_works": ["呪術廻戦"],
    }
    return cfg


def test_judge_batches_and_asks_again_for_missing():
    items = [{"id": str(i), "text": f"募集{i}", "penalties": []} for i in range(3)]
    fake = FakeGemini(skip={"1"})
    results, t_in, t_out, calls = judge.judge(items, "key", "gemini-3.5-flash-lite", ["呪術廻戦"], 2, session=fake)
    assert set(results) == {"0", "1", "2"}
    assert calls == 3  # 2件 + 1件 + 返し忘れた 1件を聞き直し
    assert (t_in, t_out) == (3000, 750)
    assert "呪術廻戦" in fake.prompts[0]


def test_judge_error_is_raised():
    with pytest.raises(judge.JudgeError):
        judge.judge([{"id": "1", "text": "x"}], "key", "m", [], 20, session=FakeGemini(status=400))


def test_reject_reason():
    assert judge.reject_reason(ai_result("1"), False) is None
    assert judge.reject_reason(ai_result("1", wants_photographer=False), False) == "カメラマン募集ではない"
    assert judge.reject_reason(ai_result("1", official_or_job=True), False) == "公式・求人"
    assert judge.reject_reason(ai_result("1", excluded_work="呪術廻戦"), False) == "除外作品: 呪術廻戦"
    assert judge.reject_reason(ai_result("1", is_cosplay=False, case_type="ポートレート"), True) == "コスプレ以外"


def test_cost_uses_model_price():
    assert judge.cost_usd("gemini-3.5-flash-lite", 1_000_000, 1_000_000) == pytest.approx(2.80)


def test_score_type_and_prefecture():
    total, detail, critical, why = score(ai_result("1"), [], "併せ", config())
    assert total == 50 and critical and why == "50点以上"
    total, detail, _, _ = score(ai_result("1", prefecture="大阪府"), [], "", config())
    assert total == 20
    assert detail[1]["label"] == "場所: 大阪府(その他の道府県)"


def test_region_penalty_only_when_prefecture_unknown():
    penalties = [{"word": "大阪", "category": "地方・都市名", "points": -30}, {"word": "栄", "category": "その他", "points": -10}]
    known, _, _, _ = score(ai_result("1", prefecture="大阪府"), penalties, "", config())
    assert known == 50 - 30 - 10  # 地名の減点語は使わず、都道府県の点だけ
    unknown, detail, _, _ = score(ai_result("1", prefecture="不明"), penalties, "", config())
    assert unknown == 50 + 0 - 30 - 10
    assert "場所: 場所不明" in [d["label"] for d in detail]


def test_critical_condition_matches_text():
    _, _, critical, why = score(ai_result("1", case_type="ポートレート", prefecture="神奈川県"), [], "ブルアカ併せ", config())
    assert critical and why == "条件: 推し作品"


@pytest.fixture
def wired(d1, monkeypatch, tmp_path):
    tables = good_tables()
    tables["basic"] = kv_rows(BASIC, まとめて判定する件数="1")  # 1件でも判定する(待たない)
    tables["scores"] += [row(条件=title("東京都"), 区分=sel("都道府県"), 点数=num(0), 有効=chk()),
                         row(条件=title("その他の道府県"), 区分=sel("都道府県"), 点数=num(-30), 有効=chk())]
    notion = setup(d1, tables)
    monkeypatch.setattr(run_collect.D1, "from_env", classmethod(lambda cls: d1))
    monkeypatch.setattr(run_collect, "Notion", lambda key: notion)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    return d1, summary


def test_production_judges_and_scores(wired, monkeypatch, make_tweet):
    d1, summary = wired
    tweets = [make_tweet("1", text="都内スタジオでカメラマンさん募集"), make_tweet("2", text="被写体さん募集", author_id="101"),
              make_tweet("3", text="大阪でカメラマン募集", author_id="102")]
    monkeypatch.setattr(collect, "fetch", lambda *a, **k: (tweets, 1))
    fake = FakeGemini({"2": ai_result("2", wants_photographer=False), "3": ai_result("3", prefecture="大阪府")})
    real_judge = judge.judge
    monkeypatch.setattr(judge, "judge", lambda *a, **k: real_judge(*a[:5], session=fake))
    monkeypatch.setenv("MODE", "production")
    run_collect.main()

    status = {r["id"]: (r["status"], r["skip_reason"]) for r in d1.query("SELECT id, status, skip_reason FROM posts")}
    assert status == {"1": ("judged", None), "2": ("rejected", "カメラマン募集ではない"), "3": ("judged", None)}
    j = {r["post_id"]: r for r in d1.query("SELECT * FROM judgments")}
    assert j["1"]["score"] == 50 and j["3"]["score"] == 20 and j["2"]["score"] is None
    assert d1.query("SELECT COUNT(*) AS n FROM costs WHERE service = 'claude'")[0]["n"] == 1
    text = summary.read_text()
    assert "対象 2件 / 対象外 1件" in text and "今月の費用" in text

    # 2 回目: 判定済みのポストはもう判定しない
    fake.prompts.clear()
    run_collect.main()
    assert fake.prompts == []


def test_failed_judge_keeps_posts_waiting(wired, monkeypatch, make_tweet):
    d1, summary = wired
    monkeypatch.setattr(collect, "fetch", lambda *a, **k: ([make_tweet("1")], 1))
    real_judge = judge.judge
    monkeypatch.setattr(judge, "judge", lambda *a, **k: real_judge(*a[:5], session=FakeGemini(status=403)))
    monkeypatch.setenv("MODE", "production")
    run_collect.main()
    assert d1.query("SELECT status FROM posts")[0]["status"] == "new"
    assert "判定待ちのまま" in summary.read_text()


def test_dry_run_judges_without_saving(wired, monkeypatch, make_tweet):
    d1, summary = wired
    monkeypatch.setattr(collect, "fetch", lambda *a, **k: ([make_tweet("1")], 1))
    real_judge = judge.judge
    monkeypatch.setattr(judge, "judge", lambda *a, **k: real_judge(*a[:5], session=FakeGemini()))
    monkeypatch.setenv("MODE", "dry_run")
    run_collect.main()
    assert d1.query("SELECT COUNT(*) AS n FROM judgments")[0]["n"] == 0
    assert "50点" in summary.read_text()


def test_claude_call_reads_tool_result():
    fake = FakeGemini()
    results, t_in, t_out, calls = judge.judge([{"id": "1", "text": "募集"}], "key", "claude-haiku-5-5", [], 20, session=fake)
    assert results["1"]["case_type"] == "コスプレ撮影" and (t_in, t_out, calls) == (1000, 250, 1)
    assert judge.cost_usd("claude-haiku-5-5", 1_000_000, 1_000_000) == pytest.approx(0.60)


def test_claude_schema_is_lowercase_json_schema():
    schema = judge.json_schema(judge.SCHEMA)
    assert schema["type"] == "array" and schema["items"]["properties"]["case_type"]["type"] == "string"
    assert "コスプレ撮影" in schema["items"]["properties"]["case_type"]["enum"]


def test_missing_key_skips_judging(d1):
    lines = judge_step.run(d1, config(), True, {})
    assert any("GEMINI_API_KEY" in line for line in lines)
    cfg = config()
    cfg["basic"]["判定に使うAI"] = "claude-haiku-5-5"
    assert any("ANTHROPIC_API_KEY" in line for line in judge_step.run(d1, cfg, True, {}))
    cfg["basic"]["判定に使うAI"] = "gpt-6-luna"
    assert any("使えません" in line for line in judge_step.run(d1, cfg, True, {"X": "k"}))


def test_out_of_list_prefecture_becomes_unknown():
    r = judge.normalize(ai_result("1", prefecture="関東", case_type="撮影会"))
    assert (r["prefecture"], r["prefecture_source"], r["place"], r["case_type"]) == ("不明", "不明", "関東", "その他")
    assert judge.normalize(ai_result("1", prefecture="千葉県"))["prefecture"] == "千葉県"


def test_production_waits_until_batch_is_full(wired, monkeypatch, make_tweet):
    d1, summary = wired
    tables = good_tables()  # まとめて判定する件数 = 20
    monkeypatch.setattr(run_collect, "Notion", lambda key: setup(d1, tables))
    monkeypatch.setattr(collect, "fetch", lambda *a, **k: ([make_tweet("1")], 1))
    fake = FakeGemini()
    real_judge = judge.judge
    monkeypatch.setattr(judge, "judge", lambda *a, **k: real_judge(*a[:5], session=fake))
    monkeypatch.setenv("MODE", "production")
    run_collect.main()
    assert fake.prompts == []
    assert d1.query("SELECT status FROM posts")[0]["status"] == "new"
    assert "20件たまるか" in summary.read_text()


def test_should_wait():
    from datetime import datetime, timezone
    now = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
    fresh = [{"collected_at": "2026-10-12T11:30:00+00:00"}]
    old = [{"collected_at": "2026-10-12T10:30:00+00:00"}]
    assert judge_step.should_wait(fresh, 20, 60, now) is True
    assert judge_step.should_wait(old, 20, 60, now) is False
    assert judge_step.should_wait(fresh * 20, 20, 60, now) is False


def test_old_penalties_get_category_from_config():
    cfg = config()
    cfg["penalty_words"] = [{"word": "大阪", "category": "地方・都市名", "points": -30}]
    items = [{"id": "1", "text": "大阪", "penalties": [{"word": "大阪", "points": -30}]}]
    (_, _, _, total, *_), = judge_step.evaluate(items, {"1": ai_result("1", prefecture="大阪府")}, cfg)
    assert total == 20  # 地名の減点語は使われず、都道府県の -30 だけ
