import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";

import { resetCertsCache, verifyAccess } from "../src/auth.js";
import worker, { changeBlacklist, listTables, readTable, getCase, getSettings, getStats, getSummary, listBlacklist, listCases, saveSettings, setReview, setReviews } from "../src/index.js";
import { fakeD1 } from "./fake-d1.js";

const TEAM = "example.cloudflareaccess.com";
const AUD = "aud-tag-123";

// ---- テスト用の Access の鍵とトークン ----
const keyPair = await crypto.subtle.generateKey(
  { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
  true, ["sign", "verify"]);
const publicJwk = { ...(await crypto.subtle.exportKey("jwk", keyPair.publicKey)), kid: "k1" };
const b64url = (bytes) => Buffer.from(bytes).toString("base64url");

async function makeToken(overrides = {}, kid = "k1") {
  const header = b64url(JSON.stringify({ alg: "RS256", kid }));
  const payload = b64url(JSON.stringify({
    aud: [AUD], iss: `https://${TEAM}`, email: "me@example.com", exp: Math.floor(Date.now() / 1000) + 600, ...overrides,
  }));
  const sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", keyPair.privateKey, new TextEncoder().encode(`${header}.${payload}`));
  return `${header}.${payload}.${b64url(new Uint8Array(sig))}`;
}

const certsFetch = async (url) => {
  assert.equal(url, `https://${TEAM}/cdn-cgi/access/certs`);
  return new Response(JSON.stringify({ keys: [publicJwk] }));
};

function req(path, token) {
  return new Request(`https://x-finder.example.workers.dev${path}`, { headers: token ? { "cf-access-jwt-assertion": token } : {} });
}

// ---- テスト用の D1 の中身 ----
function seed(db) {
  const now = new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00");
  const old = new Date(Date.now() - 30 * 864e5).toISOString().replace(/\.\d{3}Z$/, "+00:00");
  const run = (sql, ...a) => db.raw.prepare(sql).run(...a);
  run(`INSERT INTO users (id, username, name, followers, following, profile_image, status, first_seen_at, last_seen_at)
       VALUES ('100', 'client', 'クライアント', 500, 100, 'https://pbs.twimg.com/a.jpg', 'confirmed', ?, ?)`, now, now);
  run(`INSERT INTO users (id, username, name, status, first_seen_at, last_seen_at) VALUES ('300', 'friend', '友達', 'confirmed', ?, ?)`, now, now);
  const post = (id, status, collected, text = "カメラマン募集") =>
    run(`INSERT INTO posts (id, author_id, source, text, url, created_at, media_json, status, collected_at)
         VALUES (?, '100', 'search', ?, ?, ?, '["https://pbs.twimg.com/m.jpg"]', ?, ?)`, id, text, `https://x.com/client/status/${id}`, collected, status, collected);
  const judged = (id, score, critical, pref, reject = null) =>
    run(`INSERT INTO judgments (post_id, judged_at, case_type, prefecture, summary, score, score_json, critical, reject_reason)
         VALUES (?, ?, 'コスプレ撮影', ?, '併せ撮影', ?, '[{"label":"種類: コスプレ撮影","points":50},{"label":"作品: 呪術廻戦","points":-100}]', ?, ?)`, id, now, pref, score, critical, reject);
  post("1", "judged", now); judged("1", 50, 0, "東京都");
  post("2", "judged", now); judged("2", 80, 1, "大阪府");
  post("3", "rejected", now); judged("3", null, 0, "不明", "カメラマン募集ではない");
  post("4", "judged", old); judged("4", 90, 1, "東京都");
  post("5", "new", now);
  run(`INSERT INTO post_users (post_id, user_id, role) VALUES ('1', '300', 'mention'), ('1', '100', 'client')`);
  run(`INSERT INTO costs (at, service, units, usd) VALUES (?, 'claude', 1, 1.0), (?, 'twitterapi.io', 1, 0.5)`, now, old);
  run(`INSERT INTO meta (key, value) VALUES ('config_last_good', ?)`, JSON.stringify({ basic: { "為替レート(円/ドル)": 150, "月の費用の目標(円)": 1200 } }));
  run(`INSERT INTO runs (started_at, finished_at, status) VALUES (?, ?, 'ok')`, now, now);
}

let db;
beforeEach(() => {
  resetCertsCache();
  db = fakeD1();
  seed(db);
});

test("一覧は対象の案件を新しい順に、直近の分だけ返す(点数順に並べるのは画面)", async () => {
  const cases = await listCases(db, new URLSearchParams());
  assert.deepEqual(cases.map((c) => c.id), ["2", "1"]);
  assert.equal(cases[0].critical, undefined); // クリティカルは点数に一本化したので返さない
  assert.deepEqual(cases[0].work_points, [{ name: "呪術廻戦", points: -100 }]);
  assert.equal(cases[1].shoot_kanto, true);
  assert.equal(cases[1].shoot_metro, true);
  assert.equal(cases[1].shoot_pref, "東京都");
  assert.equal(cases[1].others, 1);
  assert.equal(cases[1].username, "client");
});

test("一覧の枠は新しい案件から埋め、点の高い古い案件や削除予定で新しい案件が押し出されない", async () => {
  const run = (sql, ...a) => db.raw.prepare(sql).run(...a);
  const at = (min) => new Date(Date.now() - min * 6e4).toISOString().replace(/\.\d{3}Z$/, "+00:00");
  for (let i = 0; i < 600; i++) {
    const id = `old${i}`;
    run(`INSERT INTO posts (id, author_id, source, text, url, created_at, status, collected_at) VALUES (?, '100', 'search', 't', 'u', ?, 'judged', ?)`, id, at(600 + i), at(600 + i));
    run(`INSERT INTO judgments (post_id, judged_at, case_type, score) VALUES (?, ?, 'コスプレ撮影', 90)`, id, at(600 + i));
    if (i < 280) run(`INSERT INTO reviews (post_id, state, updated_at) VALUES (?, 'delete', ?)`, id, at(1));
  }
  const cases = await listCases(db, new URLSearchParams());
  assert.equal(cases.length, 300);
  assert.ok(cases.some((c) => c.id === "1") && cases.some((c) => c.id === "2")); // 点の低い新しい案件も入る
  assert.ok(cases.every((c) => c.review !== "delete"));
  assert.equal((await listCases(db, new URLSearchParams("group=deleted"))).length, 280);
});

test("撮影場所は依頼主のプロフィールから推定した都道府県を数えない(活動地域とは別)", async () => {
  db.raw.prepare("UPDATE judgments SET prefecture_source = 'プロフィール' WHERE post_id = '1'").run();
  const c = (await listCases(db, new URLSearchParams())).find((x) => x.id === "1");
  assert.equal(c.shoot_kanto, false);
  assert.equal(c.shoot_far, false);
});

test("プロフィールのリンク先から読んだ年齢・活動地域・性別を出す", async () => {
  const found = JSON.stringify({ age: { label: "成人", minor: false }, areas: ["福岡県"], gender: "女性" });
  db.raw.prepare(`INSERT INTO user_links (user_id, urls_json, updated_at, found_json) VALUES ('100', '["https://lit.link/a"]', 'x', ?)`).run(found);
  const c = (await listCases(db, new URLSearchParams()))[0];
  assert.deepEqual(c.areas, ["福岡県"]);
  assert.equal(c.areas_from, "link");
  assert.equal(c.age.label, "成人");
  assert.equal(c.gender, "女性");
  assert.equal(c.link_found, undefined);
  const d = await getCase(db, "1");
  assert.equal(d.age_from, "link");
  assert.equal(d.link_found, undefined);
});

test("カードの状態: 開いても新着のまま(青い点だけ消える)、受注中・保留は古くても一覧に残る", async () => {
  let cases = await listCases(db, new URLSearchParams());
  assert.deepEqual(cases.map((c) => [c.review, c.opened]), [["new", false], ["new", false]]);
  assert.equal(await setReview(db, "1", { opened: true }), "new");
  assert.equal(await setReview(db, "2", { state: "delete" }), "delete");
  assert.equal(await setReview(db, "2", { opened: true }), "delete"); // 開いても削除予定のまま
  assert.equal(await setReview(db, "4", { state: "accepted" }), "accepted"); // 30 日前の案件
  cases = await listCases(db, new URLSearchParams());
  assert.deepEqual(cases.map((c) => [c.id, c.review, c.opened]), [["4", "accepted", false], ["1", "new", true]]); // 削除予定は別に取る
  assert.deepEqual((await listCases(db, new URLSearchParams("group=deleted"))).map((c) => [c.id, c.review, c.opened]), [["2", "delete", true]]);
  assert.equal(await setReview(db, "4", { state: "hold" }), "hold");
  assert.equal((await getCase(db, "4")).review, "hold");
  assert.equal(await setReview(db, "999", { state: "hold" }), null);
  assert.equal(await setReview(db, "2", { state: "new" }), "new"); // 外すと新着に戻り、開いた記録は残る
  assert.equal((await getCase(db, "2")).opened, true);
  assert.equal(await setReview(db, "4", { state: "seen" }), "new"); // 前の名前で届いても新着
  await assert.rejects(setReview(db, "1", { state: "bad" }), RangeError);
});

test("確認中: 自分で付けたときだけ。開いても変わらず、2週間を過ぎたら新着と同じく出ない", async () => {
  assert.equal(await setReview(db, "1", { opened: true }), "new"); // 開いただけでは新着のまま
  assert.equal(await setReview(db, "1", { state: "checking" }), "checking");
  assert.equal(await setReview(db, "1", { opened: true }), "checking");
  assert.equal((await getCase(db, "1")).review, "checking");
  assert.equal(await setReview(db, "4", { state: "checking" }), "checking"); // 30 日前の案件
  const ids = (await listCases(db, new URLSearchParams())).map((c) => [c.id, c.review]);
  assert.ok(ids.some(([id, r]) => id === "1" && r === "checking"));
  assert.ok(!ids.some(([id]) => id === "4"));
  assert.equal((await saveSettings(db, { swipe_right: "checking" })).swipe_right, "checking");
});

test("削除ポスト確認で見られなくなったカードには印(gone_at)が付く", async () => {
  db.raw.prepare("INSERT INTO post_checks (post_id, checked_at, gone_at) VALUES ('1', 't', 't')").run();
  const cases = await listCases(db, new URLSearchParams());
  assert.equal(cases.find((c) => c.id === "1").gone_at, "t");
  assert.ok(cases.filter((c) => c.id !== "1").every((c) => c.gone_at === null));
  assert.equal((await getCase(db, "1")).gone_at, "t");
});

test("スワイプは状態タブごと: 前からの設定はどのタブも同じ、そのタブの状態は新着に戻すになる", async () => {
  const first = await getSettings(db);
  assert.deepEqual(first.swipe_tabs.new, { right: "hold", left: "delete" });
  assert.deepEqual(first.swipe_tabs.hold, { right: "new", left: "delete" });
  assert.deepEqual(first.swipe_tabs.delete, { right: "hold", left: "new" });
  const s = await saveSettings(db, { swipe_tabs: { checking: { right: "accepted", left: "checking" }, new: { left: "new" } } });
  assert.deepEqual(s.swipe_tabs.checking, { right: "accepted", left: "new" });
  assert.deepEqual(s.swipe_tabs.new, { right: "hold", left: "none" });
  assert.deepEqual((await getSettings(db)).swipe_tabs.checking, { right: "accepted", left: "new" });
  await assert.rejects(saveSettings(db, { swipe_tabs: { bad: { right: "hold" } } }), RangeError);
  await assert.rejects(saveSettings(db, { swipe_tabs: { new: { right: "zzz" } } }), RangeError);
});

test("前の「確認済み」の行は新着(開いた)として出る", async () => {
  db.raw.prepare(`INSERT INTO reviews (post_id, state, opened_at, updated_at) VALUES ('1', 'seen', 'x', 'x')`).run();
  const c = (await listCases(db, new URLSearchParams())).find((x) => x.id === "1");
  assert.deepEqual([c.review, c.opened], ["new", true]);
});

test("サマリー: 日ごとの数・累計・今月と先月の比べ・種類の内訳", async () => {
  const s = await getStats(db, new URLSearchParams("days=7"));
  assert.equal(s.days, 7);
  assert.equal(s.daily.length, 7);
  const today = s.daily.at(-1);
  assert.equal(today.judged, 4); // seed の judged_at はすべて今
  assert.equal(today.target, 3);
  assert.equal(today.ai_yen, 150); // claude $1 × 150円
  assert.equal(s.totals.collected, 5);
  assert.equal(s.this_month.target, 3);
  assert.deepEqual(s.types, [{ k: "コスプレ撮影", n: 3 }]);
  assert.equal((await getStats(db, new URLSearchParams("days=5"))).days, 30); // 決まった期間以外は 30 日
});

test("ブラックリスト: 載せた人の案件は一覧に出ず、判定待ちのポストは判定に回さない。解除すると戻る", async () => {
  assert.deepEqual(await changeBlacklist(db, { user_id: "100", reason: "spam", note: "  同じ文面を連投  " }), { user_id: "100", blacklisted: true });
  assert.deepEqual(await listCases(db, new URLSearchParams()), []);
  assert.deepEqual(await listCases(db, new URLSearchParams("view=rejected")), []);
  const p5 = db.raw.prepare("SELECT status, skip_reason FROM posts WHERE id = '5'").get();
  assert.deepEqual({ ...p5 }, { status: "skipped", skip_reason: "ブラックリスト" });
  assert.equal((await getCase(db, "1")).blacklist.reason, "spam");
  const list = await listBlacklist(db);
  assert.equal(list.length, 1);
  assert.equal(list[0].username, "client");
  assert.equal(list[0].note, "同じ文面を連投");
  assert.equal(list[0].posts, 5);
  assert.equal(await changeBlacklist(db, { user_id: "999", reason: "spam" }), null); // 台帳にいない人
  await assert.rejects(changeBlacklist(db, { user_id: "100", reason: "bad" }), RangeError);
  await assert.rejects(changeBlacklist(db, { user_id: "abc", reason: "spam" }), RangeError);
  assert.deepEqual(await changeBlacklist(db, { user_id: "100", remove: true }), { user_id: "100", blacklisted: false });
  assert.deepEqual((await listCases(db, new URLSearchParams())).map((c) => c.id), ["2", "1"]);
  assert.equal((await getCase(db, "1")).blacklist, null);
  assert.equal(db.raw.prepare("SELECT status FROM posts WHERE id = '5'").get().status, "new"); // 判定待ちに戻る
});

test("データベースの中身: 表ごとの件数と、新しい順の行・検索・ページ送り。決めた表だけ読める", async () => {
  const tables = await listTables(db);
  assert.equal(tables.find((t) => t.name === "posts").rows, 5);
  assert.ok(tables.some((t) => t.name === "meta"));
  const posts = await readTable(db, "posts", new URLSearchParams());
  assert.equal(posts.total, 5);
  assert.equal(posts.rows[0].id, "5"); // 新しく入れた順
  assert.ok(posts.columns.includes("author_id"));
  const found = await readTable(db, "users", new URLSearchParams("q=友達"));
  assert.deepEqual(found.rows.map((r) => r.id), ["300"]);
  db.raw.prepare("UPDATE judgments SET summary = ? WHERE post_id = '1'").run("あ".repeat(500));
  const long = await readTable(db, "judgments", new URLSearchParams("q=ああ"));
  assert.match(long.rows[0].summary, /…\(500文字\)$/);
  assert.equal((await readTable(db, "posts", new URLSearchParams("offset=4"))).rows.length, 1);
  assert.equal(await readTable(db, "sqlite_master", new URLSearchParams()), null);
});

test("スワイプの設定は初期値があり、変えると保存される", async () => {
  const first = await getSettings(db);
  assert.deepEqual([first.swipe_right, first.swipe_left], ["hold", "delete"]);
  assert.deepEqual([first.sort_rule, first.sort_auto, first.sort_presets], [{}, false, []]);
  const saved = await saveSettings(db, { swipe_left: "accepted" });
  assert.equal(saved.swipe_left, "accepted");
  await assert.rejects(saveSettings(db, { swipe_right: "seen" }), RangeError);
});

test("仕分けのチェック・自動・プリセットを保存でき、おかしな中身は受け付けない", async () => {
  const saved = await saveSettings(db, { sort_rule: { gender: ["男性", "男性", " "], area: ["大阪府"], score: ["-50"], crit: ["クリティカル"], zzz: ["x"] }, sort_auto: true });
  assert.deepEqual(saved.sort_rule, { gender: ["男性"], area: ["大阪府"], score: ["-50"] }); // 廃止したクリティカルの項目は落とす
  assert.equal(saved.sort_auto, true);
  assert.equal(saved.swipe_right, "hold");
  const p = await saveSettings(db, { sort_presets: [{ name: "ふだん", rule: { gender: ["男性"] } }, { name: "空", rule: {} }] });
  assert.deepEqual(p.sort_presets, [{ name: "ふだん", rule: { gender: ["男性"] } }, { name: "空", rule: {} }]);
  assert.deepEqual(await getSettings(db), p);
  await assert.rejects(saveSettings(db, { sort_rule: ["男性"] }), RangeError);
  await assert.rejects(saveSettings(db, { sort_rule: { gender: "男性" } }), RangeError);
  await assert.rejects(saveSettings(db, { sort_auto: "yes" }), RangeError);
  await assert.rejects(saveSettings(db, { sort_presets: [{ name: "", rule: {} }] }), RangeError);
  // 選んでいるプリセットは1つ。ないプリセットの名前は残さず、プリセットを消すと外れる
  const a = await saveSettings(db, { sort_presets: [{ name: "A", rule: {} }, { name: "B", rule: { gender: ["男性"] } }], sort_active: "B" });
  assert.equal(a.sort_active, "B");
  assert.equal((await saveSettings(db, { sort_active: "ない" })).sort_active, null);
  await saveSettings(db, { sort_active: "B" });
  assert.equal((await saveSettings(db, { sort_presets: [{ name: "A", rule: {} }] })).sort_active, null);
  assert.equal((await getSettings(db)).sort_active, null);
  await assert.rejects(saveSettings(db, { sort_active: 3 }), RangeError);
  // 前からの保存(sort_active がない)は、今のチェックと同じプリセットを選んでいることにする
  await db.prepare("UPDATE meta SET value = ? WHERE key = 'finder_settings'")
    .bind(JSON.stringify({ sort_rule: { gender: ["男性"] }, sort_presets: [{ name: "A", rule: {} }, { name: "B", rule: { gender: ["男性"] } }] })).run();
  assert.equal((await getSettings(db)).sort_active, "B");
});

test("絞り込みの条件は仕分けとは別に保存され、収集日の項目も持てる", async () => {
  const before = await getSettings(db);
  const saved = await saveSettings(db, { view_rule: { shoot: ["東京都", "東京都"], score: ["20"], within: ["3"], before: ["7"], zzz: ["x"] } });
  assert.deepEqual(saved.view_rule, { shoot: ["東京都"], score: ["20"], within: ["3"], before: ["7"] });
  assert.deepEqual(saved.sort_rule, before.sort_rule); // 仕分けのチェックは変わらない
  assert.deepEqual((await getSettings(db)).view_rule, saved.view_rule);
  assert.deepEqual((await saveSettings(db, { sort_rule: { within: ["3"] } })).sort_rule, {}); // 収集日は仕分けにはない
  await assert.rejects(saveSettings(db, { view_rule: ["東京都"] }), RangeError);
  assert.deepEqual((await saveSettings(db, { view_rule: {} })).view_rule, {});
});

test("前の「まとめて」のプリセットは読み飛ばす", async () => {
  await db.prepare("INSERT OR REPLACE INTO meta (key, value, updated_at) VALUES ('finder_settings', ?, '2026-10-01')")
    .bind(JSON.stringify({ swipe_right: "none", bulk_presets: [{ name: "ブルアカ", conds: [], q: "ブルアカ", mode: "and" }], presets_seeded: true })).run();
  const s = await getSettings(db);
  assert.equal(s.swipe_right, "none");
  assert.equal("bulk_presets" in s, false);
});

test("自動の仕分けにかけてよいか(touched): 状態を一度でも変えた案件は外す。開いただけの前の行はかける", async () => {
  const touched = async () => Object.fromEntries((await listCases(db, new URLSearchParams())).map((c) => [c.id, c.touched]));
  assert.deepEqual(await touched(), { "1": false, "2": false });
  db.raw.prepare(`INSERT INTO reviews (post_id, state, opened_at, updated_at) VALUES ('1', 'new', 'x', 'x')`).run(); // 前に開いただけ
  assert.deepEqual(await touched(), { "1": false, "2": false });
  await setReviews(db, { ids: ["1", "2"], state: "delete" });
  await setReviews(db, { ids: ["1", "2"], state: "new" }); // 元に戻した
  assert.deepEqual(await touched(), { "1": true, "2": true });
});

test("一覧にも添付画像の URL が入る(詳細を開いた瞬間から画像の場所を取るため)", async () => {
  const cases = await listCases(db, new URLSearchParams());
  assert.deepEqual(cases.find((c) => c.id === "1").media, ["https://pbs.twimg.com/m.jpg"]);
  assert.equal("media_json" in cases[0], false);
});

test("まとめて状態を変えられる(見つからない ID は飛ばし、新着に戻すこともできる)", async () => {
  const r = await setReviews(db, { ids: ["1", "2", "999"], state: "delete" });
  assert.deepEqual(r, { updated: ["1", "2"], state: "delete" });
  const all = async () => [...(await listCases(db, new URLSearchParams())), ...(await listCases(db, new URLSearchParams("group=deleted")))];
  const states = async () => Object.fromEntries((await all()).map((c) => [c.id, c.review]));
  assert.deepEqual(await states(), { "1": "delete", "2": "delete" });
  await setReviews(db, { ids: ["1"], state: "new" });
  assert.equal((await states())["1"], "new");
  await setReviews(db, { ids: ["2"], state: "accepted" });
  assert.equal((await states())["2"], "accepted");
  await assert.rejects(setReviews(db, { ids: [], state: "delete" }), RangeError);
  await assert.rejects(setReviews(db, { ids: ["1"], state: "nope" }), RangeError);
  await assert.rejects(setReviews(db, { ids: ["1; DROP"], state: "seen" }), RangeError);
});

test("対象外の一覧も見られる", async () => {
  const cases = await listCases(db, new URLSearchParams("view=rejected"));
  assert.deepEqual(cases.map((c) => [c.id, c.reject_reason]), [["3", "カメラマン募集ではない"]]);
});

test("詳細は画像・点数の内訳・関係するアカウントを含む", async () => {
  const c = await getCase(db, "1");
  assert.deepEqual(c.media, ["https://pbs.twimg.com/m.jpg"]);
  assert.equal(c.score_detail[0].points, 50);
  assert.deepEqual(c.people.map((p) => [p.username, p.role]), [["friend", "mention"]]);
  assert.equal(await getCase(db, "999"), null);
});

test("詳細はツリーを古い順に、返信・引用と依頼主本人かを付けて返す", async () => {
  assert.deepEqual((await getCase(db, "1")).tree, []);
  const run = (sql, ...a) => db.raw.prepare(sql).run(...a);
  run(`INSERT INTO posts (id, author_id, source, text, created_at, quoted_id, media_json, status, collected_at)
       VALUES ('20', '300', 'tree', '11/3 ビッグサイトで併せ', 'a', NULL, '["https://pbs.twimg.com/t.jpg"]', 'context', 'x'),
              ('30', '100', 'tree', '撮ってくれる方を探します', 'b', '20', '[]', 'context', 'x')`);
  run(`UPDATE posts SET reply_to_id = '30' WHERE id = '1'`);
  run(`INSERT INTO post_trees (post_id, ids_json, updated_at) VALUES ('1', '["20","30","404"]', 'x')`);
  const tree = (await getCase(db, "1")).tree;
  assert.deepEqual(tree.map((t) => [t.id, t.relation, t.by_client, t.username]), [["20", "root", false, "friend"], ["30", "quote", true, "client"]]);
  assert.deepEqual(tree[0].media, ["https://pbs.twimg.com/t.jpg"]);
  assert.equal(tree[0].author_id, undefined);
});

test("画像から読んだアカウントも関係するアカウントに並べる", async () => {
  db.raw.prepare(`INSERT INTO image_reads (post_id, read_at, images, accounts_json) VALUES ('1', 'x', 1, ?)`).run(
    JSON.stringify([{ handle: "Friend", name: "" }, { handle: "cos_new", name: "新しい人" }, { handle: "client", name: "" }, { handle: "bad id", name: "" }]));
  const c = await getCase(db, "1");
  assert.deepEqual(c.people.map((p) => [p.username, p.name, p.roles]), [["friend", "友達", ["mention", "image"]], ["cos_new", "新しい人", ["image"]]]);
  assert.equal(c.people[1].id, null);
  // 画像を読んでいない案件は今まで通り
  assert.deepEqual((await getCase(db, "2")).people, []);
});

test("関係するアカウントは同じ人を 1 回だけ出し、投稿者本人は出さない", async () => {
  const sql = (q) => db.raw.prepare(q).run();
  sql(`INSERT INTO post_users (post_id, user_id, role) VALUES ('1', '300', 'reply_to'), ('1', '100', 'reply_to'), ('1', '100', 'mention')`);
  try {
    const c = await getCase(db, "1");
    assert.deepEqual(c.people.map((p) => [p.username, p.role, p.roles]), [["friend", "reply_to", ["reply_to", "mention"]]]);
  } finally {
    sql(`DELETE FROM post_users WHERE post_id = '1' AND (role = 'reply_to' OR (user_id = '100' AND role = 'mention'))`);
  }
});

test("今月の費用は今月分だけを設定の為替レートで円にする", async () => {
  const s = await getSummary(db);
  assert.equal(s.month_cost_yen, 150);
  assert.equal(s.goal_yen, 1200);
  assert.equal(s.waiting, 1);
  assert.equal(s.last_run_status, "ok");
});

test("Access のトークンが正しければ通す", async () => {
  const env = { ACCESS_TEAM_DOMAIN: TEAM, ACCESS_AUD: AUD };
  assert.equal(await verifyAccess(req("/", await makeToken()), env, certsFetch), "me@example.com");
});

test("トークンがない・偽物・期限切れ・別アプリ用なら通さない", async () => {
  const env = { ACCESS_TEAM_DOMAIN: TEAM, ACCESS_AUD: AUD };
  assert.equal(await verifyAccess(req("/"), env, certsFetch), null);
  const good = await makeToken();
  const forged = good.slice(0, -4) + (good.endsWith("AAAA") ? "BBBB" : "AAAA");
  assert.equal(await verifyAccess(req("/", forged), env, certsFetch), null);
  assert.equal(await verifyAccess(req("/", await makeToken({ exp: 1 })), env, certsFetch), null);
  assert.equal(await verifyAccess(req("/", await makeToken({ aud: ["other"] })), env, certsFetch), null);
  assert.equal(await verifyAccess(req("/", await makeToken({ iss: "https://evil.example" })), env, certsFetch), null);
  assert.equal(await verifyAccess(req("/", await makeToken({}, "unknown")), env, certsFetch), null);
});

test("Access の設定がないときは中身を返さない", async () => {
  const res = await worker.fetch(req("/api/cases", await makeToken()), { DB: db });
  assert.equal(res.status, 503);
  const res2 = await worker.fetch(req("/api/cases"), { DB: db, ACCESS_TEAM_DOMAIN: TEAM, ACCESS_AUD: AUD });
  assert.equal(res2.status, 403);
});

test("Access を通れば画面と API が返る", async () => {
  const realFetch = globalThis.fetch;
  globalThis.fetch = certsFetch;
  try {
    const env = { DB: db, ACCESS_TEAM_DOMAIN: TEAM, ACCESS_AUD: AUD };
    const token = await makeToken();
    const page = await worker.fetch(req("/", token), env);
    assert.equal(page.status, 200);
    assert.match(await page.text(), /<title>X Finder<\/title>/);
    assert.equal(page.headers.get("cache-control"), "no-store");
    const manifest = await (await worker.fetch(req("/manifest.webmanifest", token), env)).json();
    assert.equal(manifest.display, "standalone");
    const icon = await worker.fetch(req("/apple-touch-icon.png", token), env);
    assert.equal(icon.headers.get("content-type"), "image/png");
    assert.deepEqual([...new Uint8Array(await icon.arrayBuffer()).slice(1, 4)], [80, 78, 71]); // "PNG"
    assert.equal((await worker.fetch(req("/icon-512.png"), env)).status, 403); // アイコンも Access を通った人だけ
    const list = await (await worker.fetch(req("/api/cases", token), env)).json();
    assert.equal(list.cases.length, 2);
    assert.equal(list.area_choices.blocks.flatMap((b) => b[1]).length, 47); // 仕分けに最初から並べる活動地域
    assert.equal((await worker.fetch(req("/api/cases/1", token), env)).status, 200);
    assert.equal((await worker.fetch(req("/api/cases/abc", token), env)).status, 404);

    const post = (body, headers = {}) => worker.fetch(new Request("https://x-finder.example.workers.dev/api/cases/1/review", {
      method: "POST", body: JSON.stringify(body),
      headers: { "cf-access-jwt-assertion": token, "content-type": "application/json", "x-finder": "1", ...headers },
    }), env);
    assert.deepEqual(await (await post({ state: "hold" })).json(), { review: "hold" });
    assert.equal((await post({ state: "bad" })).status, 400);
    assert.equal((await post({ state: "accepted" }, { "x-finder": "" })).status, 403);
    assert.equal((await post({ state: "accepted" }, { origin: "https://evil.example" })).status, 403);
    assert.equal((await worker.fetch(req("/api/cases/1/review", token), env)).status, 405);
    const setting = await worker.fetch(new Request("https://x-finder.example.workers.dev/api/settings", {
      method: "POST", body: JSON.stringify({ swipe_right: "none" }),
      headers: { "cf-access-jwt-assertion": token, "content-type": "application/json", "x-finder": "1" },
    }), env);
    assert.equal((await setting.json()).swipe_right, "none");
    assert.equal((await (await worker.fetch(req("/api/settings", token), env)).json()).swipe_right, "none");
    const black = (body, headers = {}) => worker.fetch(new Request("https://x-finder.example.workers.dev/api/blacklist", {
      method: "POST", body: JSON.stringify(body),
      headers: { "cf-access-jwt-assertion": token, "content-type": "application/json", "x-finder": "1", ...headers },
    }), env);
    assert.equal((await black({ user_id: "100", reason: "spam" }, { origin: "https://evil.example" })).status, 403);
    assert.equal((await black({ user_id: "100", reason: "nope" })).status, 400);
    assert.equal((await black({ user_id: "999", reason: "spam" })).status, 404);
    assert.equal((await black({ user_id: "100", reason: "photographer" })).status, 200);
    assert.equal((await (await worker.fetch(req("/api/blacklist", token), env)).json()).blacklist[0].reason, "photographer");
    assert.equal((await (await worker.fetch(req("/api/cases", token), env)).json()).cases.length, 0);
    assert.ok((await (await worker.fetch(req("/api/db", token), env)).json()).tables.length > 5);
    assert.equal((await worker.fetch(req("/api/db/runs", token), env)).status, 200);
    assert.equal((await worker.fetch(req("/api/db/nope", token), env)).status, 404);
    assert.equal((await worker.fetch(req("/api/db/posts"), env)).status, 403); // Access を通っていなければ見られない
  } finally {
    globalThis.fetch = realFetch;
  }
});
