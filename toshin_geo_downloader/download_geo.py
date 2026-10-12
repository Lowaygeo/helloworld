"""東進過去問データベース：各大學「地理」過去問 PDF 下載工具（在你自己的電腦上執行）

使用方式：見同資料夾的 README.md
- 帳號密碼不經過程式：程式只會打開瀏覽器，由你自己登入。
- 只做一般瀏覽（GET），每頁之間預設等待 2 秒，避免造成網站負擔。
- 可中斷後重新執行，會從上次進度繼續（state.json）。
"""
import argparse
import csv
import heapq
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

from playwright.sync_api import sync_playwright

START_URL = "https://www.toshin-kakomon.com/"
ALLOWED_HOST_SUFFIX = "toshin-kakomon.com"
SKIP_WORDS = ["ログアウト", "logout", "退会", "解約", "withdraw", "password", "パスワード", "mailto:", "javascript:", "tel:"]
EXCLUDE_WORDS = ["共通テスト", "共通テ", "センター試験", "kyotsu", "center"]

# JS：取出頁面所有連結，連同連結文字與其所在區塊（表格列/清單項目）的文字
JS_LINKS = """
() => Array.from(document.querySelectorAll('a[href]')).map(a => {
  let blk = null;
  for (const sel of ['tr', 'li', 'dl', 'p', 'div']) { blk = a.closest(sel); if (blk) break; }
  // 往上找，直到區塊文字比連結本身多（例如包含年度、科目名）
  const own = (a.innerText || '').trim();
  while (blk && blk.parentElement && blk.innerText.trim().length <= own.length + 2) blk = blk.parentElement;
  // 表格：一併附上表頭與同欄位資訊
  let head = '';
  const table = a.closest('table');
  if (table) { const th = table.querySelector('thead') || (table.querySelector('th') && table.querySelector('th').closest('tr')); head = th ? th.innerText.slice(0, 200) : ''; }
  return {href: a.href, text: (own || a.title || '').trim(),
          ctx: blk ? (blk.innerText.slice(0, 300) + ' ' + head) : head};
})
"""
JS_HEADING = """
() => {
  const pick = s => { const e = document.querySelector(s); return e ? e.innerText.trim() : ''; };
  return {title: document.title || '', h1: pick('h1'), h2: pick('h2'),
          crumb: pick('.breadcrumb, .breadcrumbs, nav[aria-label*="breadcrumb"], .pankuzu')};
}
"""


def years_in(text):
    ys = {int(y) for y in re.findall(r"(20[12]\d)", text)}
    ys |= {2018 + int(n) for n in re.findall(r"令和\s*(\d{1,2})", text)}
    return ys


def safe_name(s, limit=60):
    s = re.sub(r'[<>:"/\\|?*\r\n\t]', "_", s).strip(" .")
    return (s or "未命名")[:limit]


def university_of(head):
    for src in (head["crumb"], head["h1"], head["title"], head["h2"]):
        m = re.search(r"([^\s>/|｜・、]{1,20}(大学|大學|大学校))", src)
        if m:
            return m.group(1)
    return safe_name(head["h1"] or head["title"] or "大學不明", 30)


def in_scope(url, host=ALLOWED_HOST_SUFFIX):
    p = urlparse(url)
    return p.scheme in ("http", "https") and bool(p.hostname) and p.hostname.endswith(host)


def looks_pdf(url, text):
    u = url.lower()
    return u.endswith(".pdf") or ".pdf?" in u or "/pdf" in u or "pdf" in text.lower()


def priority(text, url):
    t = text + url
    if "地理" in t:
        return 0
    if "大学" in t or "大學" in t or "univ" in url.lower():
        return 1
    if re.search(r"年度|20[12]\d", t):
        return 2
    return 3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="地理過去問")
    ap.add_argument("--start-url", default=START_URL)
    ap.add_argument("--headless", action="store_true", help="測試用")
    ap.add_argument("--chromium-path", default=None, help="測試用")
    ap.add_argument("--from-year", type=int, default=2022)
    ap.add_argument("--to-year", type=int, default=2026)
    ap.add_argument("--delay", type=float, default=2.0, help="每次請求間隔秒數（請勿調太低）")
    ap.add_argument("--max-pages", type=int, default=6000)
    ap.add_argument("--dump", type=int, default=30, help="把前 N 個頁面的 HTML 存到 debug/，供除錯")
    ap.add_argument("--keep-unknown-year", action="store_true", help="連結上看不出年度時也下載")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    debug = out / "_debug"
    debug.mkdir(exist_ok=True)
    state_path = out / "state.json"
    state = json.loads(state_path.read_text("utf-8")) if state_path.exists() else {"visited": [], "downloaded": {}}
    visited = set(state["visited"])
    downloaded = state["downloaded"]  # url -> saved path
    manifest_path = out / "清單.csv"
    new_manifest = not manifest_path.exists()
    mf = open(manifest_path, "a", newline="", encoding="utf-8-sig")
    mw = csv.writer(mf)
    if new_manifest:
        mw.writerow(["大學", "年度", "連結文字", "存檔路徑", "網址", "來源頁"])
    host = urlparse(args.start_url).hostname if args.start_url != START_URL else ALLOWED_HOST_SUFFIX
    yr = range(args.from_year, args.to_year + 1)

    def save_state():
        state_path.write_text(json.dumps({"visited": sorted(visited), "downloaded": downloaded},
                                         ensure_ascii=False, indent=1), "utf-8")

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(out / "_browser_profile"), headless=args.headless, executable_path=args.chromium_path,
                                                    accept_downloads=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(args.start_url)
        print("\n=== 請在剛打開的瀏覽器視窗中登入東進過去問網站 ===")
        print("登入完成、看得到大學列表後，回到這個黑色視窗按 Enter 開始下載。")
        input()

        queue = []  # (priority, seq, url)
        seq = 0
        for u in {args.start_url, page.url}:
            heapq.heappush(queue, (0, seq, u)); seq += 1
        queued = {u for _, _, u in queue}
        pages_done = dumped = 0

        while queue and pages_done < args.max_pages:
            _, _, url = heapq.heappop(queue)
            if url in visited:
                continue
            visited.add(url)
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(500)
            except Exception as e:
                print(f"  [略過] {url}：{e}")
                continue
            pages_done += 1
            ctype = (resp.headers.get("content-type", "") if resp else "")
            if "pdf" in ctype:
                continue  # PDF 直接開啟的情形由下方連結判斷處理
            if "login" in page.url.lower() and "login" not in url.lower():
                print("\n看起來被登出了。請在瀏覽器重新登入後按 Enter 繼續。")
                input()
                visited.discard(url)
                heapq.heappush(queue, (0, seq, url)); seq += 1
                continue

            head = page.evaluate(JS_HEADING)
            links = page.evaluate(JS_LINKS)
            if dumped < args.dump:
                (debug / f"{dumped:03d}.html").write_text(f"<!-- {url} -->\n" + page.content(), "utf-8")
                dumped += 1
            head_text = " ".join(head.values())
            print(f"[{pages_done}] {safe_name(head['title'], 50)}  ({len(links)} 連結, 已下載 {len(downloaded)})")

            for ln in links:
                href = urldefrag(urljoin(url, ln["href"]))[0]
                text, ctx_text = ln["text"], ln["ctx"]
                blob = f"{text} {ctx_text} {href}"
                if any(w.lower() in blob.lower() for w in SKIP_WORDS) or not in_scope(href, host):
                    continue
                if any(w in f"{blob} {head_text}" for w in EXCLUDE_WORDS):
                    continue
                if looks_pdf(href, text):
                    if href in downloaded:
                        continue
                    if "地理" not in f"{text} {ctx_text} {head_text}":
                        continue
                    ys = years_in(f"{text} {ctx_text} {href}") or years_in(head_text)
                    hit = sorted(y for y in ys if y in yr)
                    if not hit and (ys or not args.keep_unknown_year):
                        continue
                    year = str(hit[0]) if hit else "年度不明"
                    uni = university_of(head)
                    folder = out / safe_name(uni) / year
                    folder.mkdir(parents=True, exist_ok=True)
                    try:
                        r = ctx.request.get(href, timeout=60000)
                        body = r.body()
                        if not body.startswith(b"%PDF"):
                            # 不是 PDF（可能是檢視頁），當作一般頁面繼續爬
                            if href not in queued:
                                heapq.heappush(queue, (0, seq, href)); seq += 1; queued.add(href)
                            continue
                        name = safe_name(f"{uni}_{year}_地理_{text or Path(urlparse(href).path).stem}", 80)
                        dest = folder / f"{name}.pdf"
                        i = 2
                        while dest.exists():
                            dest = folder / f"{name}_{i}.pdf"; i += 1
                        dest.write_bytes(body)
                        downloaded[href] = str(dest)
                        mw.writerow([uni, year, text, str(dest), href, url]); mf.flush()
                        print(f"    ✔ {uni} {year} {name}")
                    except Exception as e:
                        print(f"    ✘ 下載失敗 {href}：{e}")
                    time.sleep(args.delay)
                elif href not in visited and href not in queued:
                    heapq.heappush(queue, (priority(blob, href), seq, href)); seq += 1
                    queued.add(href)

            if pages_done % 20 == 0:
                save_state()
            time.sleep(args.delay)

        save_state()
        mf.close()
        print(f"\n完成：瀏覽 {pages_done} 頁，共下載 {len(downloaded)} 個 PDF，存放於 {out.resolve()}")
        if not downloaded:
            print("沒有下載到任何檔案。請把資料夾裡的 _debug 子資料夾傳給 Claude，以便調整程式。")
        ctx.close()


if __name__ == "__main__":
    sys.exit(main())
