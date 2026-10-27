// N13 X Finder: D1 にたまった判定・採点の結果を、スマホで見るための Web ページ。
// D1 は読むだけ。ただし案件カードの確認の状態(未確認 / 確認済み / 保留 / 削除予定)と、画面の設定だけは書く。
// Cloudflare Access を通った人だけが見られる(auth.js)。
import { verifyAccess } from "./auth.js";
import { getCalendar, offWords } from "./calendar.js";
import { AREA_CHOICES, KANTO, METRO, profileInfo } from "./profile.js";
import page from "./page.html";
import icon180 from "./icons/icon-180.png";
import icon192 from "./icons/icon-192.png";
import icon512 from "./icons/icon-512.png";

// 撮影場所の都道府県。依頼主のプロフィールから推定したもの(前の判定の分)は撮影場所に数えない(活動地域は別に持つ)
const shootPrefecture = (r) => (r.prefecture && r.prefecture !== "不明" && r.prefecture_source !== "プロフィール" ? r.prefecture : null);
const shootArea = (r) => {
  const pref = shootPrefecture(r);
  return { shoot_pref: pref, shoot_kanto: !!pref && KANTO.includes(pref), shoot_far: !!pref && !KANTO.includes(pref), shoot_metro: !!pref && METRO.includes(pref) };
};
const MAX_LIST = 300;
// カードの状態。new=新着 / checking=確認中 / accepted=受注中 / hold=保留 / delete=削除予定。行がなければ新着(まだ開いていない)。
// 確認中は自分で付けたときだけ(開いただけでは新着のまま)。2週間を過ぎたら新着と同じく消える
// 開いたかどうかは状態とは別に opened_at で持つ(今の画面では使っていない。前に開いただけの行は opened_at = updated_at の新着になっている)
const REVIEW_STATES = ["new", "checking", "accepted", "hold", "delete"];
// 前の名前(確認済み・未確認)で届いたら新着として扱う
const STATE_ALIASES = { seen: "new", unread: "new" };
const normState = (s) => STATE_ALIASES[s] || s;
// 2週間を過ぎても一覧に残す状態
const KEEP_STATES = "('accepted', 'hold')";
// 画面の設定(D1 の meta に保存するので、スマホでも PC でも同じになる)。スワイプで付ける状態。none は何もしない
const SETTINGS_KEY = "finder_settings";
const SWIPE_CHOICES = ["new", "hold", "delete", "checking", "accepted", "none"];
// スワイプは状態タブごとに決める(swipe_tabs = { タブ: { right, left } })。そのタブの状態そのものは選べない(新着に戻すのと同じなので new にする)
const swipeFor = (tab, v) => (v === tab ? (tab === "new" ? "none" : "new") : v);
const DEFAULT_SETTINGS = { swipe_right: "hold", swipe_left: "delete" };

// ブラックリストの理由。spam=スパム / false=虚偽の申告 / photographer=被写体ではなくカメラマンだった / other=その他
const ROLE_ORDER = ["reply_to", "quoted", "mention", "image"];
const BLACKLIST_REASONS = ["spam", "false", "photographer", "other"];
const BLACKLIST_SKIP = "ブラックリスト"; // 判定前のポストに付ける除外理由(finder/store.py と同じ)
const NOT_BLACKLISTED = "NOT EXISTS (SELECT 1 FROM blacklist b WHERE b.user_id = p.author_id)";

// 収集の実行(schema.sql)より先に画面が使われても困らないよう、画面が書く表はここでも作る
const TABLES_DDL = [
  `CREATE TABLE IF NOT EXISTS reviews (
    post_id TEXT PRIMARY KEY, state TEXT NOT NULL, opened_at TEXT, updated_at TEXT NOT NULL)`,
  // 「確認済み」はなくなったので、新着に戻す(何度流しても同じ)
  `UPDATE reviews SET state = 'new' WHERE state = 'seen'`,
  `CREATE TABLE IF NOT EXISTS blacklist (
    user_id TEXT PRIMARY KEY, username TEXT, reason TEXT NOT NULL, note TEXT, added_at TEXT NOT NULL)`,
  // 画像から読んだアカウント(書くのは収集の方。表がまだなくても詳細が開けるように作っておく)
  `CREATE TABLE IF NOT EXISTS image_reads (
    post_id TEXT PRIMARY KEY, read_at TEXT NOT NULL, model TEXT, images INTEGER NOT NULL DEFAULT 0, accounts_json TEXT, error TEXT)`,
  // プロフィールのリンク先から読み取った年齢・活動地域・性別(書くのは収集の方。表がまだなくても一覧が開けるように作っておく)
  `CREATE TABLE IF NOT EXISTS user_links (
    user_id TEXT PRIMARY KEY, urls_json TEXT NOT NULL, updated_at TEXT NOT NULL, fetched_at TEXT, page_url TEXT, found_json TEXT, error TEXT)`,
  // 削除ポスト確認の結果(書くのは収集の方。元のポストが見られなくなったら gone_at が入る)
  `CREATE TABLE IF NOT EXISTS post_checks (post_id TEXT PRIMARY KEY, checked_at TEXT NOT NULL, gone_at TEXT)`,
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
  p.id, p.text, p.url, p.created_at, p.collected_at, p.status, p.skip_reason, p.quoted_id, p.reply_to_id,
  j.case_type, j.works, j.prefecture, j.prefecture_source, j.place, j.dates, j.summary, j.score, j.reject_reason,
  p.author_id, u.username, u.name, u.followers, u.following, u.profile_image, CASE WHEN r.state IN ('checking', 'accepted', 'hold', 'delete') THEN r.state ELSE 'new' END AS review, (r.opened_at IS NOT NULL) AS opened,
  (r.post_id IS NOT NULL AND NOT (r.state = 'new' AND r.opened_at IS r.updated_at)) AS touched,
  u.description, u.location, (SELECT ul.found_json FROM user_links ul WHERE ul.user_id = p.author_id) AS link_found,
  (SELECT pc.gone_at FROM post_checks pc WHERE pc.post_id = p.id) AS gone_at`;

// 点数の内訳から、カードに出す作品の点(「作品: 〇〇」の行)だけを取り出す
function workPoints(detail) {
  return (Array.isArray(detail) ? detail : [])
    .filter((d) => typeof d?.label === "string" && d.label.startsWith("作品: "))
    .map((d) => ({ name: d.label.slice(4), points: d.points }));
}

// 一覧は新しい順に MAX_LIST 件まで取る(点数順に並べるのは画面)。点数順で打ち切ると、点の低い新しい案件が入らなくなるため。
// 削除予定は数が多く枠を埋めてしまうので別に取る(group=deleted で削除予定だけ、ふだんは削除予定以外)。
// 2週間を過ぎても残す状態(受注中・保留)は、枠からはみ出ないよう先に取る
export async function listCases(db, params) {
  const view = params.get("view") === "rejected" ? "rejected" : "judged";
  const days = Math.min(Math.max(Number(params.get("days")) || 14, 1), 60);
  const group = view === "rejected" ? "" : params.get("group") === "deleted" ? "AND r.state = 'delete'" : "AND (r.state IS NULL OR r.state != 'delete')";
  const { results } = await db
    .prepare(
      `SELECT ${CASE_COLUMNS}, p.media_json, j.score_json,
        (SELECT COUNT(*) FROM post_users pu WHERE pu.post_id = p.id AND pu.role IN ('mention', 'quoted')) AS others
       FROM posts p JOIN judgments j ON j.post_id = p.id LEFT JOIN users u ON u.id = p.author_id
       LEFT JOIN reviews r ON r.post_id = p.id
       WHERE p.status = ? AND (p.collected_at >= ? OR r.state IN ${KEEP_STATES}) AND ${NOT_BLACKLISTED} ${group}
       ORDER BY (r.state IN ${KEEP_STATES}) DESC, p.collected_at DESC, p.created_at DESC, p.id DESC LIMIT ?`,
    )
    .bind(view, daysAgoIso(days), MAX_LIST)
    .all();
  // 添付画像の URL も返す(詳細を開いた瞬間から画像の場所を取っておき、あとから表示がずれないように)
  // 自己紹介は一覧では使わないので返さない(年齢と活動地域だけ読み取って付ける)
  return results.map(({ media_json, score_json, description, location, link_found, ...r }) => ({
    ...r,
    ...profileInfo({ name: r.name, description, location, link: parseJson(link_found, null) }),
    work_points: workPoints(parseJson(score_json, [])),
    opened: !!r.opened,
    touched: !!r.touched,
    ...shootArea(r),
    media: parseJson(media_json, []),
  }));
}

export async function getCase(db, id) {
  const row = await db
    .prepare(
      `SELECT ${CASE_COLUMNS}, p.media_json, j.score_json, q.text AS quoted_text, q.url AS quoted_url
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
  // 同じ人が「リプライ先」と「メンション」の両方で入っていることがある(リプライには相手の @ が自動で付く)ので 1 人にまとめる。
  // 投稿者本人(自分のポストへのリプライなど)は「クライアント」に出ているので外す。
  const merged = new Map();
  for (const p of people) {
    if (p.id === row.author_id) continue;
    const m = merged.get(p.id);
    if (m) m.roles.push(p.role);
    else merged.set(p.id, { ...p, roles: [p.role] });
  }
  // 添付画像(メンバー表など)から AI が読んだアカウント。台帳に同じ ID(username)の人がいればその人として出す
  const read = await db.prepare("SELECT accounts_json FROM image_reads WHERE post_id = ?").bind(id).first();
  const fromImage = parseJson(read && read.accounts_json, []).filter((a) => a && /^[A-Za-z0-9_]{1,15}$/.test(a.handle || ""));
  if (fromImage.length) {
    const handles = fromImage.map((a) => a.handle.toLowerCase());
    const { results: known } = await db
      .prepare(
        `SELECT id, username, name, followers, following, profile_image FROM users
         WHERE lower(username) IN (${handles.map(() => "?").join(", ")})`,
      )
      .bind(...handles)
      .all();
    const byName = new Map(known.map((u) => [u.username.toLowerCase(), u]));
    const author = (row.username || "").toLowerCase();
    for (const a of fromImage) {
      const key = a.handle.toLowerCase();
      if (key === author) continue;
      const u = byName.get(key);
      const same = u ? merged.get(u.id) : [...merged.values()].find((m) => (m.username || "").toLowerCase() === key);
      if (same) {
        if (!same.roles.includes("image")) same.roles.push("image");
        continue;
      }
      if (u && u.id === row.author_id) continue;
      const entry = u ? { ...u, roles: ["image"] } : { id: null, username: a.handle, name: a.name || null, roles: ["image"] };
      merged.set(u ? u.id : `image:${key}`, entry);
    }
  }
  for (const m of merged.values()) {
    m.roles.sort((a, b) => ROLE_ORDER.indexOf(a) - ROLE_ORDER.indexOf(b));
    m.role = m.roles[0];
  }
  const black = await db.prepare("SELECT reason, note, added_at FROM blacklist WHERE user_id = ?").bind(row.author_id).first();
  // 公開直後は表がまだないことがある(次の収集で作られる)ので、読めなければツリーなしで出す
  const tree = await getTree(db, id, row.author_id).catch(() => []);
  const { media_json, score_json, link_found, ...rest } = row;
  return {
    ...rest,
    ...profileInfo({ ...row, link: parseJson(link_found, null) }),
    opened: !!row.opened,
    ...shootArea(row),
    media: parseJson(media_json, []),
    score_detail: parseJson(score_json, []),
    people: [...merged.values()],
    blacklist: black || null,
    tree,
  };
}

// 遡った返信元・引用元(ツリー)を古い順に返す。relation は root(いちばん上)/ reply(返信)/ quote(引用)。
// by_client は投稿者が依頼主本人か。遡りが済んでいなければ空。
async function getTree(db, id, authorId) {
  const t = await db.prepare("SELECT ids_json FROM post_trees WHERE post_id = ?").bind(id).first();
  const ids = parseJson(t && t.ids_json, []).filter((x) => typeof x === "string").slice(0, 20);
  if (!ids.length) return [];
  const { results } = await db
    .prepare(
      `SELECT p.id, p.text, p.url, p.created_at, p.author_id, p.reply_to_id, p.quoted_id, p.media_json, u.username, u.name
       FROM posts p LEFT JOIN users u ON u.id = p.author_id WHERE p.id IN (${ids.map(() => "?").join(", ")})`,
    )
    .bind(...ids)
    .all();
  const byId = new Map(results.map((r) => [r.id, r]));
  return ids
    .filter((x) => byId.has(x))
    .map((x, i) => {
      const { media_json, reply_to_id, quoted_id, author_id, ...r } = byId.get(x);
      return {
        ...r,
        relation: i === 0 ? "root" : reply_to_id ? "reply" : quoted_id ? "quote" : "root",
        by_client: author_id === authorId,
        media: parseJson(media_json, []),
      };
    });
}

// データベースの中身を見る画面(読むだけ)。見せる表はここに並べたものだけ
const DB_TABLES = ["posts", "judgments", "reviews", "blacklist", "users", "user_links", "user_aliases", "post_users", "image_reads", "post_trees", "missing_posts", "runs", "costs", "meta"];
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

// カードの状態を変える。opened=true は「開いた」の記録で、状態は変えない。
// 新着に戻すときは行を消さずに状態だけ戻す(一度でも状態を変えた案件は、開いたときの自動の仕分けにかけない: touched)。
// 新しく作る行には opened_at を入れない(開いただけの前の行と見分けるため)
function reviewWrite(db, id, state, now) {
  if (state === "new") return db.prepare("UPDATE reviews SET state = 'new', updated_at = ? WHERE post_id = ?").bind(now, id);
  return db
    .prepare(`INSERT INTO reviews (post_id, state, opened_at, updated_at) VALUES (?, ?, NULL, ?)
              ON CONFLICT(post_id) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at`)
    .bind(id, state, now);
}

export async function setReview(db, id, { state, opened }) {
  const exists = await db.prepare("SELECT 1 AS ok FROM judgments WHERE post_id = ?").bind(id).first();
  if (!exists) return null;
  const now = nowIso();
  if (opened) {
    await db
      .prepare(`INSERT INTO reviews (post_id, state, opened_at, updated_at) VALUES (?, 'new', ?, ?)
                ON CONFLICT(post_id) DO UPDATE SET opened_at = COALESCE(reviews.opened_at, excluded.opened_at)`)
      .bind(id, now, now)
      .run();
  } else {
    state = normState(state);
    if (!REVIEW_STATES.includes(state)) throw new RangeError("state");
    await reviewWrite(db, id, state, now).run();
  }
  const row = await db.prepare("SELECT state FROM reviews WHERE post_id = ?").bind(id).first();
  return REVIEW_STATES.includes(row?.state) ? row.state : "new";
}

// まとめて状態を変える(ホームの「仕分け」から)。ids は 300 件まで。見つからない ID は飛ばす。
const BULK_MAX = 300;
export async function setReviews(db, body) {
  const ids = [...new Set(Array.isArray(body.ids) ? body.ids.map(String) : [])];
  if (!ids.length || ids.length > BULK_MAX || ids.some((id) => !/^\d{1,25}$/.test(id))) throw new RangeError("ids");
  const state = normState(body.state);
  if (!REVIEW_STATES.includes(state)) throw new RangeError("state");
  const known = new Set();
  for (let i = 0; i < ids.length; i += 90) {
    const part = ids.slice(i, i + 90);
    const { results } = await db
      .prepare(`SELECT post_id FROM judgments WHERE post_id IN (${part.map(() => "?").join(", ")})`)
      .bind(...part)
      .all();
    for (const r of results) known.add(r.post_id);
  }
  const hit = ids.filter((id) => known.has(id));
  if (!hit.length) return { updated: [] };
  const now = nowIso();
  await db.batch(hit.map((id) => reviewWrite(db, id, state, now)));
  return { updated: hit, state };
}

// 「仕分け」: チェックした値に当てはまる新着を削除予定にする。rule は { 項目: [チェックした値] }
// 値は画面が案件から作る文字(「東京都」「記載なし」など)なので、ここでは形と長さだけ確かめる
// score は「この点数以下」の数字を1つだけ入れる(["-50"] のような形)
const SORT_FACETS = ["gender", "age", "area", "shoot", "plan", "score", "type"];
const SORT_VALUES_MAX = 60;
const PRESET_MAX = 20;
function cleanRule(rule) {
  if (!rule || typeof rule !== "object" || Array.isArray(rule)) return null;
  const out = {};
  for (const k of SORT_FACETS) {
    if (rule[k] === undefined) continue;
    if (!Array.isArray(rule[k])) return null;
    const vals = [...new Set(rule[k].map((v) => String(v ?? "").trim().slice(0, 30)).filter(Boolean))].slice(0, SORT_VALUES_MAX);
    if (vals.length) out[k] = vals;
  }
  return out;
}
function cleanSortPresets(list) {
  if (!Array.isArray(list)) return null;
  const out = [];
  for (const p of list.slice(0, PRESET_MAX)) {
    const name = String(p?.name || "").trim().slice(0, 20);
    const rule = cleanRule(p?.rule);
    if (!name || !rule) return null;
    out.push({ name, rule });
  }
  return out;
}

export async function getSettings(db) {
  const row = await db.prepare("SELECT value FROM meta WHERE key = ?").bind(SETTINGS_KEY).first();
  const saved = parseJson(row?.value, {});
  const out = { ...DEFAULT_SETTINGS };
  for (const k of Object.keys(DEFAULT_SETTINGS)) if (SWIPE_CHOICES.includes(saved[k])) out[k] = saved[k];
  // タブごとの設定がまだない(前からの)保存は、どのタブも右・左の設定と同じにする
  out.swipe_tabs = Object.fromEntries(REVIEW_STATES.map((tab) => {
    const t = saved.swipe_tabs?.[tab] || {};
    const pick = (v, fallback) => swipeFor(tab, SWIPE_CHOICES.includes(v) ? v : fallback);
    return [tab, { right: pick(t.right, out.swipe_right), left: pick(t.left, out.swipe_left) }];
  }));
  out.sort_rule = cleanRule(saved.sort_rule) || {};
  out.sort_auto = saved.sort_auto === true;
  out.sort_presets = cleanSortPresets(saved.sort_presets) || [];
  // 選んでいるプリセットは1つだけ。消したプリセットの名前は残さない
  // まだ一度も選んでいない(この項目ができる前の)保存は、今のチェックと同じプリセットを選んでいることにする
  const key = (r) => JSON.stringify(SORT_FACETS.map((k) => [...(r[k] || [])].sort()));
  const active = saved.sort_active === undefined
    ? out.sort_presets.find((p) => key(p.rule) === key(out.sort_rule))?.name
    : saved.sort_active;
  out.sort_active = out.sort_presets.some((p) => p.name === active) ? active : null;
  return out;
}

export async function saveSettings(db, body) {
  const next = await getSettings(db);
  for (const k of Object.keys(DEFAULT_SETTINGS)) {
    if (body[k] === undefined) continue;
    if (!SWIPE_CHOICES.includes(body[k])) throw new RangeError(k);
    next[k] = body[k];
  }
  if (body.swipe_tabs !== undefined) {
    if (!body.swipe_tabs || typeof body.swipe_tabs !== "object") throw new RangeError("swipe_tabs");
    for (const [tab, t] of Object.entries(body.swipe_tabs)) {
      if (!REVIEW_STATES.includes(tab) || !t || typeof t !== "object") throw new RangeError("swipe_tabs");
      for (const side of ["right", "left"]) {
        if (t[side] === undefined) continue;
        if (!SWIPE_CHOICES.includes(t[side])) throw new RangeError("swipe_tabs");
        next.swipe_tabs[tab][side] = swipeFor(tab, t[side]);
      }
    }
  }
  if (body.sort_rule !== undefined) {
    const rule = cleanRule(body.sort_rule);
    if (!rule) throw new RangeError("sort_rule");
    next.sort_rule = rule;
  }
  if (body.sort_auto !== undefined) {
    if (typeof body.sort_auto !== "boolean") throw new RangeError("sort_auto");
    next.sort_auto = body.sort_auto;
  }
  if (body.sort_presets !== undefined) {
    const presets = cleanSortPresets(body.sort_presets);
    if (!presets) throw new RangeError("sort_presets");
    next.sort_presets = presets;
  }
  if (body.sort_active !== undefined) {
    if (body.sort_active !== null && typeof body.sort_active !== "string") throw new RangeError("sort_active");
    next.sort_active = body.sort_active ? body.sort_active.trim().slice(0, 20) || null : null;
  }
  if (!next.sort_presets.some((p) => p.name === next.sort_active)) next.sort_active = null;
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

  // 日ごと: 取得ポスト数(runs)、判定数・対象(judgments)、費用(costs)
  const fetched = await all(`SELECT ${JST_DAY("started_at")} AS d, SUM(fetched) AS fetched, SUM(new_posts) AS new_posts, COUNT(*) AS runs
    FROM runs WHERE started_at >= ? AND status = 'ok' GROUP BY d`, since);
  const judged = await all(`SELECT ${JST_DAY("j.judged_at")} AS d, COUNT(*) AS judged,
      SUM(CASE WHEN j.reject_reason IS NULL THEN 1 ELSE 0 END) AS target
    FROM judgments j WHERE j.judged_at >= ? GROUP BY d`, since);
  const costs = await all(`SELECT ${JST_DAY("at")} AS d,
      SUM(CASE WHEN service = 'twitterapi.io' THEN usd ELSE 0 END) AS collect_usd,
      SUM(CASE WHEN service = 'twitterapi.io' THEN 0 ELSE usd END) AS ai_usd
    FROM costs WHERE at >= ? GROUP BY d`, since);
  const byDay = new Map();
  for (let i = days - 1; i >= 0; i--) {
    const d = jstDay(i);
    byDay.set(d, { date: d, fetched: 0, new_posts: 0, judged: 0, target: 0, collect_yen: 0, ai_yen: 0 });
  }
  for (const r of fetched) if (byDay.has(r.d)) Object.assign(byDay.get(r.d), { fetched: r.fetched || 0, new_posts: r.new_posts || 0 });
  for (const r of judged) if (byDay.has(r.d)) Object.assign(byDay.get(r.d), { judged: r.judged || 0, target: r.target || 0 });
  for (const r of costs) if (byDay.has(r.d)) Object.assign(byDay.get(r.d), { collect_yen: (r.collect_usd || 0) * rate, ai_yen: (r.ai_usd || 0) * rate });
  const daily = [...byDay.values()].map((d) => ({ ...d, collect_yen: Math.round(d.collect_yen * 100) / 100, ai_yen: Math.round(d.ai_yen * 100) / 100 }));

  // 累計(期間に関係なく、これまでの全部)
  const totals = await db.prepare(`SELECT
      (SELECT COUNT(*) FROM posts WHERE source = 'search') AS collected,
      (SELECT COUNT(*) FROM posts WHERE status = 'skipped') AS skipped,
      (SELECT COUNT(*) FROM judgments) AS judged,
      (SELECT COUNT(*) FROM judgments WHERE reject_reason IS NULL) AS target,
      (SELECT COALESCE(SUM(usd), 0) FROM costs) AS usd,
      (SELECT MIN(started_at) FROM runs) AS first_run`).first();
  const reviews = await all("SELECT state, COUNT(*) AS n FROM reviews GROUP BY state");

  // 今月と先月の比べ(先月は同じ日付まで)
  const month = async (from, to) => db.prepare(`SELECT
      (SELECT COALESCE(SUM(fetched), 0) FROM runs WHERE started_at >= ?1 AND started_at < ?2) AS fetched,
      (SELECT COUNT(*) FROM judgments WHERE judged_at >= ?1 AND judged_at < ?2) AS judged,
      (SELECT COUNT(*) FROM judgments WHERE judged_at >= ?1 AND judged_at < ?2 AND reject_reason IS NULL) AS target,
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
    if (url.pathname === "/api/reviews" && request.method === "POST") {
      if (!sameOriginWrite(request, url)) return text("このページからだけ変更できます。", 403);
      try {
        await ensureTables(env.DB);
        return json(await setReviews(env.DB, await request.json().catch(() => ({}))));
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
      if (url.pathname === "/api/cases") return json({ cases: await listCases(env.DB, url.searchParams), area_choices: AREA_CHOICES });
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
      if (url.pathname === "/api/calendar") {
        try {
          const config = parseJson((await env.DB.prepare("SELECT value FROM meta WHERE key = 'config_last_good'").first())?.value, {});
          return json(await getCalendar(env, url.searchParams, fetch, Date.now(), offWords(config.basic?.["休みとみなす予定の言葉"])));
        } catch (e) {
          console.error(e);
          return json({ configured: true, error: "Google カレンダーを読めませんでした", days: {} });
        }
      }
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
