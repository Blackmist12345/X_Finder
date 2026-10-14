"""依頼主のプロフィールにあるリンク先(Litlink など)を読み、年齢・活動地域・性別を user_links に保存する。

X のプロフィールに年齢や活動地域が書いてなくても、リンク先のページに書いてあることがある。
読み方はプロフィールと同じ決まり(web/src/profile.js)を node で呼んで使う。ページの文字そのものは保存しない。
AI は使わないので費用はかからない。案件になった依頼主だけ、1 人 1 回(REFRESH_DAYS ごとに読み直す)。
"""
import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import requests

from finder import store

MAX_USERS = 20           # 1 回の実行で読む人数
REFRESH_DAYS = 30        # これより古くなったら読み直す
MAX_BYTES = 2_000_000    # これより大きいページは途中まで
MAX_TEXT = 20000         # 読み取りに使う文字数(保存はしない)
TIMEOUT = 10
READER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "scripts", "read-links.mjs")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; XFinder/1.0)", "Accept-Language": "ja,en;q=0.5"}

# ログインしないと中身が見えない SNS や、動画・ショップは読まない
SKIP_HOSTS = ("instagram.com", "tiktok.com", "facebook.com", "youtube.com", "youtu.be", "threads.net",
              "x.com", "twitter.com", "booth.pm", "amazon.co.jp", "amzn.to", "amzn.asia", "discord.gg", "discord.com")
# 自己紹介をまとめるためのリンク集サービス。ここにあるものを先に読む
LINK_PAGES = ("lit.link", "linktr.ee", "potofu.me", "profcard.info", "bio.link", "solo.to", "linkco.re",
              "tap.bio", "instabio.cc", "taplink.cc", "carrd.co", "fc2.com", "fanbox.cc", "skeb.jp", "pixiv.net")


def host_of(url):
    try:
        return url.split("/")[2].split(":")[0].lower().removeprefix("www.")
    except IndexError:
        return ""


def _matches(host, names):
    return any(host == n or host.endswith("." + n) for n in names)


def pick_url(urls):
    """読むリンクを 1 つ選ぶ。リンク集サービスを優先し、読まないサイトは外す。"""
    usable = [u for u in urls if u.startswith(("http://", "https://")) and not _matches(host_of(u), SKIP_HOSTS)]
    for u in usable:
        if _matches(host_of(u), LINK_PAGES):
            return u
    return usable[0] if usable else None


class _TextParser(HTMLParser):
    """ページの見える文字と、説明(meta)と、ページに埋め込まれたデータ(JSON)の文字を集める。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.data_parts = [], []
        self._skip = 0
        self._json = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "noscript", "template", "svg"):
            self._skip += 1
            self._json = tag == "script" and ("json" in (a.get("type") or "") or a.get("id") == "__NEXT_DATA__")
        elif tag == "meta" and (a.get("name") or a.get("property") or "").lower() in ("description", "og:description", "og:title"):
            if a.get("content"):
                self.parts.append(a["content"])
        elif tag in ("br", "p", "div", "li", "h1", "h2", "h3", "h4", "a", "span", "section"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template", "svg") and self._skip:
            self._skip -= 1
            self._json = False

    def handle_data(self, data):
        if self._json:
            self.data_parts.append(data)
        elif not self._skip:
            self.parts.append(data)


def _json_strings(value, out):
    if isinstance(value, str):
        v = value.strip()
        # URL や ID のような文字は外し、人が書いた文章だけ残す
        if v and not v.startswith(("http://", "https://", "/", "{", "[")) and not re.fullmatch(r"[A-Za-z0-9_\-.:#]+", v):
            out.append(v)
    elif isinstance(value, dict):
        for v in value.values():
            _json_strings(v, out)
    elif isinstance(value, list):
        for v in value:
            _json_strings(v, out)


def page_text(html):
    """HTML から、年齢や地域を探すための文字を取り出す(重複した行はまとめる)。"""
    p = _TextParser()
    try:
        p.feed(html)
        p.close()
    except Exception:  # 壊れた HTML でも、読めたところまでは使う
        pass
    lines = []
    for chunk in p.parts:
        lines += chunk.split("\n")
    strings = []
    for raw in p.data_parts:
        try:
            _json_strings(json.loads(raw), strings)
        except ValueError:
            continue
    for s in strings:
        lines += s.split("\n")
    out, seen = [], set()
    for line in lines:
        line = re.sub(r"\s+", " ", line).strip()
        if line and line not in seen:
            seen.add(line)
            out.append(line)
    return "\n".join(out)[:MAX_TEXT]


def fetch_page(s, url):
    """(最後に着いた URL, 文字) を返す。読めなければ例外。"""
    res = s.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True, allow_redirects=True)
    try:
        res.raise_for_status()
        kind = res.headers.get("content-type", "")
        if kind and "html" not in kind and "text" not in kind:
            raise ValueError(f"ページではありません({kind.split(';')[0]})")
        body = b""
        for chunk in res.iter_content(65536):
            body += chunk
            if len(body) >= MAX_BYTES:
                break
        enc = res.encoding if res.encoding and res.encoding.lower() != "iso-8859-1" else "utf-8"
        return res.url, page_text(body.decode(enc, errors="replace"))
    finally:
        res.close()


def candidates(d1, now=None):
    """案件になったポストの依頼主のうち、リンク先をまだ読んでいない(古くなった)人。"""
    now = now or datetime.now(timezone.utc)
    stale = (now - timedelta(days=REFRESH_DAYS)).replace(microsecond=0).isoformat()
    return d1.query(
        "SELECT l.user_id, l.urls_json FROM user_links l "
        "WHERE (l.fetched_at IS NULL OR l.fetched_at < ?) "
        "AND EXISTS (SELECT 1 FROM posts p WHERE p.author_id = l.user_id AND p.status = 'judged') "
        "ORDER BY l.updated_at DESC LIMIT ?",
        [stale, MAX_USERS],
    )


def read_found(texts):
    """{user_id: ページの文字} から {user_id: {age, areas, gender}} を作る(web/src/profile.js の決まりで読む)。"""
    if not texts:
        return {}
    items = [{"id": k, "text": v} for k, v in texts.items()]
    res = subprocess.run(["node", READER], input=json.dumps(items, ensure_ascii=False), capture_output=True,
                         text=True, encoding="utf-8", timeout=60, check=True)
    return {r["id"]: {"age": r.get("age"), "areas": r.get("areas") or [], "gender": r.get("gender")}
            for r in json.loads(res.stdout)}


def run(d1, production, session=None, reader=read_found):
    """リンク先を読んで結果を保存し、要約の行を返す。試し実行では何もしない。"""
    if not production:
        return []
    lines = ["", "## プロフィールのリンク先を読む", ""]
    try:
        users = candidates(d1)
    except Exception as e:
        return lines + [f"- ⚠️ 読むリンクを探せませんでした: {e}"]
    if not users:
        return lines + ["- 新しく読むリンクはありませんでした"]
    s = session or requests.Session()
    pages, texts = {}, {}
    for u in users:
        url = pick_url(json.loads(u.get("urls_json") or "[]"))
        page_url, error = url, None
        if not url:
            error = "読めるリンクがありません"
        else:
            try:
                page_url, text = fetch_page(s, url)
                if text:
                    texts[u["user_id"]] = text
                else:
                    error = "ページに文字がありません"
            except Exception as e:
                error = str(e)[:300]
        pages[u["user_id"]] = (page_url, error)
    try:
        found = reader(texts)
    except Exception as e:  # 読み取れなければ保存せず、次の実行でもう一度読む
        return lines + [f"- ⚠️ リンク先の文字から読み取れませんでした: {str(e)[:200]}"]
    ok = failed = hit = 0
    for uid, (page_url, error) in pages.items():
        f = found.get(uid)
        if f:
            ok += 1
            hit += bool(f["age"] or f["areas"] or f["gender"])
        else:
            failed += 1
        d1.query("UPDATE user_links SET fetched_at = ?, page_url = ?, found_json = ?, error = ? WHERE user_id = ?",
                 [store.now_iso(), page_url, json.dumps(f, ensure_ascii=False) if f else None, error, uid])
    return lines + [f"- リンク先を読んだ人: {ok}人(年齢・活動地域・性別のどれかが分かった {hit}人)"
                    + (f"、読めなかった {failed}人" if failed else "")]
