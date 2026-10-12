// テスト用: Worker と同じく .html を文字列として import できるようにする。
import { register } from "node:module";

register("data:text/javascript," + encodeURIComponent(`
  import { readFile } from "node:fs/promises";
  export async function load(url, context, next) {
    if (url.endsWith(".html")) {
      const source = await readFile(new URL(url), "utf8");
      return { format: "module", source: "export default " + JSON.stringify(source) + ";", shortCircuit: true };
    }
    return next(url, context);
  }
`));
