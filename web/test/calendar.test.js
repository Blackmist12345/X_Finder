import assert from "node:assert/strict";
import { test } from "node:test";

import { eventsByDay, getCalendar, parseIcs, resetCalendarCache } from "../src/calendar.js";

const ICS = `BEGIN:VCALENDAR
BEGIN:VEVENT
UID:a
DTSTART:20261025T040000Z
DTEND:20261025T080000Z
SUMMARY:別の撮影\\, 渋谷
END:VEVENT
BEGIN:VEVENT
UID:b
DTSTART;VALUE=DATE:20261101
DTEND;VALUE=DATE:20261104
SUMMARY:旅行
END:VEVENT
BEGIN:VEVENT
UID:c
DTSTART;TZID=Asia/Tokyo:20261005T190000
DTEND;TZID=Asia/Tokyo:20261005T200000
RRULE:FREQ=WEEKLY;BYDAY=MO
EXDATE;TZID=Asia/Tokyo:20261026T190000
SUMMARY:ジム
END:VEVENT
BEGIN:VEVENT
UID:c
RECURRENCE-ID;TZID=Asia/Tokyo:20261019T190000
DTSTART;TZID=Asia/Tokyo:20261020T190000
DTEND;TZID=Asia/Tokyo:20261020T200000
SUMMARY:ジム(火曜に移動)
END:VEVENT
BEGIN:VEVENT
UID:d
DTSTART:20261026T000000Z
SUMMARY:取り消し
STATUS:CANCELLED
END:VEVENT
BEGIN:VEVENT
UID:e
DTSTART;TZID=Asia/Tokyo:20260110T100000
DTEND;TZID=Asia/Tokyo:20260110T110000
RRULE:FREQ=MONTHLY;BYDAY=2SA
SUMMARY:定例
END:VEVENT
END:VCALENDAR`;

test("予定を日本時間の日ごとにまとめる(終日・何日も続く・繰り返し・除外・移動・取り消し)", () => {
  const days = eventsByDay(parseIcs(ICS), "2026-10-01", "2026-11-30");
  assert.deepEqual(days["2026-10-25"], [{ title: "別の撮影, 渋谷", time: "13:00–17:00", free: false }]);
  assert.deepEqual(Object.keys(days).filter((d) => days[d].some((e) => e.title === "旅行")), ["2026-11-01", "2026-11-02", "2026-11-03"]);
  const gym = Object.keys(days).filter((d) => days[d].some((e) => e.title.startsWith("ジム"))).sort();
  assert.deepEqual(gym, ["2026-10-05", "2026-10-12", "2026-10-20", "2026-11-02", "2026-11-09", "2026-11-16", "2026-11-23", "2026-11-30"]);
  assert.equal(days["2026-10-20"][0].title, "ジム(火曜に移動)");
  assert.equal(days["2026-10-26"], undefined);
  assert.deepEqual(Object.keys(days).filter((d) => days[d].some((e) => e.title === "定例")), ["2026-10-10", "2026-11-14"]);
});

test("URL がなければ未設定、あれば読んで短い間は覚えておく", async () => {
  resetCalendarCache();
  assert.deepEqual(await getCalendar({}, new URLSearchParams()), { configured: false, days: {} });
  let calls = 0;
  const fetchFn = async (url) => { calls++; assert.equal(url, "https://calendar.google.com/x/basic.ics"); return new Response(ICS); };
  const env = { GCAL_ICS_URL: "https://calendar.google.com/x/basic.ics" };
  const now = Date.parse("2026-10-20T00:00:00Z");
  const r = await getCalendar(env, new URLSearchParams("from=2026-10-20&to=2026-10-31"), fetchFn, now);
  assert.equal(r.configured, true);
  assert.deepEqual(Object.keys(r.days).sort(), ["2026-10-20", "2026-10-25"]);
  await getCalendar(env, new URLSearchParams("from=2026-10-20&to=2026-10-31"), fetchFn, now + 60e3);
  assert.equal(calls, 1);
  // 長すぎる期間は 120 日までにする
  const long = await getCalendar(env, new URLSearchParams("from=2026-10-01&to=2027-12-31"), fetchFn, now + 60e3);
  assert.equal(long.to, "2027-01-29");
  resetCalendarCache();
  await assert.rejects(getCalendar(env, new URLSearchParams(), async () => new Response("no", { status: 404 }), now));
});

test("「休み」「有給」などの予定には off を付ける(言葉は設定で変えられる)", async () => {
  const { markOff, offWords } = await import("../src/calendar.js");
  const days = { "2026-10-25": [{ title: "有給", time: null, free: false }, { title: "撮影", time: "13:00–17:00", free: false }] };
  markOff(days, offWords(undefined));
  assert.deepEqual(days["2026-10-25"].map((e) => !!e.off), [true, false]);
  assert.deepEqual(offWords("お休み、 リフレッシュ"), ["お休み", "リフレッシュ"]);
});
