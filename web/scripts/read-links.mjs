// finder/link_step.py から呼ばれる。標準入力の [{id, text}] を読み、[{id, age, areas, gender}] を標準出力に返す。
// 年齢・活動地域・性別の読み方を web/src/profile.js の 1 か所だけにしておくため、Python からこの node を呼ぶ。
import { readLinkText } from "../src/profile.js";

let input = "";
process.stdin.setEncoding("utf8");
for await (const chunk of process.stdin) input += chunk;
const out = JSON.parse(input || "[]").map(({ id, text }) => ({ id, ...readLinkText(text || "") }));
process.stdout.write(JSON.stringify(out));
