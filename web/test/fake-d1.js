// テスト用: Cloudflare D1 のバインディングを、Node の SQLite で同じ形に真似る(D1 も中身は SQLite)。
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";

export function fakeD1() {
  const db = new DatabaseSync(":memory:");
  const sql = readFileSync(new URL("../../schema.sql", import.meta.url), "utf8");
  const body = sql.split("\n").filter((l) => !l.trim().startsWith("--")).join("\n");
  for (const s of body.split(";").map((s) => s.trim()).filter(Boolean)) db.exec(s);
  return {
    raw: db,
    prepare(q) {
      let args = [];
      const stmt = db.prepare(q);
      const api = {
        bind(...a) { args = a; return api; },
        async all() { return { results: stmt.all(...args).map((r) => ({ ...r })) }; },
        async first() { const r = stmt.get(...args); return r ? { ...r } : null; },
      };
      return api;
    },
  };
}
