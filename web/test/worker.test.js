import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";

import { resetCertsCache, verifyAccess } from "../src/auth.js";
import worker, { getCase, getSettings, getSummary, listCases, saveSettings, setReview } from "../src/index.js";
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
         VALUES (?, ?, 'コスプレ撮影', ?, '併せ撮影', ?, '[{"label":"種類: コスプレ撮影","points":50}]', ?, ?)`, id, now, pref, score, critical, reject);
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

test("一覧は対象の案件を点数順に、直近の分だけ返す", async () => {
  const cases = await listCases(db, new URLSearchParams());
  assert.deepEqual(cases.map((c) => c.id), ["2", "1"]);
  assert.equal(cases[0].critical, true);
  assert.equal(cases[1].near, true);
  assert.equal(cases[1].others, 1);
  assert.equal(cases[1].username, "client");
});

test("確認の状態: 開くと確認済み、保留・削除予定に変えられ、保留は古くても一覧に残る", async () => {
  let cases = await listCases(db, new URLSearchParams());
  assert.deepEqual(cases.map((c) => c.review), ["unread", "unread"]);
  assert.equal(await setReview(db, "1", { opened: true }), "seen");
  assert.equal(await setReview(db, "2", { state: "delete" }), "delete");
  assert.equal(await setReview(db, "2", { opened: true }), "delete"); // 開いても削除予定のまま
  assert.equal(await setReview(db, "4", { state: "hold" }), "hold"); // 30 日前の案件
  cases = await listCases(db, new URLSearchParams());
  assert.deepEqual(cases.map((c) => [c.id, c.review]), [["4", "hold"], ["2", "delete"], ["1", "seen"]]);
  assert.equal((await getCase(db, "4")).review, "hold");
  assert.equal(await setReview(db, "999", { state: "hold" }), null);
  assert.equal(await setReview(db, "1", { state: "unread" }), "unread"); // 元に戻す
  assert.equal((await getCase(db, "1")).review, "unread");
  await assert.rejects(setReview(db, "1", { state: "bad" }), RangeError);
});

test("スワイプの設定は初期値があり、変えると保存される", async () => {
  assert.deepEqual(await getSettings(db), { swipe_right: "hold", swipe_left: "delete" });
  assert.deepEqual(await saveSettings(db, { swipe_left: "seen" }), { swipe_right: "hold", swipe_left: "seen" });
  assert.deepEqual(await getSettings(db), { swipe_right: "hold", swipe_left: "seen" });
  await assert.rejects(saveSettings(db, { swipe_right: "unread" }), RangeError);
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
    assert.match(await page.text(), /ファインダー/);
    assert.equal(page.headers.get("cache-control"), "no-store");
    const list = await (await worker.fetch(req("/api/cases", token), env)).json();
    assert.equal(list.cases.length, 2);
    assert.equal((await worker.fetch(req("/api/cases/1", token), env)).status, 200);
    assert.equal((await worker.fetch(req("/api/cases/abc", token), env)).status, 404);

    const post = (body, headers = {}) => worker.fetch(new Request("https://x-finder.example.workers.dev/api/cases/1/review", {
      method: "POST", body: JSON.stringify(body),
      headers: { "cf-access-jwt-assertion": token, "content-type": "application/json", "x-finder": "1", ...headers },
    }), env);
    assert.deepEqual(await (await post({ state: "hold" })).json(), { review: "hold" });
    assert.equal((await post({ state: "bad" })).status, 400);
    assert.equal((await post({ state: "seen" }, { "x-finder": "" })).status, 403);
    assert.equal((await post({ state: "seen" }, { origin: "https://evil.example" })).status, 403);
    assert.equal((await worker.fetch(req("/api/cases/1/review", token), env)).status, 405);
    const setting = await worker.fetch(new Request("https://x-finder.example.workers.dev/api/settings", {
      method: "POST", body: JSON.stringify({ swipe_right: "none" }),
      headers: { "cf-access-jwt-assertion": token, "content-type": "application/json", "x-finder": "1" },
    }), env);
    assert.equal((await setting.json()).swipe_right, "none");
    assert.equal((await (await worker.fetch(req("/api/settings", token), env)).json()).swipe_right, "none");
  } finally {
    globalThis.fetch = realFetch;
  }
});
