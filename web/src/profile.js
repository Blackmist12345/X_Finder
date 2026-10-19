// 依頼主のプロフィール(名前・自己紹介・所在地)と、プロフィールのリンク先のページの文字から、年齢と活動地域を読み取る。
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

// ---- 性別 ----
// はっきり書いてあるときだけ読む(「女性カメラマン募集」のような書き方は拾わない)
const FEMALE = /性別\s*[:：]?\s*(?:女|♀|female)|♀|女性です|女です|女子(?:中学|高校?|大学?)生|(?<![A-Za-z])(?:JK|JC|JD)[1-4]?(?![A-Za-z])/i;
const MALE = /性別\s*[:：]?\s*(?:男|♂|male)|♂|男性です|男です|男子(?:中学|高校?|大学?)生|(?<![A-Za-z])(?:DK|DC)[1-4]?(?![A-Za-z])/i;

// 返り値: "女性" / "男性" / null(書いてない・両方ある)
export function parseGender(text) {
  const t = normalize(text);
  if (!t) return null;
  const f = FEMALE.test(t);
  const m = MALE.test(t);
  if (f === m) return null;
  return f ? "女性" : "男性";
}

// ---- 活動地域 ----

export const PREFECTURES = [
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
// よく書かれる市の名前と、その都道府県。市はそのまま1つの地域として出す(仕分けで市ごとに選べるように)
const CITIES = [
  ["札幌", "北海道"], ["仙台", "宮城県"], ["宇都宮", "栃木県"], ["高崎", "群馬県"], ["横浜", "神奈川県"], ["川崎", "神奈川県"],
  ["相模原", "神奈川県"], ["金沢", "石川県"], ["浜松", "静岡県"], ["名古屋", "愛知県"], ["堺", "大阪府"], ["神戸", "兵庫県"],
  ["姫路", "兵庫県"], ["倉敷", "岡山県"], ["北九州", "福岡県"], ["博多", "福岡県"], ["那覇", "沖縄県"],
];
const CITY_PREF = Object.fromEntries(CITIES);
// 都道府県名の一部や、都道府県の言いかえ → 都道府県
const PLACES = [
  ...PREFECTURES.map((p) => [p, p]),
  ...PREFECTURES.filter((p) => p !== "北海道").map((p) => [p.slice(0, -1), p]),
  ["都内", "東京都"], ["23区", "東京都"], ["道民", "北海道"], ["道内", "北海道"],
  ...CITIES.map(([c]) => [c, c]),
];
// 仕分けの画面に最初から並べる活動地域。都道府県は地方ごとのまとまりで並べる
export const AREA_CHOICES = {
  regions: REGIONS.map(([name]) => name),
  cities: CITIES.map(([c]) => c),
  blocks: [
    ["北海道・東北", PREFECTURES.slice(0, 7)], ["関東", PREFECTURES.slice(7, 14)], ["中部", PREFECTURES.slice(14, 23)],
    ["近畿", PREFECTURES.slice(23, 30)], ["中国・四国", PREFECTURES.slice(30, 39)], ["九州・沖縄", PREFECTURES.slice(39)],
  ],
};
// 先に伏せておく言葉(「東京都」の中の「京都」、「京都府」と「京都」の二重取りなど)
const MASK = ["東京都", "京都府", "大阪府"];

// 返り値: 書かれていた順の地域の配列(都道府県名・地方名・市の名前)。例: ["関西"], ["愛知県", "東京都"], ["名古屋"]
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
  // 長い名前から先に探す(「北九州」を「九州」より先に、「堺」は「堺市」も含めて、など)
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

// 関東(1都6県)。撮影場所と活動地域のどちらも、関東かどうかで分ける
export const KANTO = ["東京都", "神奈川県", "埼玉県", "千葉県", "茨城県", "栃木県", "群馬県"];
// 活動地域として関東に入るもの(首都圏は関東の中なので、こちらに入れる)。市は都道府県で数える
const KANTO_AREAS = [...KANTO, "首都圏", "関東"];
const asPref = (a) => CITY_PREF[a] || a;
// 4都県(東京・神奈川・埼玉・千葉)(今の画面では使っていない。仕分けは都道府県の値そのもので選ぶ)
export const METRO = ["東京都", "神奈川県", "埼玉県", "千葉県"];
// 活動地域として4都県に入るもの。「首都圏」「関東」とだけ書いている人も入れる
const METRO_AREAS = [...METRO, "首都圏", "関東"];

// プロフィールに書いてない項目だけ、リンク先のページ(Litlink など)から読み取った結果を使う。
// link は収集のとき finder/link_step.py がリンク先から読んだ結果 { age, areas, gender }(ページの文字は保存しない)。
// *_from は "profile"(プロフィールから) / "link"(リンク先から)
export function profileInfo(user) {
  const text = [user.name, user.description, user.location].filter(Boolean).join("\n");
  const areaText = [user.location, user.description].filter(Boolean).join("\n");
  const link = user.link || {};
  const pick = (own, fromLink, empty) => {
    if (!empty(own)) return [own, "profile"];
    if (!empty(fromLink)) return [fromLink, "link"];
    return [own, null];
  };
  const none = (v) => !v;
  const [age, ageFrom] = pick(parseAge(text), link.age || null, none);
  const [areas, areasFrom] = pick(parseAreas(areaText), Array.isArray(link.areas) ? link.areas : [], (v) => !v.length);
  const [gender, genderFrom] = pick(parseGender(text), link.gender || null, none);
  return {
    age,
    areas,
    gender,
    age_from: ageFrom,
    areas_from: areasFrom,
    gender_from: genderFrom,
    areas_kanto: areas.some((a) => KANTO_AREAS.includes(asPref(a))),
    areas_far: areas.length > 0 && !areas.some((a) => KANTO_AREAS.includes(asPref(a))),
    areas_metro: areas.some((a) => METRO_AREAS.includes(asPref(a))),
  };
}

// リンク先のページの文字から、保存する結果だけを作る(finder/link_step.py が node で呼ぶ)
export function readLinkText(text) {
  return { age: parseAge(text), areas: parseAreas(text), gender: parseGender(text) };
}
