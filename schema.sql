-- ファインダーのデータベース(Cloudflare D1)。N12 データ保存 / N15 ユーザー台帳。
-- 何度実行しても壊れないように IF NOT EXISTS で作る。

-- 収集したポスト。検索で見つけたもの(source=search)と、引用元として一緒に届いたもの(source=quoted)。
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

-- 画像から読み間違えた文字列のうち、この人だと確定したもの。
CREATE TABLE IF NOT EXISTS user_aliases (
  alias TEXT NOT NULL,
  user_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (alias, user_id)
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
