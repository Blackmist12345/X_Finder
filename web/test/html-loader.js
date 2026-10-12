// テスト用: Worker と同じく .html を文字列、.png をバイト列として import できるようにする。
import { register } from "node:module";

register("data:text/javascript," + encodeURIComponent(`
  import { readFile } from "node:fs/promises";
  export async function load(url, context, next) {
    if (url.endsWith(".html")) {
      const source = await readFile(new URL(url), "utf8");
      return { format: "module", source: "export default " + JSON.stringify(source) + ";", shortCircuit: true };
    }
    if (url.endsWith(".png")) {
      const b64 = (await readFile(new URL(url))).toString("base64");
      return { format: "module", source: "export default Uint8Array.from(atob(" + JSON.stringify(b64) + "), (c) => c.charCodeAt(0)).buffer;", shortCircuit: true };
    }
    return next(url, context);
  }
`));
