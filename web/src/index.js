// N13 X Finder: D1 にたまった判定・採点の結果を、スマホで見るための Web ページ。
// D1 は読むだけ。ただし案件カードの確認の状態(未確認 / 確認済み / 保留 / 削除予定)と、画面の設定だけは書く。
// Cloudflare Access を通った人だけが見られる(auth.js)。
import { verifyAccess } from "./auth.js";
import page from "./page.html";
import icon180 from "./icons/icon-180.png";
import icon192 from "./icons/icon-192.png";
import icon512 from "./icons/icon-512.png";

const NEAR = ["東京都", "神奈川県", "埼玉県", "千葉県"];
const MAX_LIST = 300;
const REVIEW_STATES = ["seen", "hold", "delete", "unread"]; // 行がなければ未確認(unread)。unread は「元に戻す」用
// 画面の設定(D1 の meta に保存するので、スマホでも PC でも同じになる)。スワイプで付ける状態。none は何もしない
const SETTINGS_KEY = "finder_settings";
const SWIPE_CHOICES = ["hold", "delete", "seen", "none"];
const DEFAULT_SETTINGS = { swipe_right: "hold", swipe_left: "delete" };

// ブラックリストの理由。spam=スパム / false=虚偽の申告 / photographer=被写体ではなくカメラマンだった / other=その他
const BLACKLIST_REASONS = ["spam", "false", "photographer", "other"];
const BLACKLIST_SKIP = "ブラックリスト"; // 判定前のポストに付ける除外理由(finder/store.py と同じ)
const NOT_BLACKLISTED = "NOT EXISTS (SELECT 1 FROM blacklist b WHERE b.user_id = p.author_id)";

// 収集の実行(schema.sql)より先に画面が使われても困らないよう、画面が書く表はここでも作る
const TABLES_DDL = [
  `CREATE TABLE IF NOT EXISTS reviews (
    post_id TEXT PRIMARY KEY, state TEXT NOT NULL, opened_at TEXT, updated_at TEXT NOT NULL)`,
  `CREATE TABLE IF NOT EXISTS blacklist (
    user_id TEXT PRIMARY KEY, username TEXT, reason TEXT NOT NULL, note TEXT, added_at TEXT NOT NULL)`,
];
let tablesReady = false;
async function ensureTables(db) {
  if (tablesReady) return;
  await db.batch(TABLES_DDL.map((sql) => db.prepare(sql)));
  tablesReady = true;
}

const SECURITY_HEADERS = {
  "cache-control": "no-store",
  "x-robots-tag": "noindex, nofollow",
  "x-content-type-options": "nosniff",
  "referrer-policy": "no-referrer",
  "x-frame-options": "DENY",
};

// ホーム画面に追加してアプリのように使うための情報(PWA)
const MANIFEST = {
  name: "X Finder",
  short_name: "X Finder",
  start_url: "/",
  scope: "/",
  display: "standalone",
  orientation: "portrait",
  background_color: "#f3f5f8",
  theme_color: "#141a24",
  icons: [
    { src: "/icon-192.png", sizes: "192x192", type: "image/png", purpose: "any maskable" },
    { src: "/icon-512.png", sizes: "512x512", type: "image/png", purpose: "any maskable" },
  ],
};
const ICONS = { "/icon-180.png": icon180, "/apple-touch-icon.png": icon180, "/icon-192.png": icon192, "/icon-512.png": icon512 };

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", ...SECURITY_HEADERS },
  });
}

function text(body, status) {
  return new Response(body, { status, headers: { "content-type": "text/plain; charset=utf-8", ...SECURITY_HEADERS } });
}

function parseJson(value, fallback) {
  try {
    return value ? JSON.parse(value) : fallback;
  } catch {
    return fallback;
  }
}

function daysAgoIso(days) {
  return new Date(Date.now() - days * 864e5).toISOString().replace(/\.\d{3}Z$/, "+00:00");
}

const CASE_COLUMNS = `
  p.id, p.text, p.url, p.created_at, p.collected_at, p.status, p.skip_reason, p.quoted_id,
  j.case_type, j.works, j.prefecture, j.place, j.dates, j.summary, j.score, j.critical, j.critical_reason, j.reject_reason,
  p.author_id, u.username, u.name, u.followers, u.following, u.profile_image, COALESCE(r.state, 'unread') AS review`;

export async function listCases(db, params) {
  const view = params.get("view") === "rejected" ? "rejected" : "judged";
  const days = Math.min(Math.max(Number(params.get("days")) || 14, 1), 60);
  const { results } = await db
    .prepare(
      `SELECT ${CASE_COLUMNS},
        (SELECT COUNT(*) FROM post_users pu WHERE pu.post_id = p.id AND pu.role IN ('mention', 'quoted')) AS others
       FROM posts p JOIN judgments j ON j.post_id = p.id LEFT JOIN users u ON u.id = p.author_id
       LEFT JOIN reviews r ON r.post_id = p.id
       WHERE p.status = ? AND (p.collected_at >= ? OR r.state = 'hold') AND ${NOT_BLACKLISTED}
       ORDER BY j.score DESC, p.created_at DESC LIMIT ?`,
    )
    .bind(view, daysAgoIso(days), MAX_LIST)
    .all();
  return results.map((r) => ({ ...r, critical: !!r.critical, near: NEAR.includes(r.prefecture) }));
}

export async function getCase(db, id) {
  const row = await db
    .prepare(
      `SELECT ${CASE_COLUMNS}, p.media_json, j.score_json, j.prefecture_source, q.text AS quoted_text, q.url AS quoted_url,
         u.description, u.location
       FROM posts p JOIN judgments j ON j.post_id = p.id LEFT JOIN users u ON u.id = p.author_id
       LEFT JOIN reviews r ON r.post_id = p.id
       LEFT JOIN posts q ON q.id = p.quoted_id
       WHERE p.id = ?`,
    )
    .bind(id)
    .first();
  if (!row) return null;
  const { results: people } = await db
    .prepare(
      `SELECT pu.role, u.id, u.username, u.name, u.followers, u.following, u.profile_image
       FROM post_users pu JOIN users u ON u.id = pu.user_id
       WHERE pu.post_id = ? AND pu.role IN ('mention', 'quoted', 'reply_to')
       ORDER BY pu.role, u.username`,
    )
    .bind(id)
    .all();
  const black = await db.prepare("SELECT reason, note, added_at FROM blacklist WHERE user_id = ?").bind(row.author_id).first();
  const { media_json, score_json, ...rest } = row;
  return {
    ...rest,
    critical: !!row.critical,
    near: NEAR.includes(row.prefecture),
    media: parseJson(media_json, []),
    score_detail: parseJson(score_json, []),
    people,
    blacklist: black || null,
  };
}

// データベースの中身を見る画面(読むだけ)。見せる表はここに並べたものだけ
const DB_TABLES = ["posts", "judgments", "reviews", "blacklist", "users", "user_aliases", "post_users", "runs", "costs", "meta"];
const DB_PAGE = 30;
const DB_CELL_MAX = 400; // 長い値(AI の返事の JSON など)は先頭だけ返す

export async function listTables(db) {
  const out = [];
  for (const name of DB_TABLES) {
    const row = await db.prepare(`SELECT COUNT(*) AS n FROM ${name}`).first().catch(() => null);
    if (row) out.push({ name, rows: row.n });
  }
  return out;
}

export async function readTable(db, name, params) {
  if (!DB_TABLES.includes(name)) return null;
  const { results: info } = await db.prepare(`PRAGMA table_info(${name})`).all();
  const columns = info.map((c) => c.name);
  const offset = Math.max(0, Math.floor(Number(params.get("offset")) || 0));
  const q = (params.get("q") || "").trim().slice(0, 100);
  const where = q ? `WHERE ${columns.map((c) => `CAST("${c}" AS TEXT) LIKE ?`).join(" OR ")}` : "";
  const binds = q ? columns.map(() => `%${q}%`) : [];
  const total = (await db.prepare(`SELECT COUNT(*) AS n FROM ${name} ${where}`).bind(...binds).first()).n;
  const { results } = await db
    .prepare(`SELECT * FROM ${name} ${where} ORDER BY rowid DESC LIMIT ? OFFSET ?`)
    .bind(...binds, DB_PAGE, offset)
    .all();
  const rows = results.map((r) => {
    const o = {};
    for (const c of columns) {
      const v = r[c];
      o[c] = typeof v === "string" && v.length > DB_CELL_MAX ? `${v.slice(0, DB_CELL_MAX)}…(${v.length}文字)` : v;
    }
    return o;
  });
  return { name, columns, total, offset, page: DB_PAGE, rows };
}

// ブラックリスト。載せた人の判定待ちのポストは判定にかけない(AI の費用を使わない)
export async function listBlacklist(db) {
  const { results } = await db
    .prepare(
      `SELECT b.user_id, COALESCE(u.username, b.username) AS username, u.name, u.profile_image, b.reason, b.note, b.added_at,
         (SELECT COUNT(*) FROM posts p WHERE p.author_id = b.user_id) AS posts
       FROM blacklist b LEFT JOIN users u ON u.id = b.user_id ORDER BY b.added_at DESC`,
    )
    .all();
  return results;
}

export async function changeBlacklist(db, body) {
  const id = String(body.user_id || "");
  if (!/^\d{1,25}$/.test(id)) throw new RangeError("user_id");
  if (body.remove === true) {
    // 載っていた間に判定を飛ばした直近のポストは、判定待ちに戻す(次の収集で判定され、一覧に出る)
    await db.batch([
      db.prepare("DELETE FROM blacklist WHERE user_id = ?").bind(id),
      db
        .prepare("UPDATE posts SET status = 'new', skip_reason = NULL WHERE author_id = ? AND status = 'skipped' AND skip_reason = ? AND collected_at >= ?")
        .bind(id, BLACKLIST_SKIP, daysAgoIso(14)),
    ]);
    return { user_id: id, blacklisted: false };
  }
  if (!BLACKLIST_REASONS.includes(body.reason)) throw new RangeError("reason");
  const note = typeof body.note === "string" ? body.note.trim().slice(0, 200) : "";
  const user = await db.prepare("SELECT username FROM users WHERE id = ?").bind(id).first();
  if (!user) return null;
  await db.batch([
    db
      .prepare(`INSERT INTO blacklist (user_id, username, reason, note, added_at) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET reason = excluded.reason, note = excluded.note`)
      .bind(id, user.username, body.reason, note || null, nowIso()),
    db.prepare("UPDATE posts SET status = 'skipped', skip_reason = ? WHERE author_id = ? AND status = 'new'").bind(BLACKLIST_SKIP, id),
  ]);
  return { user_id: id, blacklisted: true };
}

function nowIso() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00");
}

// 確認の状態を変える。opened=true は「開いた」の記録で、未確認のときだけ確認済みにする。
export async function setReview(db, id, { state, opened }) {
  const exists = await db.prepare("SELECT 1 AS ok FROM judgments WHERE post_id = ?").bind(id).first();
  if (!exists) return null;
  const now = nowIso();
  if (opened) {
    await db
      .prepare(`INSERT INTO reviews (post_id, state, opened_at, updated_at) VALUES (?, 'seen', ?, ?)
                ON CONFLICT(post_id) DO NOTHING`)
      .bind(id, now, now)
      .run();
  } else {
    if (!REVIEW_STATES.includes(state)) throw new RangeError("state");
    if (state === "unread") {
      await db.prepare("DELETE FROM reviews WHERE post_id = ?").bind(id).run();
      return "unread";
    }
    await db
      .prepare(`INSERT INTO reviews (post_id, state, opened_at, updated_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(post_id) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at`)
      .bind(id, state, now, now)
      .run();
  }
  const row = await db.prepare("SELECT state FROM reviews WHERE post_id = ?").bind(id).first();
  return row?.state || "unread";
}

export async function getSettings(db) {
  const row = await db.prepare("SELECT value FROM meta WHERE key = ?").bind(SETTINGS_KEY).first();
  const saved = parseJson(row?.value, {});
  const out = { ...DEFAULT_SETTINGS };
  for (const k of Object.keys(DEFAULT_SETTINGS)) if (SWIPE_CHOICES.includes(saved[k])) out[k] = saved[k];
  return out;
}

export async function saveSettings(db, body) {
  const next = await getSettings(db);
  for (const k of Object.keys(DEFAULT_SETTINGS)) {
    if (body[k] === undefined) continue;
    if (!SWIPE_CHOICES.includes(body[k])) throw new RangeError(k);
    next[k] = body[k];
  }
  await db
    .prepare(`INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?)
              ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at`)
    .bind(SETTINGS_KEY, JSON.stringify(next), nowIso())
    .run();
  return next;
}

// 画面の fetch からだけ書けるようにする(よそのサイトからのフォーム送信を受け付けない)
function sameOriginWrite(request, url) {
  const origin = request.headers.get("origin");
  if (origin && origin !== url.origin) return false;
  return request.headers.get("x-finder") === "1" && (request.headers.get("content-type") || "").startsWith("application/json");
}

// サマリー画面の数字。日付は日本時間で区切る。
const JST_DAY = (col) => `date(datetime(substr(${col}, 1, 19), '+9 hours'))`;
const STATS_DAYS = [7, 30, 90];

function jstDay(offsetDays = 0) {
  return new Date(Date.now() + 9 * 3600e3 - offsetDays * 864e5).toISOString().slice(0, 10);
}

function jstMonthStartIso(monthsAgo) {
  const jst = new Date(Date.now() + 9 * 3600e3);
  return new Date(Date.UTC(jst.getUTCFullYear(), jst.getUTCMonth() - monthsAgo, 1) - 9 * 3600e3).toISOString().replace(/\.\d{3}Z$/, "+00:00");
}

export async function getStats(db, params) {
  const days = STATS_DAYS.includes(Number(params.get("days"))) ? Number(params.get("days")) : 30;
  const config = parseJson((await db.prepare("SELECT value FROM meta WHERE key = 'config_last_good'").first())?.value, {});
  const rate = Number((config.basic || {})["為替レート(円/ドル)"]) || 155;
  const since = new Date(Date.now() - days * 864e5).toISOString().replace(/\.\d{3}Z$/, "+00:00");
  const all = async (sql, ...binds) => (await db.prepare(sql).bind(...binds).all()).results;

  // 日ごと: 取得ポスト数(runs)、判定数・対象・クリティカル(judgments)、費用(costs)
  const fetched = await all(`SELECT ${JST_DAY("started_at")} AS d, SUM(fetched) AS fetched, SUM(new_posts) AS new_posts, COUNT(*) AS runs
    FROM runs WHERE started_at >= ? AND status = 'ok' GROUP BY d`, since);
  const judged = await all(`SELECT ${JST_DAY("j.judged_at")} AS d, COUNT(*) AS judged,
      SUM(CASE WHEN j.reject_reason IS NULL THEN 1 ELSE 0 END) AS target, SUM(j.critical) AS critical
    FROM judgments j WHERE j.judged_at >= ? GROUP BY d`, since);
  const costs = await all(`SELECT ${JST_DAY("at")} AS d,
      SUM(CASE WHEN service = 'twitterapi.io' THEN usd ELSE 0 END) AS collect_usd,
      SUM(CASE WHEN service = 'twitterapi.io' THEN 0 ELSE usd END) AS ai_usd
    FROM costs WHERE at >= ? GROUP BY d`, since);
  const byDay = new Map();
  for (let i = days - 1; i >= 0; i--) {
    const d = jstDay(i);
    byDay.set(d, { date: d, fetched: 0, new_posts: 0, judged: 0, target: 0, critical: 0, collect_yen: 0, ai_yen: 0 });
  }
  for (const r of fetched) if (byDay.has(r.d)) Object.assign(byDay.get(r.d), { fetched: r.fetched || 0, new_posts: r.new_posts || 0 });
  for (const r of judged) if (byDay.has(r.d)) Object.assign(byDay.get(r.d), { judged: r.judged || 0, target: r.target || 0, critical: r.critical || 0 });
  for (const r of costs) if (byDay.has(r.d)) Object.assign(byDay.get(r.d), { collect_yen: (r.collect_usd || 0) * rate, ai_yen: (r.ai_usd || 0) * rate });
  const daily = [...byDay.values()].map((d) => ({ ...d, collect_yen: Math.round(d.collect_yen * 100) / 100, ai_yen: Math.round(d.ai_yen * 100) / 100 }));

  // 累計(期間に関係なく、これまでの全部)
  const totals = await db.prepare(`SELECT
      (SELECT COUNT(*) FROM posts WHERE source = 'search') AS collected,
      (SELECT COUNT(*) FROM posts WHERE status = 'skipped') AS skipped,
      (SELECT COUNT(*) FROM judgments) AS judged,
      (SELECT COUNT(*) FROM judgments WHERE reject_reason IS NULL) AS target,
      (SELECT COUNT(*) FROM judgments WHERE critical = 1) AS critical,
      (SELECT COALESCE(SUM(usd), 0) FROM costs) AS usd,
      (SELECT MIN(started_at) FROM runs) AS first_run`).first();
  const reviews = await all("SELECT state, COUNT(*) AS n FROM reviews GROUP BY state");

  // 今月と先月の比べ(先月は同じ日付まで)
  const month = async (from, to) => db.prepare(`SELECT
      (SELECT COALESCE(SUM(fetched), 0) FROM runs WHERE started_at >= ?1 AND started_at < ?2) AS fetched,
      (SELECT COUNT(*) FROM judgments WHERE judged_at >= ?1 AND judged_at < ?2) AS judged,
      (SELECT COUNT(*) FROM judgments WHERE judged_at >= ?1 AND judged_at < ?2 AND reject_reason IS NULL) AS target,
      (SELECT COUNT(*) FROM judgments WHERE judged_at >= ?1 AND judged_at < ?2 AND critical = 1) AS critical,
      (SELECT COALESCE(SUM(usd), 0) FROM costs WHERE at >= ?1 AND at < ?2) AS usd`).bind(from, to).first();
  const now = new Date();
  const thisStart = jstMonthStartIso(0);
  const lastStart = jstMonthStartIso(1);
  const sameDayLast = new Date(Math.min(Date.parse(thisStart), Date.parse(lastStart) + (now - Date.parse(thisStart)))).toISOString().replace(/\.\d{3}Z$/, "+00:00");
  const toYen = (m) => ({ ...m, yen: Math.round((m.usd || 0) * rate) });

  const types = await all(`SELECT COALESCE(case_type, 'その他') AS k, COUNT(*) AS n FROM judgments
    WHERE judged_at >= ? AND reject_reason IS NULL GROUP BY k ORDER BY n DESC`, since);
  const prefs = await all(`SELECT COALESCE(prefecture, '不明') AS k, COUNT(*) AS n FROM judgments
    WHERE judged_at >= ? AND reject_reason IS NULL GROUP BY k ORDER BY n DESC LIMIT 8`, since);
  const rejects = await all(`SELECT reject_reason AS k, COUNT(*) AS n FROM judgments
    WHERE judged_at >= ? AND reject_reason IS NOT NULL GROUP BY k ORDER BY n DESC LIMIT 6`, since);

  return {
    days,
    rate,
    daily,
    totals: { ...totals, yen: Math.round((totals?.usd || 0) * rate) },
    reviews: Object.fromEntries(reviews.map((r) => [r.state, r.n])),
    this_month: toYen(await month(thisStart, "9999")),
    last_month_same_point: toYen(await month(lastStart, sameDayLast)),
    last_month_total: toYen(await month(lastStart, thisStart)),
    types,
    prefectures: prefs,
    reject_reasons: rejects,
  };
}

export async function getSummary(db) {
  const config = parseJson((await db.prepare("SELECT value FROM meta WHERE key = 'config_last_good'").first())?.value, {});
  const basic = config.basic || {};
  const rate = Number(basic["為替レート(円/ドル)"]) || 155;
  const goal = Number(basic["月の費用の目標(円)"]) || 1000;
  // 日本時間の月初め
  const jst = new Date(Date.now() + 9 * 3600e3);
  const monthStart = new Date(Date.UTC(jst.getUTCFullYear(), jst.getUTCMonth(), 1) - 9 * 3600e3).toISOString().replace(/\.\d{3}Z$/, "+00:00");
  const cost = await db.prepare("SELECT COALESCE(SUM(usd), 0) AS usd FROM costs WHERE at >= ?").bind(monthStart).first();
  const run = await db.prepare("SELECT finished_at, status FROM runs ORDER BY id DESC LIMIT 1").first();
  const waiting = await db.prepare("SELECT COUNT(*) AS n FROM posts WHERE status = 'new'").first();
  return {
    month_cost_yen: Math.round((cost?.usd || 0) * rate),
    goal_yen: goal,
    last_run_at: run?.finished_at || null,
    last_run_status: run?.status || null,
    waiting: waiting?.n || 0,
  };
}

export default {
  async fetch(request, env) {
    const email = await verifyAccess(request, env).catch(() => null);
    if (!email) {
      if (!env.ACCESS_TEAM_DOMAIN || !env.ACCESS_AUD) {
        return text("Cloudflare Access の設定(ACCESS_TEAM_DOMAIN と ACCESS_AUD)がまだなので、表示できません。", 503);
      }
      return text("このページは見られません。", 403);
    }

    const url = new URL(request.url);
    if (url.pathname === "/api/settings" && request.method === "POST") {
      if (!sameOriginWrite(request, url)) return text("このページからだけ変更できます。", 403);
      try {
        return json(await saveSettings(env.DB, await request.json().catch(() => ({}))));
      } catch (e) {
        if (e instanceof RangeError) return json({ error: "設定の値が正しくありません" }, 400);
        console.error(e);
        return json({ error: "保存できませんでした" }, 500);
      }
    }
    if (url.pathname === "/api/blacklist" && request.method === "POST") {
      if (!sameOriginWrite(request, url)) return text("このページからだけ変更できます。", 403);
      try {
        await ensureTables(env.DB);
        const r = await changeBlacklist(env.DB, await request.json().catch(() => ({})));
        return r ? json(r) : json({ error: "見つかりません" }, 404);
      } catch (e) {
        if (e instanceof RangeError) return json({ error: "指定が正しくありません" }, 400);
        console.error(e);
        return json({ error: "保存できませんでした" }, 500);
      }
    }
    const write = url.pathname.match(/^\/api\/cases\/(\d{1,25})\/review$/);
    if (write) {
      if (request.method !== "POST") return text("POST だけ使えます。", 405);
      if (!sameOriginWrite(request, url)) return text("このページからだけ変更できます。", 403);
      try {
        const body = await request.json().catch(() => ({}));
        await ensureTables(env.DB);
        const review = await setReview(env.DB, write[1], { state: body.state, opened: body.opened === true });
        return review ? json({ review }) : json({ error: "見つかりません" }, 404);
      } catch (e) {
        if (e instanceof RangeError) return json({ error: "状態の指定が正しくありません" }, 400);
        console.error(e);
        return json({ error: "保存できませんでした" }, 500);
      }
    }
    if (request.method !== "GET") return text("GET だけ使えます。", 405);
    try {
      if (url.pathname.startsWith("/api/")) await ensureTables(env.DB);
      if (url.pathname === "/") {
        return new Response(page, { headers: { "content-type": "text/html; charset=utf-8", ...SECURITY_HEADERS } });
      }
      if (url.pathname === "/manifest.webmanifest") {
        return new Response(JSON.stringify(MANIFEST), { headers: { "content-type": "application/manifest+json", ...SECURITY_HEADERS } });
      }
      if (ICONS[url.pathname]) {
        return new Response(ICONS[url.pathname], { headers: { "content-type": "image/png", ...SECURITY_HEADERS, "cache-control": "private, max-age=86400" } });
      }
      if (url.pathname === "/api/cases") return json({ cases: await listCases(env.DB, url.searchParams) });
      if (url.pathname === "/api/summary") return json(await getSummary(env.DB));
      if (url.pathname === "/api/settings") return json(await getSettings(env.DB));
      if (url.pathname === "/api/db") return json({ tables: await listTables(env.DB) });
      const dbm = url.pathname.match(/^\/api\/db\/([a-z_]{1,30})$/);
      if (dbm) {
        const t = await readTable(env.DB, dbm[1], url.searchParams);
        return t ? json(t) : json({ error: "見つかりません" }, 404);
      }
      if (url.pathname === "/api/blacklist") return json({ blacklist: await listBlacklist(env.DB) });
      if (url.pathname === "/api/stats") return json(await getStats(env.DB, url.searchParams));
      const m = url.pathname.match(/^\/api\/cases\/(\d{1,25})$/);
      if (m) {
        const c = await getCase(env.DB, m[1]);
        return c ? json(c) : json({ error: "見つかりません" }, 404);
      }
      return text("ページがありません。", 404);
    } catch (e) {
      console.error(e);
      return json({ error: "データを読めませんでした" }, 500);
    }
  },
};
