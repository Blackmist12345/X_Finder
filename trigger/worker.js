// N0 起動: Cloudflare Worker の Cron Trigger(10分ごと)から、GitHub の Collect ワークフローを動かす。
// GitHub の schedule は遅れたり飛ばされたりするため、時刻の正確な Cloudflare から起動する。
//
// Cloudflare のダッシュボードでこのファイルの中身を貼り付けて使う(手順は README)。
// 必要なシークレット: GITHUB_TOKEN(X_Finder の Actions を「読み書き」できる fine-grained トークン)

const REPO = "Blackmist12345/X_Finder";
const WORKFLOW = "collect.yml";

async function dispatch(env) {
  const res = await fetch(`https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "x-finder-trigger",
    },
    body: JSON.stringify({ ref: "main", inputs: { mode: "production", hours: "1" } }),
  });
  if (res.status !== 204) {
    // ダッシュボードの「ログ」に出る
    console.error(`起動に失敗しました (${res.status}): ${await res.text()}`);
  }
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatch(env));
  },
  // URL を開かれても起動はしない(誰でも動かせてしまうため)
  async fetch() {
    return new Response("X_Finder trigger");
  },
};
