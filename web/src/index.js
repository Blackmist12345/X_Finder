// N13 ファインダー: D1 にたまった判定・採点の結果を、スマホで見るための Web ページ。
// D1 は読むだけ。ただし案件カードの確認の状態(未確認 / 確認済み / 保留 / 削除予定)だけは書く。
// Cloudflare Access を通った人だけが見られる(auth.js)。
import { verifyAccess } from "./auth.js";
import page from "./page.html";

const NEAR = ["東京都", "神奈川県", "埼玉県", "千葉県"];
const MAX_LIST = 300;
const REVIEW_STATES = ["seen", "hold", "delete"]; // 行がなければ未確認(unread)

// 収集の実行(schema.sql)より先に画面が使われても困らないよう、確認の状態の表はここでも作る
const REVIEWS_DDL = `CREATE TABLE IF NOT EXISTS reviews (
  post_id TEXT PRIMARY KEY, state TEXT NOT NULL, opened_at TEXT, updated_at TEXT NOT NULL)`;
let reviewsReady = false;
async function ensureReviews(db) {
  if (reviewsReady) return;
  await db.prepare(REVIEWS_DDL).run();
  reviewsReady = true;
}

const SECURITY_HEADERS = {
  "cache-control": "no-store",
  "x-robots-tag": "noindex, nofollow",
  "x-content-type-options": "nosniff",
  "referrer-policy": "no-referrer",
  "x-frame-options": "DENY",
};

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
  u.username, u.name, u.followers, u.following, u.profile_image, COALESCE(r.state, 'unread') AS review`;

export async function listCases(db, params) {
  const view = params.get("view") === "rejected" ? "rejected" : "judged";
  const days = Math.min(Math.max(Number(params.get("days")) || 14, 1), 60);
  const { results } = await db
    .prepare(
      `SELECT ${CASE_COLUMNS},
        (SELECT COUNT(*) FROM post_users pu WHERE pu.post_id = p.id AND pu.role IN ('mention', 'quoted')) AS others
       FROM posts p JOIN judgments j ON j.post_id = p.id LEFT JOIN users u ON u.id = p.author_id
       LEFT JOIN reviews r ON r.post_id = p.id
       WHERE p.status = ? AND (p.collected_at >= ? OR r.state = 'hold')
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
  const { media_json, score_json, ...rest } = row;
  return {
    ...rest,
    critical: !!row.critical,
    near: NEAR.includes(row.prefecture),
    media: parseJson(media_json, []),
    score_detail: parseJson(score_json, []),
    people,
  };
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
    await db
      .prepare(`INSERT INTO reviews (post_id, state, opened_at, updated_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(post_id) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at`)
      .bind(id, state, now, now)
      .run();
  }
  const row = await db.prepare("SELECT state FROM reviews WHERE post_id = ?").bind(id).first();
  return row?.state || "unread";
}

// 画面の fetch からだけ書けるようにする(よそのサイトからのフォーム送信を受け付けない)
function sameOriginWrite(request, url) {
  const origin = request.headers.get("origin");
  if (origin && origin !== url.origin) return false;
  return request.headers.get("x-finder") === "1" && (request.headers.get("content-type") || "").startsWith("application/json");
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
    const write = url.pathname.match(/^\/api\/cases\/(\d{1,25})\/review$/);
    if (write) {
      if (request.method !== "POST") return text("POST だけ使えます。", 405);
      if (!sameOriginWrite(request, url)) return text("このページからだけ変更できます。", 403);
      try {
        const body = await request.json().catch(() => ({}));
        await ensureReviews(env.DB);
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
      if (url.pathname.startsWith("/api/")) await ensureReviews(env.DB);
      if (url.pathname === "/") {
        return new Response(page, { headers: { "content-type": "text/html; charset=utf-8", ...SECURITY_HEADERS } });
      }
      if (url.pathname === "/api/cases") return json({ cases: await listCases(env.DB, url.searchParams) });
      if (url.pathname === "/api/summary") return json(await getSummary(env.DB));
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
