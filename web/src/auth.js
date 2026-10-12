// Cloudflare Access の確認。X Finderは自分だけが見るページなので、Access を通った人だけに中身を返す。
// Access が付けてくる署名つきトークン(JWT)を、Access の公開鍵で検証する。
// 設定(Worker の変数): ACCESS_TEAM_DOMAIN(例 xxx.cloudflareaccess.com)、ACCESS_AUD(アプリの AUD タグ)

let certsCache = { at: 0, keys: null, team: null };
const CERTS_TTL_MS = 60 * 60 * 1000;

function b64urlToBytes(s) {
  const b64 = s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4);
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function decodeJson(part) {
  return JSON.parse(new TextDecoder().decode(b64urlToBytes(part)));
}

async function loadKeys(team, fetchImpl) {
  if (certsCache.keys && certsCache.team === team && Date.now() - certsCache.at < CERTS_TTL_MS) return certsCache.keys;
  const res = await fetchImpl(`https://${team}/cdn-cgi/access/certs`);
  if (!res.ok) throw new Error(`Access の公開鍵を取得できません (${res.status})`);
  const { keys } = await res.json();
  certsCache = { at: Date.now(), keys, team };
  return keys;
}

function tokenFrom(request) {
  const header = request.headers.get("cf-access-jwt-assertion");
  if (header) return header;
  const cookie = request.headers.get("cookie") || "";
  const m = cookie.match(/(?:^|;\s*)CF_Authorization=([^;]+)/);
  return m ? m[1] : null;
}

// 確認できたらメールアドレスを返す。できなければ null。
export async function verifyAccess(request, env, fetchImpl = fetch) {
  const team = (env.ACCESS_TEAM_DOMAIN || "").replace(/^https?:\/\//, "").replace(/\/$/, "");
  const token = tokenFrom(request);
  if (!team || !env.ACCESS_AUD || !token) return null;
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  let header, payload;
  try {
    header = decodeJson(parts[0]);
    payload = decodeJson(parts[1]);
  } catch {
    return null;
  }
  if (header.alg !== "RS256") return null;
  const keys = await loadKeys(team, fetchImpl);
  const jwk = keys.find((k) => k.kid === header.kid);
  if (!jwk) {
    certsCache.keys = null; // 鍵が入れ替わった直後かもしれないので、次回は取り直す
    return null;
  }
  const key = await crypto.subtle.importKey("jwk", jwk, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
  const ok = await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, b64urlToBytes(parts[2]),
    new TextEncoder().encode(`${parts[0]}.${parts[1]}`));
  if (!ok) return null;
  const now = Math.floor(Date.now() / 1000);
  const aud = Array.isArray(payload.aud) ? payload.aud : [payload.aud];
  if (!aud.includes(env.ACCESS_AUD)) return null;
  if (payload.iss !== `https://${team}`) return null;
  if (!payload.exp || payload.exp < now) return null;
  return payload.email || "(メールなし)";
}

export function resetCertsCache() {
  certsCache = { at: 0, keys: null, team: null };
}
