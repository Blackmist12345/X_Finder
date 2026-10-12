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
    // D1 の batch はまとめて 1 回で、途中で失敗したら全部取り消す
    async batch(stmts) {
      db.exec("BEGIN");
      try { const out = []; for (const st of stmts) out.push(await st.run()); db.exec("COMMIT"); return out; }
      catch (e) { db.exec("ROLLBACK"); throw e; }
    },
    prepare(q) {
      let args = [];
      const stmt = db.prepare(q);
      const api = {
        bind(...a) { args = a; return api; },
        async all() { return { results: stmt.all(...args).map((r) => ({ ...r })) }; },
        async first() { const r = stmt.get(...args); return r ? { ...r } : null; },
        async run() { const r = stmt.run(...args); return { meta: { changes: Number(r.changes) } }; },
      };
      return api;
    },
  };
}
