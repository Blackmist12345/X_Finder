-- X Finderのデータベース(Cloudflare D1)。N12 データ保存 / N15 ユーザー台帳。
-- 何度実行しても壊れないように IF NOT EXISTS で作る。

-- 収集したポスト。検索で見つけたもの(source=search)と、引用元として一緒に届いたもの(source=quoted)、
-- 返信元・引用元を root まで遡って取ったもの(source=tree)。
CREATE TABLE IF NOT EXISTS posts (
  id TEXT PRIMARY KEY,
  author_id TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'search',
  text TEXT,
  url TEXT,
  created_at TEXT,
  quoted_id TEXT,
  reply_to_id TEXT,
  conversation_id TEXT,
  media_json TEXT,
  penalty_json TEXT,
  status TEXT NOT NULL DEFAULT 'new',  -- new(判定待ち) / skipped(一次フィルタで除外) / context(引用元) / judged(対象) / rejected(AI 判定で対象外) / judge_error
  skip_reason TEXT,
  collected_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS posts_status ON posts (status);
CREATE INDEX IF NOT EXISTS posts_author ON posts (author_id);
CREATE INDEX IF NOT EXISTS posts_collected ON posts (collected_at);

-- N4 AI 判定と N10 採点の結果(判定したポスト 1 件 = 1 行)。
CREATE TABLE IF NOT EXISTS judgments (
  post_id TEXT PRIMARY KEY,
  judged_at TEXT NOT NULL,
  model TEXT,
  case_type TEXT,
  is_cosplay INTEGER,
  works TEXT,
  excluded_work TEXT,
  prefecture TEXT,
  prefecture_source TEXT,
  place TEXT,
  dates TEXT,
  summary TEXT,
  reject_reason TEXT,
  score REAL,
  score_json TEXT,
  critical INTEGER NOT NULL DEFAULT 0,
  critical_reason TEXT,
  raw_json TEXT
);
CREATE INDEX IF NOT EXISTS judgments_score ON judgments (score);

-- N13 X Finderでの確認の状態(案件カード 1 枚 = 1 行)。行がなければ「未確認」(一度も開いていない)。
-- seen(確認済み) / hold(保留: ピン留め、期間が過ぎても一覧に残す) / delete(削除予定: 投稿日から 14 日で消す)
CREATE TABLE IF NOT EXISTS reviews (
  post_id TEXT PRIMARY KEY,
  state TEXT NOT NULL,
  opened_at TEXT,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reviews_state ON reviews (state);

-- 削除ポスト確認(finder/deleted_step.py)の結果。元のポストが見られなくなったら gone_at を入れ、削除予定へ移す。
-- 印の付いたポストは二度と確かめない(自分で新着などに戻したら、そのまま残る)。
CREATE TABLE IF NOT EXISTS post_checks (
  post_id TEXT PRIMARY KEY,
  checked_at TEXT NOT NULL,
  gone_at TEXT
);

-- ブラックリスト: X Finderで「スパム」「虚偽の申告」などと判断したアカウント(1 人 = 1 行、数字の ID)。
-- 載っている人のポストは収集しても AI 判定にかけず(status=skipped)、X Finderにも出さない。解除もできる。
CREATE TABLE IF NOT EXISTS blacklist (
  user_id TEXT PRIMARY KEY,
  username TEXT,
  reason TEXT NOT NULL,  -- spam(スパム) / false(虚偽の申告) / photographer(カメラマンだった) / other(その他)
  note TEXT,
  added_at TEXT NOT NULL
);

-- ユーザー台帳: X のアカウント 1 人 = 1 行。数字の ID で同じ人を見分ける。
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  username TEXT,
  name TEXT,
  followers INTEGER,
  following INTEGER,
  description TEXT,
  location TEXT,
  profile_image TEXT,
  status TEXT NOT NULL DEFAULT 'confirmed',
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  profile_updated_at TEXT,
  research_json TEXT,
  researched_at TEXT
);
CREATE INDEX IF NOT EXISTS users_username ON users (lower(username));

-- プロフィールの URL 欄と自己紹介にあるリンク(Litlink など)と、そのページから読み取った年齢・活動地域・性別。1 人 = 1 行。
-- 収集のときに urls_json を入れ、案件になった依頼主だけ link_step がページを読んで found_json に結果を入れる。
-- ページの文字そのものは保存しない。found_json = {"age": {"label": "20代", "minor": false}, "areas": ["愛知県"], "gender": "女性"}
CREATE TABLE IF NOT EXISTS user_links (
  user_id TEXT PRIMARY KEY,
  urls_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  fetched_at TEXT,
  page_url TEXT,
  found_json TEXT,
  error TEXT
);

-- 画像から読み間違えた文字列のうち、この人だと確定したもの。
CREATE TABLE IF NOT EXISTS user_aliases (
  alias TEXT NOT NULL,
  user_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (alias, user_id)
);

-- N7 添付画像(メンバー表など)から AI が読み取った X のアカウント。1 ポスト = 1 行。読んだら(0 人でも)行を作り、二度は読まない。
-- accounts_json = [{"handle": "abc", "name": "表示名"}]。数字の ID は分からないので、画面で users の username と突き合わせる。
CREATE TABLE IF NOT EXISTS image_reads (
  post_id TEXT PRIMARY KEY,
  read_at TEXT NOT NULL,
  model TEXT,
  images INTEGER NOT NULL DEFAULT 0,
  accounts_json TEXT,
  error TEXT
);

-- どのポストに、誰が、どの立場で出てきたか(client / quoted / reply_to / mention)。
CREATE TABLE IF NOT EXISTS post_users (
  post_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  role TEXT NOT NULL,
  PRIMARY KEY (post_id, user_id, role)
);
CREATE INDEX IF NOT EXISTS post_users_user ON post_users (user_id);

-- 実行の記録(1 回の収集 = 1 行)。
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  window_start TEXT,
  window_end TEXT,
  status TEXT NOT NULL,
  fetched INTEGER DEFAULT 0,
  new_posts INTEGER DEFAULT 0,
  api_calls INTEGER DEFAULT 0,
  cost_usd REAL DEFAULT 0,
  note TEXT
);

-- 外部 API の費用(今月の費用の表示と、費用の目標の判定に使う)。
CREATE TABLE IF NOT EXISTS costs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  service TEXT NOT NULL,
  units INTEGER,
  usd REAL NOT NULL,
  note TEXT
);
CREATE INDEX IF NOT EXISTS costs_at ON costs (at);

-- 小さな値の置き場(Notion の表の ID、前回の検索時間枠、前回の正しい設定など)。
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT,
  updated_at TEXT
);

-- 遡り: N3 を通ったポストの、返信元・引用元を root まで(最大 10 段)たどった結果。
-- ids_json は遡ったポストの ID を古い順(root が先頭)に並べたもの。親がなければ []。
-- 行があるポストは遡りが済んでいる(判定はこれを待つ)。
CREATE TABLE IF NOT EXISTS post_trees (
  post_id TEXT PRIMARY KEY,
  ids_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- 遡りで取りに行ったが見られなかったポスト(削除・非公開)。二度と取りに行かない。
CREATE TABLE IF NOT EXISTS missing_posts (
  id TEXT PRIMARY KEY,
  checked_at TEXT NOT NULL
);
