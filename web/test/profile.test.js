import test from "node:test";
import assert from "node:assert/strict";
import { parseAge, parseAreas, profileInfo } from "../src/profile.js";

test("年齢: 矢印や言葉のいろいろな書き方を読む", () => {
  const cases = [
    ["18↓", "18↓", true], ["18⬇️", "18↓", true], ["１８⤵︎です", "18↓", true], ["18🔽", "18↓", true], ["18歳未満", "18↓", true],
    ["18⤴︎", "18↑", false], ["30↑", "30↑", false], ["20⬆️", "20↑", false], ["20over", "20↑", false], ["18+", "18↑", false],
    ["20🆙", "20↑", false], ["25歳以上", "25↑", false], ["15↑", "15↑", null], ["20↓", "20↓", null],
    ["義務教育修了済み", "成人", false], ["成人済み/コスプレ", "成人", false], ["社会人レイヤー", "成人", false],
    ["高校生レイヤー", "未成年", true], ["JK2 / 東京", "未成年", true], ["未成年です", "未成年", true],
    ["20代 / 関西", "20代", false], ["23歳", "23歳", false], ["17歳", "17歳", true],
  ];
  for (const [text, label, minor] of cases) assert.deepEqual(parseAge(text), { label, minor }, text);
  assert.equal(parseAge("コスプレしてます 東京"), null);
  assert.equal(parseAge(""), null);
});

test("活動地域: 地方・都道府県・市の名前を読む", () => {
  assert.deepEqual(parseAreas("関西で活動中"), ["関西"]);
  assert.deepEqual(parseAreas("名古屋 / 東海"), ["愛知県", "東海"]);
  assert.deepEqual(parseAreas("北海道"), ["北海道"]);
  assert.deepEqual(parseAreas("福岡↔東京"), ["福岡県", "東京都"]);
  assert.deepEqual(parseAreas("東京都在住"), ["東京都"]); // 「京都」と取り違えない
  assert.deepEqual(parseAreas("京都府"), ["京都府"]);
  assert.deepEqual(parseAreas("北九州"), ["福岡県"]); // 九州を二重に取らない
  assert.deepEqual(parseAreas("近畿 都内にも行きます"), ["関西", "東京都"]);
  assert.deepEqual(parseAreas("中国語ができます"), []);
  assert.deepEqual(parseAreas(""), []);
});

test("首都圏以外の判定: 首都圏や関東が入っていれば外す", () => {
  assert.equal(profileInfo({ location: "大阪" }).areas_far, true);
  assert.equal(profileInfo({ location: "関東/関西" }).areas_far, false);
  assert.equal(profileInfo({ description: "撮影大好き" }).areas_far, false);
  assert.deepEqual(profileInfo({ name: "あいう@18↓", description: "", location: "" }).age, { label: "18↓", minor: true });
});
