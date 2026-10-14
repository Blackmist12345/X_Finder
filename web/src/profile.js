// 依頼主のプロフィール(名前・自己紹介・所在地)から、年齢と活動地域を読み取る。
// 決まった書き方を探すだけなので AI は使わない(費用なし・今ある案件にもすぐ効く)。

const ADULT_AGE = 18;

// 矢印や記号の書き方はいろいろあるので、まとめて「上」「下」に読み替える
const UP = "↑⬆⤴⇧⇑🔼⏫↗🆙";
const DOWN = "↓⬇⤵⇩⇓🔽⏬↘";
const UP_WORDS = "over|オーバー|以上|\\+|＋|上";
const DOWN_WORDS = "under|アンダー|以下|未満|下";

function normalize(text) {
  // 全角数字・記号をそろえ、絵文字の見た目を変えるだけの文字(異体字セレクタ)を消す
  return String(text || "").normalize("NFKC").replace(/[︎️]/g, "");
}

const AGE_ARROW = new RegExp(
  `(?<![0-9])([1-9][0-9])\\s*(?:歳|才)?\\s*(?:([${UP}]|${UP_WORDS})|([${DOWN}]|${DOWN_WORDS}))`,
  "iu",
);
const ADULT_WORDS = /成人済|成人して|成人です|成人組|義務教育(?:は)?(?:修了|終了|卒業)|社会人|大学生|院生|専門学生|アラサー|アラフォー|アラフィフ/;
const MINOR_WORDS = /未成年|中学生|高校生|女子高生|男子高生|(?<![A-Za-z])(?:JK|JC|DK|DC)[1-3]?(?![A-Za-z])|(?<![0-9一-龥])(?:中|高)[1-3一二三](?:です|の|\/|\s|$)/;
const DECADE = /(?<![0-9])([2-6]0)\s*代/;
const PLAIN_AGE = /(?<![0-9])(1[0-9]|[2-6][0-9])\s*(?:歳|才)(?![0-9])/;

// 返り値: { label: 画面に出す短い文字, minor: true(未成年)/false(成人)/null(分からない) } または null
export function parseAge(text) {
  const t = normalize(text);
  if (!t) return null;
  const m = AGE_ARROW.exec(t);
  if (m) {
    const n = Number(m[1]);
    if (m[2]) return { label: `${n}↑`, minor: n >= ADULT_AGE ? false : null };
    return { label: `${n}↓`, minor: n <= ADULT_AGE ? true : null };
  }
  const minor = MINOR_WORDS.exec(t);
  if (minor) return { label: "未成年", minor: true };
  const d = DECADE.exec(t);
  if (d) return { label: `${d[1]}代`, minor: false };
  if (ADULT_WORDS.test(t)) return { label: "成人", minor: false };
  const a = PLAIN_AGE.exec(t);
  if (a) {
    const n = Number(a[1]);
    return { label: `${n}歳`, minor: n < ADULT_AGE };
  }
  return null;
}

// ---- 活動地域 ----

const PREFECTURES = [
  "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県", "茨城県", "栃木県", "群馬県",
  "埼玉県", "千葉県", "東京都", "神奈川県", "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県",
  "岐阜県", "静岡県", "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県", "奈良県", "和歌山県",
  "鳥取県", "島根県", "岡山県", "広島県", "山口県", "徳島県", "香川県", "愛媛県", "高知県", "福岡県",
  "佐賀県", "長崎県", "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
];
// 地方の名前は、そのまま地域として出す
const REGIONS = [
  ["首都圏", /首都圏/], ["関東", /関東/], ["関西", /関西|近畿/], ["東海", /東海(?!オンエア)/], ["北陸", /北陸/],
  ["甲信越", /甲信越/], ["東北", /東北/], ["中国地方", /中国地方|山陽|山陰/], ["四国", /四国/], ["九州", /九州/],
];
// 都道府県名の一部や、よく書かれる市・地域の名前 → 都道府県
const PLACES = [
  ...PREFECTURES.map((p) => [p, p]),
  ...PREFECTURES.filter((p) => p !== "北海道").map((p) => [p.slice(0, -1), p]),
  ["都内", "東京都"], ["23区", "東京都"], ["道民", "北海道"], ["道内", "北海道"], ["札幌", "北海道"], ["仙台", "宮城県"],
  ["横浜", "神奈川県"], ["川崎", "神奈川県"], ["名古屋", "愛知県"], ["神戸", "兵庫県"], ["博多", "福岡県"],
  ["北九州", "福岡県"], ["浜松", "静岡県"], ["金沢", "石川県"], ["岡山", "岡山県"], ["那覇", "沖縄県"],
];
// 先に伏せておく言葉(「東京都」の中の「京都」、「京都府」と「京都」の二重取りなど)
const MASK = ["東京都", "京都府", "大阪府"];

// 返り値: 書かれていた順の地域の配列(都道府県名か地方名)。例: ["関西"], ["愛知県", "東京都"]
export function parseAreas(text) {
  let t = normalize(text);
  if (!t) return [];
  const found = [];
  const add = (name, at) => {
    if (!found.some((f) => f.name === name)) found.push({ name, at });
  };
  for (const w of MASK) {
    let i = t.indexOf(w);
    while (i >= 0) {
      add(w, i);
      t = t.slice(0, i) + "〓".repeat(w.length) + t.slice(i + w.length);
      i = t.indexOf(w);
    }
  }
  // 長い名前から先に探す(「北九州」を「九州」より先に、など)
  for (const [word, pref] of [...PLACES].sort((a, b) => b[0].length - a[0].length)) {
    let i = t.indexOf(word);
    while (i >= 0) {
      add(pref, i);
      t = t.slice(0, i) + "〓".repeat(word.length) + t.slice(i + word.length);
      i = t.indexOf(word);
    }
  }
  // 地方の名前は最後に探す(「北九州」は福岡県として伏せてあるので、九州にはならない)
  for (const [name, re] of REGIONS) {
    const m = re.exec(t);
    if (m) add(name, m.index);
  }
  return found.sort((a, b) => a.at - b.at).map((f) => f.name);
}

// 首都圏(東京・神奈川・埼玉・千葉)に関係する地域。関東は首都圏を含むので、こちらに入れる
const NEAR_AREAS = ["東京都", "神奈川県", "埼玉県", "千葉県", "首都圏", "関東"];

export function profileInfo(user) {
  const text = [user.name, user.description, user.location].filter(Boolean).join("\n");
  const areaText = [user.location, user.description].filter(Boolean).join("\n");
  const areas = parseAreas(areaText);
  return {
    age: parseAge(text),
    areas,
    areas_far: areas.length > 0 && !areas.some((a) => NEAR_AREAS.includes(a)),
  };
}
