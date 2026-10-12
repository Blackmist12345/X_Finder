# X_Finder

カメラマン募集のポストを X から集め、点数を付けて、スマホの Web ページ「ファインダー」で確認するための仕組みです。
今の仕組み(x-search-pipeline)を作り直したもので、しばらくは両方を並行して動かします。

モジュールの番号(N0〜N15)と用語は、定義書に合わせています。

## いまできること(第1段階: 収集と保存 / 第2段階: AI 判定と採点)

| 番号 | 名前 | 場所 |
| --- | --- | --- |
| N1 | 設定読込 | `finder/config.py`(Notion 8表から読む。不正な値は前回の値を使う) |
| N2 | 収集 | `finder/collect.py`(TwitterAPI.io。失敗したら検索時間枠を進めない) |
| N3 | 一次フィルタ(一部) | `finder/filters.py`(リポスト・フォロワー数・入りきらなかった除外語。減点語は記録だけ) |
| N12 | データ保存 | `finder/store.py`, `schema.sql`(Cloudflare D1) |
| N15 | ユーザー台帳 | `finder/store.py`(投稿者・引用元・メンション先・リプライ先を登録) |
| N4 | AI 判定 | `finder/judge.py`(文字だけを Claude Haiku 5.5 でまとめて判定。Gemini にも切り替え可。対象外にするかはルールで決める) |
| N10 | 採点 | `finder/score.py`(【5】点数表と【6】クリティカル条件。都道府県が分かれば地名の減点語は使わない) |

収集は Collect ワークフローで 10分ごとに自動で動き、判定待ちが「まとめて判定する件数」たまるか、いちばん古いポストが「判定を待つ最長時間(分)」待ったら判定・採点します。通知とファインダーの画面は次の段階で作ります。

## 準備(初回だけ)

### 1. Repository secrets に鍵を入れる

GitHub の X_Finder → Settings → Secrets and variables → Actions → New repository secret で、次の名前で登録します。

| 名前 | 中身 |
| --- | --- |
| `TWITTERAPI_KEY` | TwitterAPI.io の API キー(今の仕組みと同じもの) |
| `ANTHROPIC_API_KEY` | Claude の API キー(判定に使う。ないと判定だけ飛ばす) |
| `GEMINI_API_KEY` | Gemini の API キー(【7】基本設定で Gemini に切り替えたときだけ必要) |
| `NOTION_API_KEY` | Notion のインテグレーションのキー(今の仕組みと同じもの) |
| `NOTION_PARENT_PAGE_ID` | 新しい 8表を置く Notion ページの URL |
| `NOTION_KEYWORDS_DB_ID` ほか3つ | 今の 4表の ID(今の仕組みと同じ名前・同じ値。移行にだけ使う) |
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare のアカウント ID |
| `CLOUDFLARE_API_TOKEN` | D1 の編集権限を持つ API トークン |
| `D1_DATABASE_ID` | 作った D1 データベースの ID |

### 2. Setup ワークフローを上から順に実行する

Actions → Setup → Run workflow で、task を選んで 1 回ずつ実行します。

1. `d1_schema`: D1 にテーブルを作る
2. `notion_tables`: Notion 8表を作る(親ページにインテグレーションを招待しておく)
3. `migrate_preview`: 今の 4表からの振り分け案を見る(何も書き込まない)
4. `migrate_apply`: 振り分け案のとおりに新しい表へ移す(今の 4表はそのまま残る)

### 3. Collect ワークフローを試す

Actions → Collect → Run workflow で、まず `dry_run`(何も保存しない)を実行し、結果の要約を確認します。
問題なければ `production` で実行します。定刻での自動起動は、手動で数回試したあとに追加します。

## テスト

```
pip install -r requirements.txt pytest
python -m pytest -q
```
