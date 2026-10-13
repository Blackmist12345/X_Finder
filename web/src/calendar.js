// N9 カレンダー照合: Google カレンダーの「iCal 形式の非公開 URL」を読み、日ごとの予定にする。
// URL は Worker の secret GCAL_ICS_URL に入れる(Cloudflare のダッシュボードで。複数なら改行かカンマで区切る)。
// 予定はこの Worker の中で読むだけで、D1 にもリポジトリにも保存しない。
//
// 時刻はすべて日本時間の「壁の時計」で扱う(Date の UTC の欄を日本時間として使う)。

const JST_MS = 9 * 3600e3;
const DAY_MS = 864e5;
const MAX_DAYS = 120; // 一度に返す日数の上限
const MAX_SPAN_DAYS = 31; // 何日も続く予定は、この日数までを数える
const MAX_STEPS = 5000; // 繰り返し予定を展開するときの上限(暴走しないように)
const CACHE_MS = 10 * 60e3;
const WEEKDAYS = ["SU", "MO", "TU", "WE", "TH", "FR", "SA"];

// ---- iCal(.ics)を読む ----

function unfold(text) {
  return text.replace(/\r\n/g, "\n").replace(/\n[ \t]/g, "").split("\n");
}

function unescapeText(v) {
  return v.replace(/\\n/gi, " ").replace(/\\([,;\\])/g, "$1").trim();
}

// "20261025T130000Z" / "20261025T130000"(TZID 付き・なし)/ "20261025"(終日)を、日本時間の壁の時計にする
export function parseIcsDate(value, params = "") {
  const m = /^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})?(Z)?)?$/.exec(value.trim());
  if (!m) return null;
  const [, y, mo, d, h, mi, s, z] = m;
  const allDay = h === undefined || /VALUE=DATE(?!-)/.test(params);
  let ms = Date.UTC(+y, +mo - 1, +d, +(h || 0), +(mi || 0), +(s || 0));
  if (z) ms += JST_MS; // UTC → 日本時間(TZID 付きや時差なしの時刻は、日本時間として扱う)
  return { ms, allDay };
}

export function parseIcs(text) {
  const events = [];
  let ev = null;
  for (const line of unfold(text)) {
    if (line === "BEGIN:VEVENT") { ev = { exdates: [] }; continue; }
    if (line === "END:VEVENT") { if (ev && ev.start) events.push(ev); ev = null; continue; }
    if (!ev) continue;
    const i = line.indexOf(":");
    if (i < 0) continue;
    const [name, ...rest] = line.slice(0, i).split(";");
    const params = rest.join(";");
    const value = line.slice(i + 1);
    switch (name.toUpperCase()) {
      case "SUMMARY": ev.title = unescapeText(value); break;
      case "DTSTART": ev.start = parseIcsDate(value, params); break;
      case "DTEND": ev.end = parseIcsDate(value, params); break;
      case "DURATION": ev.duration = parseDuration(value); break;
      case "RRULE": ev.rrule = Object.fromEntries(value.split(";").map((p) => p.split("=")).map(([k, v]) => [k.toUpperCase(), v])); break;
      case "EXDATE": for (const v of value.split(",")) { const d = parseIcsDate(v, params); if (d) ev.exdates.push(d.ms); } break;
      case "RECURRENCE-ID": ev.recurrenceId = parseIcsDate(value, params); break;
      case "UID": ev.uid = value.trim(); break;
      case "STATUS": ev.cancelled = value.trim().toUpperCase() === "CANCELLED"; break;
      case "TRANSP": ev.free = value.trim().toUpperCase() === "TRANSPARENT"; break;
    }
  }
  return events;
}

function parseDuration(v) {
  const m = /^P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$/.exec(v.trim());
  if (!m) return null;
  const [, w, d, h, mi, s] = m.map((x) => +(x || 0));
  return ((w * 7 + d) * 24 * 3600 + h * 3600 + mi * 60 + s) * 1000;
}

// ---- 繰り返し予定を展開する ----

function untilMs(v) {
  const d = v && parseIcsDate(v);
  if (!d) return Infinity;
  return d.allDay ? d.ms + DAY_MS - 1 : d.ms;
}

function nthWeekdayOfMonth(y, mo, weekday, n) {
  if (n > 0) {
    const first = new Date(Date.UTC(y, mo, 1)).getUTCDay();
    const day = 1 + ((weekday - first + 7) % 7) + (n - 1) * 7;
    return day <= daysIn(y, mo) ? day : null;
  }
  const lastDay = daysIn(y, mo);
  const last = new Date(Date.UTC(y, mo, lastDay)).getUTCDay();
  const day = lastDay - ((last - weekday + 7) % 7) + (n + 1) * 7;
  return day >= 1 ? day : null;
}

const daysIn = (y, mo) => new Date(Date.UTC(y, mo + 1, 0)).getUTCDate();

// 予定の開始時刻(壁の時計の ms)を、windowEnd までの分だけ順に返す
export function occurrences(ev, windowEnd) {
  const start = ev.start.ms;
  const r = ev.rrule;
  if (!r) return [start];
  const freq = (r.FREQ || "").toUpperCase();
  const interval = Math.max(1, +r.INTERVAL || 1);
  const count = r.COUNT ? +r.COUNT : Infinity;
  const until = Math.min(untilMs(r.UNTIL), windowEnd);
  const timeOfDay = start % DAY_MS;
  const s = new Date(start);
  const byday = (r.BYDAY || "").split(",").filter(Boolean).map((t) => {
    const m = /^([+-]?\d+)?(SU|MO|TU|WE|TH|FR|SA)$/i.exec(t.trim());
    return m ? { n: m[1] ? +m[1] : 0, wd: WEEKDAYS.indexOf(m[2].toUpperCase()) } : null;
  }).filter(Boolean);
  const bymonthday = (r.BYMONTHDAY || "").split(",").filter(Boolean).map(Number);
  const out = [];
  let n = 0;
  const push = (ms) => {
    if (ms < start || n >= count) return;
    n++;
    if (ms <= until) out.push(ms);
  };
  for (let step = 0; step < MAX_STEPS && n < count; step++) {
    let period = [];
    if (freq === "DAILY") {
      period = [start + step * interval * DAY_MS];
    } else if (freq === "WEEKLY") {
      // 週は月曜はじまり(WKST=MO)
      const weekStart = start - timeOfDay - ((s.getUTCDay() + 6) % 7) * DAY_MS + step * interval * 7 * DAY_MS;
      const days = byday.length ? byday.map((b) => b.wd) : [s.getUTCDay()];
      period = days.map((wd) => weekStart + ((wd + 6) % 7) * DAY_MS + timeOfDay).sort((a, b) => a - b);
    } else if (freq === "MONTHLY") {
      const total = s.getUTCMonth() + step * interval;
      const y = s.getUTCFullYear() + Math.floor(total / 12), mo = total % 12;
      let days = [];
      if (byday.length) days = byday.map((b) => (b.n ? nthWeekdayOfMonth(y, mo, b.wd, b.n) : null)).filter(Boolean);
      else if (bymonthday.length) days = bymonthday.map((d) => (d < 0 ? daysIn(y, mo) + d + 1 : d)).filter((d) => d >= 1 && d <= daysIn(y, mo));
      else if (s.getUTCDate() <= daysIn(y, mo)) days = [s.getUTCDate()];
      period = days.sort((a, b) => a - b).map((d) => Date.UTC(y, mo, d) + timeOfDay);
    } else if (freq === "YEARLY") {
      const y = s.getUTCFullYear() + step * interval;
      if (s.getUTCDate() <= daysIn(y, s.getUTCMonth())) period = [Date.UTC(y, s.getUTCMonth(), s.getUTCDate()) + timeOfDay];
    } else {
      return [start]; // 知らない繰り返し方は、最初の 1 回だけ
    }
    if (!period.length) continue;
    if (period[0] > until) break;
    period.forEach(push);
  }
  return out;
}

// ---- 日ごとにまとめる ----

const ymd = (ms) => new Date(ms).toISOString().slice(0, 10);
const hm = (ms) => new Date(ms).toISOString().slice(11, 16);

// events を [from, to](日本時間の "YYYY-MM-DD")の日ごとの予定 { "2026-10-25": [{ title, time }] } にする
export function eventsByDay(events, from, to) {
  const lo = Date.parse(`${from}T00:00:00Z`), hi = Date.parse(`${to}T00:00:00Z`) + DAY_MS;
  // 1 回だけ変更・取り消しされた繰り返し予定は、元の回を外す
  const moved = new Map();
  for (const ev of events) {
    if (ev.uid && ev.recurrenceId) {
      if (!moved.has(ev.uid)) moved.set(ev.uid, new Set());
      moved.get(ev.uid).add(ev.recurrenceId.ms);
    }
  }
  const days = {};
  for (const ev of events) {
    if (ev.cancelled || !ev.start) continue;
    const length = ev.end ? Math.max(0, ev.end.ms - ev.start.ms)
      : ev.duration != null ? ev.duration : ev.start.allDay ? DAY_MS : 0;
    const skip = new Set(ev.exdates);
    const movedSet = !ev.recurrenceId && ev.uid ? moved.get(ev.uid) : null;
    for (const at of occurrences(ev, hi)) {
      if (skip.has(at) || (movedSet && movedSet.has(at))) continue;
      const end = at + length;
      if (at >= hi || Math.max(end, at + 1) <= lo) continue;
      const allDay = ev.start.allDay;
      const time = allDay ? null : `${hm(at)}${length ? `–${hm(end)}` : ""}`;
      // 何日も続く予定は、かかる日すべてに出す(終日の終わりの日は含まない)
      let day = at - (at % DAY_MS);
      const last = allDay ? end - 1 : Math.max(at, end - 1);
      for (let i = 0; i < MAX_SPAN_DAYS && day <= last; i++, day += DAY_MS) {
        if (day < lo || day >= hi) continue;
        (days[ymd(day)] ||= []).push({ title: ev.title || "(タイトルなし)", time, free: !!ev.free });
      }
    }
  }
  for (const list of Object.values(days)) list.sort((a, b) => (a.time || "").localeCompare(b.time || ""));
  return days;
}

// ---- Worker から使う ----

let cache = null; // { key, at, events }

export function calendarUrls(env) {
  return String(env.GCAL_ICS_URL || "").split(/[\s,]+/).filter((u) => /^https:\/\/\S+$/.test(u));
}

// 予定名にこの言葉が入っていれば「休み」(予定ありに数えない)。Notion【7】の「休みとみなす予定の言葉」で変えられる
export const DEFAULT_OFF_WORDS = ["休み", "有給", "休日", "公休", "代休", "振休"];

export function offWords(value) {
  if (typeof value !== "string" || !value.trim()) return DEFAULT_OFF_WORDS;
  return value.split(/[,、，\s]+/).map((w) => w.trim()).filter(Boolean);
}

export function markOff(days, words) {
  for (const list of Object.values(days)) {
    for (const ev of list) if (words.some((w) => ev.title.includes(w))) ev.off = true;
  }
  return days;
}

export async function getCalendar(env, params, fetchFn = fetch, now = Date.now(), words = DEFAULT_OFF_WORDS) {
  const urls = calendarUrls(env);
  if (!urls.length) return { configured: false, days: {} };
  const today = ymd(now + JST_MS);
  const from = /^\d{4}-\d{2}-\d{2}$/.test(params.get("from") || "") ? params.get("from") : today;
  let to = /^\d{4}-\d{2}-\d{2}$/.test(params.get("to") || "") ? params.get("to") : ymd(Date.parse(`${from}T00:00:00Z`) + 90 * DAY_MS);
  const maxTo = ymd(Date.parse(`${from}T00:00:00Z`) + MAX_DAYS * DAY_MS);
  if (to > maxTo) to = maxTo;
  const key = urls.join(" ");
  if (!cache || cache.key !== key || now - cache.at > CACHE_MS) {
    const texts = await Promise.all(urls.map(async (u) => {
      const res = await fetchFn(u, { headers: { accept: "text/calendar" } });
      if (!res.ok) throw new Error(`カレンダーを読めませんでした(${res.status})`);
      return res.text();
    }));
    cache = { key, at: now, events: texts.flatMap(parseIcs) };
  }
  return { configured: true, from, to, days: markOff(eventsByDay(cache.events, from, to), words) };
}

export function resetCalendarCache() {
  cache = null;
}
