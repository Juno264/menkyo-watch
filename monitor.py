#!/usr/bin/env python3
"""
警視庁 学科試験 予約サイト 空き状況モニター（見るだけ・予約はしない）

使い方:
  1) 記録:  python monitor.py record koto     # ブラウザが開くので、カレンダー表示まで手でクリック → ターミナルでEnter
  2) 確認:  python monitor.py check           # 記録済みの全プロファイルを1回ずつ確認（cronから呼ぶ）
  3) 集計:  python monitor.py analyze         # 調査期間のデータから「いつ・何日先の枠が・どれくらい空くか」を集計
  4) 通知テスト: python monitor.py test-notify

MODE = "observe"（調査：記録だけ、通知はエラーと1日1回のまとめ）
MODE = "hunt"   （本番：変化のたびに即通知）
定期実行は README.md を参照。
"""
import base64
import csv
import os
import hashlib
import re
from collections import Counter, defaultdict
from datetime import date, timedelta
import json
import random
import sys
import time
import difflib
import urllib.request
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

# ===== 設定 =====
START_URL = "https://license-renew.tokyo-madoguchi-yoyaku.com/police-pref-tokyo/index_000.html"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "CHANGE-ME-menkyo-xxxxxxxx")   # GitHubではSecretsから読む
NTFY_SERVER = "https://ntfy.sh"
MODE = os.environ.get("MODE", "observe")   # 10/2まで "observe"、卒業後に "hunt"
JITTER_MAX_SEC = int(os.environ.get("JITTER_MAX_SEC", "60"))                       # 毎回0〜60秒ずらしてアクセス（15分間隔の実行と重ならない範囲）
DAILY_SUMMARY_HOUR = 22                    # observe中、この時刻台の実行で1日のまとめを通知
# ================

PROXY_URL = os.environ.get("PROXY_URL", "")      # VercelのURL（例 https://xxx.vercel.app/api/fetch）
PROXY_TOKEN = os.environ.get("PROXY_TOKEN", "")
PROXY_PATTERN = re.compile(r"^https://[^/]*tokyo-madoguchi-yoyaku\.com/")

BASE = Path(__file__).resolve().parent
PROFILES = BASE / "profiles"
STATE = BASE / "state"
DEBUG = BASE / "debug"
HISTORY = BASE / "history"
OBS_CSV = BASE / "observations.csv"   # 全チェックの記録（時刻・結果）
CHG_CSV = BASE / "changes.csv"        # 変化があった時の差分
LOCK = BASE / ".lock"
for p in (PROFILES, STATE, DEBUG, HISTORY):
    p.mkdir(exist_ok=True)

RECORDER_JS = r"""
(() => {
  if (window.__recInstalled) return; window.__recInstalled = true;
  const pick = (el) => el.closest('a,button,label,[role=button],input[type=submit],input[type=button],input[type=radio],input[type=checkbox],td,li,span,div');
  document.addEventListener('click', (e) => {
    const el = pick(e.target); if (!el) return;
    if (el.tagName === 'INPUT' && ['text','email','tel','number','date','password'].includes(el.type)) return;
    let text = '';
    if (el.tagName === 'INPUT' && ['radio','checkbox'].includes(el.type)) {
      const lab = el.closest('label') || (el.id && document.querySelector(`label[for="${el.id}"]`));
      text = lab ? lab.innerText : (el.getAttribute('aria-label') || '');
    } else {
      text = el.innerText || el.value || el.getAttribute('aria-label') || '';
    }
    text = text.trim().replace(/\s+/g,' ');
    if (!text || text.length > 60) return;
    window.recordStep({kind:'click', text, tag: el.tagName});
  }, true);
  document.addEventListener('change', (e) => {
    const el = e.target; const key = el.name || el.id; if (!key) return;
    if (el.tagName === 'SELECT') window.recordStep({kind:'select', key, value: el.value});
    else if (el.tagName === 'TEXTAREA' || (el.tagName === 'INPUT' && !['radio','checkbox','submit','button'].includes(el.type)))
      window.recordStep({kind:'fill', key, value: el.value});
  }, true);
})();
"""


def notify(title, body, image=None):
    # 本文（日本語OK）をPOST → 画像は別メッセージで添付。タイトルはHTTPヘッダなのでASCIIのみ。
    url = f"{NTFY_SERVER}/{NTFY_TOPIC}"
    try:
        req = urllib.request.Request(url, data=body[:3500].encode("utf-8"), method="POST")
        req.add_header("Title", title)
        req.add_header("Tags", "car")
        req.add_header("Priority", "high")
        urllib.request.urlopen(req, timeout=20).read()
        if image and Path(image).exists():
            req = urllib.request.Request(url, data=Path(image).read_bytes(), method="PUT")
            req.add_header("Filename", "calendar.png")
            req.add_header("Title", title + " (screenshot)")
            urllib.request.urlopen(req, timeout=30).read()
    except Exception as e:
        print("通知失敗:", e)


def append_csv(path, header, row):
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow(row)


def _relay(route):
    """予約サイト宛ての通信を東京の中継役に回し、返ってきた内容をそのままブラウザに渡す"""
    req = route.request
    try:
        body = req.post_data_buffer
    except Exception:
        body = None
    payload = json.dumps({
        "url": req.url, "method": req.method, "headers": req.all_headers(),
        "body_b64": base64.b64encode(body).decode() if body else None,
    }).encode()
    last = None
    for _ in range(2):
        try:
            r = urllib.request.Request(PROXY_URL, data=payload, method="POST",
                                       headers={"content-type": "application/json", "x-proxy-token": PROXY_TOKEN})
            data = json.loads(urllib.request.urlopen(r, timeout=40).read())
            headers = dict(data.get("headers") or {})
            if data.get("set_cookie"):
                headers["set-cookie"] = "\n".join(data["set_cookie"])
            route.fulfill(status=data["status"], headers=headers, body=base64.b64decode(data["body_b64"]))
            return
        except Exception as e:
            last = e
            time.sleep(2)
    print("relay失敗:", req.url, last)
    route.abort()


UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


def new_page(browser, **kw):
    ctx = browser.new_context(locale="ja-JP", timezone_id="Asia/Tokyo", user_agent=UA, **kw)
    if PROXY_URL and os.environ.get("NO_RELAY") != "1":
        ctx.route(PROXY_PATTERN, _relay)
    return ctx.new_page()


def settle(page):
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    time.sleep(1.0)


def clean_steps(steps):
    """同じ要素の連続記録（ラベル＋ラジオ等）をまとめ、入力欄は最後の値だけ残す"""
    clean = []
    for s in steps:
        if clean and clean[-1].get("kind") == "click" and s.get("kind") == "click" and clean[-1]["text"] == s["text"]:
            continue
        if s["kind"] in ("fill", "select") and clean and clean[-1].get("key") == s.get("key") and clean[-1]["kind"] == s["kind"]:
            clean[-1] = s
            continue
        clean.append(s)
    return clean


def record(name):
    steps = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        ctx = browser.new_context(locale="ja-JP")
        ctx.expose_binding("recordStep", lambda src, s: (steps.append(s), print("  記録:", s)))
        ctx.add_init_script(RECORDER_JS)
        page = ctx.new_page()
        page.goto(START_URL)
        print("\nブラウザで『空き状況カレンダー』が表示されるまでクリックしてください。")
        print("※ 予約確定ボタンは押さないこと。カレンダーが見えたらここでEnter。")
        input()
        clean = clean_steps(steps)
        (PROFILES / f"{name}.json").write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
        (DEBUG / f"{name}_calendar.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(DEBUG / f"{name}_calendar.png"), full_page=True)
        browser.close()
    print(f"\n保存しました: profiles/{name}.json（{len(clean)}ステップ）")
    print("次に `python monitor.py check` で再生できるか確認してください。")


def do_step(page, s):
    if s["kind"] == "click":
        t = s["text"]
        for loc in (page.get_by_role("button", name=t, exact=True),
                    page.get_by_role("link", name=t, exact=True),
                    page.get_by_label(t, exact=True),
                    page.get_by_text(t, exact=True)):
            try:
                if loc.count() > 0:
                    loc.first.click(timeout=8000)
                    return
            except Exception:
                continue
        page.get_by_text(t).first.click(timeout=8000)  # 部分一致で最終手段
    else:
        sel = f'[name="{s["key"]}"], #{s["key"]}'
        loc = page.locator(sel).first
        if s["kind"] == "select":
            loc.select_option(s["value"])
        else:
            loc.fill(s["value"])


def normalize(text):
    lines = [" ".join(l.split()) for l in text.splitlines()]
    return [l for l in lines if l]


def check_one(pw, name):
    steps = json.loads((PROFILES / f"{name}.json").read_text(encoding="utf-8"))
    browser = pw.chromium.launch(headless=True)
    page = new_page(browser)
    now = datetime.now()
    stamp = now.strftime("%Y%m%d_%H%M")
    iso = now.strftime("%Y-%m-%d %H:%M:%S")
    t0 = time.time()
    try:
        page.goto(START_URL, timeout=30000)
        settle(page)
        for i, s in enumerate(steps):
            do_step(page, s)
            settle(page)
        lines = normalize(page.inner_text("body"))
        shot = DEBUG / f"{name}_latest.png"
        page.screenshot(path=str(shot), full_page=True)
    except Exception as e:
        page.screenshot(path=str(DEBUG / f"{name}_error_{stamp}.png"), full_page=True)
        (DEBUG / f"{name}_error_{stamp}.html").write_text(page.content(), encoding="utf-8")
        browser.close()
        append_csv(OBS_CSV, ["time", "profile", "result", "added", "removed", "sec"],
                   [iso, name, "error", 0, 0, round(time.time() - t0, 1)])
        raise RuntimeError(f"{name}: 再生に失敗（{e.__class__.__name__}）") from e
    browser.close()

    st_file = STATE / f"{name}.json"
    new_hash = hashlib.sha256("\n".join(lines).encode()).hexdigest()
    old = json.loads(st_file.read_text(encoding="utf-8")) if st_file.exists() else None
    st_file.write_text(json.dumps({"hash": new_hash, "lines": lines, "at": stamp, "ok": True}, ensure_ascii=False), encoding="utf-8")

    sec = round(time.time() - t0, 1)
    header = ["time", "profile", "result", "added", "removed", "sec"]
    if old is None:
        print(f"[{name}] 初回スナップショット保存")
        (HISTORY / f"{name}_{stamp}.txt").write_text("\n".join(lines), encoding="utf-8")
        append_csv(OBS_CSV, header, [iso, name, "first", 0, 0, sec])
        return
    if old.get("hash") == new_hash:
        print(f"[{name}] 変化なし")
        append_csv(OBS_CSV, header, [iso, name, "same", 0, 0, sec])
        return
    diff = list(difflib.unified_diff(old["lines"], lines, lineterm="", n=0))
    added = [l[1:] for l in diff if l.startswith("+") and not l.startswith("+++")]
    removed = [l[1:] for l in diff if l.startswith("-") and not l.startswith("---")]
    append_csv(OBS_CSV, header, [iso, name, "changed", len(added), len(removed), sec])
    append_csv(CHG_CSV, ["time", "profile", "prev_time", "added", "removed"],
               [iso, name, old.get("at", ""), " || ".join(added), " || ".join(removed)])
    (HISTORY / f"{name}_{stamp}.txt").write_text("\n".join(lines), encoding="utf-8")
    if MODE != "hunt":
        print(f"[{name}] 変化あり（observe: 記録のみ） +{len(added)} -{len(removed)}")
        return
    body = f"【{name}】カレンダーに変化\n\n増えた/変わった行:\n" + "\n".join(added[:25]) + \
           ("\n\n消えた行:\n" + "\n".join(removed[:10]) if removed else "") + \
           "\n\n今すぐ予約サイトを確認！"
    print(body)
    notify(f"Menkyo: {name} changed", body, image=shot)


def check(names=None):
    names = names or sorted(p.stem for p in PROFILES.glob("*.json"))
    if not names:
        print("profiles が空です。先に record してください。"); return
    if LOCK.exists() and time.time() - LOCK.stat().st_mtime < 900:
        print("前回の実行がまだ動いているのでスキップ"); return
    LOCK.write_text(str(time.time()))
    try:
        _check(names)
        if MODE == "observe" and datetime.now().hour == DAILY_SUMMARY_HOUR and "--now" not in sys.argv:
            flag = STATE / f"summary_{date.today()}.flag"
            if not flag.exists():
                notify("Menkyo: daily summary", analyze(days=1, quiet=True))
                flag.write_text("sent")
    finally:
        LOCK.unlink(missing_ok=True)


def _check(names):
    if "--now" not in sys.argv:
        time.sleep(random.uniform(0, JITTER_MAX_SEC))
    with sync_playwright() as pw:
        for n in names:
            err_flag = STATE / f"{n}.error"
            try:
                check_one(pw, n)
                err_flag.unlink(missing_ok=True)
            except Exception as e:
                print("エラー:", e)
                if not err_flag.exists():  # 連続エラーは初回だけ通知
                    notify(f"Menkyo: {n} error", f"{e}\n記録し直しが必要かもしれません（debugフォルダ参照）")
                    err_flag.write_text(str(e), encoding="utf-8")
            time.sleep(random.uniform(5, 15))


def explore(name):
    """記録済みの手順（途中までで可）を再生し、その時点の画面・HTML・押せる要素一覧を保存する"""
    pf = PROFILES / f"{name}.json"
    steps = json.loads(pf.read_text(encoding="utf-8")) if pf.exists() else []
    out = DEBUG / f"explore_{name}"
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = new_page(browser, viewport={"width": 1280, "height": 900})
        log = []
        try:
            resp = page.goto(START_URL, timeout=45000)
            log.append(f"status {resp.status if resp else '?'} {page.url}")
            settle(page)
            for i, s in enumerate(steps):
                do_step(page, s)
                settle(page)
                log.append(f"step{i} ok {s} -> {page.url}")
        except Exception as e:
            log.append(f"ERROR {e.__class__.__name__}: {str(e)[:300]}")
        time.sleep(6)
        log.append(f"final url: {page.url}")
        try:
            msgs = page.evaluate("""async () => { const out = {};
              for (const s of document.querySelectorAll('script[src]')) {
                if (/Messages/.test(s.src)) { try { out[s.src] = await (await fetch(s.src)).text(); } catch (e) { out[s.src] = String(e); } } }
              return out; }""")
            (out / "messages.json").write_text(json.dumps(msgs, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            log.append(f"msg fetch failed {e}")
        (out / "page.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(out / "page.png"), full_page=True)
        items = page.evaluate("""() => [...document.querySelectorAll('a,button,input,select,label,[role=button],[onclick]')]
          .map(e => ({tag:e.tagName, type:e.type||'', name:e.name||e.id||'', text:(e.innerText||e.value||e.getAttribute('aria-label')||'').trim().replace(/\\s+/g,' ').slice(0,80), href:e.getAttribute('href')||''}))""")
        (out / "clickables.json").write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
        (out / "text.txt").write_text(page.inner_text("body"), encoding="utf-8")
        (out / "log.txt").write_text("\n".join(log), encoding="utf-8")
        browser.close()
    print("\n".join(log)); print(f"保存: {out}")


OPEN_MARKS = ("○", "◯", "〇", "◎", "△", "空き", "空有", "残")
CLOSE_MARKS = ("×", "✕", "満", "受付終了", "締切", "－", "-")
DATE_RE = re.compile(r"(?:(\d{1,2})月(\d{1,2})日)|(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])")


def _status(seg):
    if any(k in seg for k in OPEN_MARKS):
        return "open"
    if any(k in seg for k in CLOSE_MARKS):
        return "close"
    return "?"


def _pairs(lines, ref):
    """行の中の各日付と、その直後（次の日付まで）の記号から {日付: open/close} を作る"""
    out = {}
    for line in lines:
        ms = list(DATE_RE.finditer(line))
        for i, m in enumerate(ms):
            mo, d = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
            mo, d = int(mo), int(d)
            if not (1 <= mo <= 12 and 1 <= d <= 31):
                continue
            y = ref.year + (1 if mo < ref.month - 1 else 0)
            try:
                dt = date(y, mo, d)
            except ValueError:
                continue
            seg = line[m.end(): ms[i + 1].start() if i + 1 < len(ms) else len(line)]
            out[dt] = _status(seg)
    return out


def analyze(days=None, quiet=False):
    """observations.csv / changes.csv を集計してテキストで返す（analysis.txt にも保存）"""
    if not OBS_CSV.exists():
        msg = "まだデータがありません"; print(msg); return msg
    since = datetime.now() - timedelta(days=days) if days else None
    obs = [r for r in csv.DictReader(OBS_CSV.open(encoding="utf-8"))
           if not since or datetime.fromisoformat(r["time"]) >= since]
    chg = [r for r in csv.DictReader(CHG_CSV.open(encoding="utf-8"))] if CHG_CSV.exists() else []
    chg = [r for r in chg if not since or datetime.fromisoformat(r["time"]) >= since]

    res = Counter(r["result"] for r in obs)
    by_hour = Counter(datetime.fromisoformat(r["time"]).hour for r in chg)
    checks_hour = Counter(datetime.fromisoformat(r["time"]).hour for r in obs if r["result"] != "error")
    by_wd = Counter("月火水木金土日"[datetime.fromisoformat(r["time"]).weekday()] for r in chg)
    by_prof = Counter(r["profile"] for r in chg)
    lead = Counter(); opened = closed = 0; open_dates = Counter()
    open_since = {}; durations = []
    buckets = [(0, 1, "当日〜翌日"), (2, 3, "2〜3日後"), (4, 7, "4〜7日後"), (8, 14, "8〜14日後"), (15, 999, "15日以上先")]
    for r in chg:
        t = datetime.fromisoformat(r["time"]).date()
        before = _pairs(filter(None, r["removed"].split(" || ")), t)
        after = _pairs(filter(None, r["added"].split(" || ")), t)
        for dt, st in after.items():
            prev = before.get(dt)
            ts = datetime.fromisoformat(r["time"])
            if st == "open" and prev != "open":
                open_since[(r["profile"], dt)] = ts
                opened += 1; open_dates[dt] += 1
                ld = (dt - t).days
                for lo, hi, lab in buckets:
                    if lo <= ld <= hi:
                        lead[lab] += 1
            elif st == "close" and prev == "open":
                closed += 1
                t_open = open_since.pop((r["profile"], dt), None)
                if t_open:
                    durations.append((ts - t_open).total_seconds() / 60)

    L = []
    span = f"直近{days}日" if days else "全期間"
    L.append(f"=== 集計（{span}）===")
    L.append(f"チェック回数: {len(obs)}（変化あり {res['changed']} / 変化なし {res['same']} / エラー {res['error']}）")
    ok = res['changed'] + res['same']
    if ok:
        L.append(f"変化が見つかった割合: {res['changed'] / ok * 100:.1f}%")
    L.append(f"×→空きに変わった: {opened} 件 / 空き→×に戻った: {closed} 件（※日付と○×の自動判定は目安）")
    if durations:
        ds = sorted(durations)
        L.append(f"空きが埋まるまで: 中央値 約{ds[len(ds)//2]:.0f}分 / 最短 約{ds[0]:.0f}分（{len(ds)}件、確認間隔より細かくは測れない）")
    L.append("\n[時刻別] 変化回数 / チェック回数")
    for h in range(24):
        if checks_hour[h]:
            L.append(f"  {h:02d}時  {'#' * by_hour[h]:<20} {by_hour[h]}/{checks_hour[h]}")
    L.append("\n[曜日別] " + "  ".join(f"{w}:{by_wd[w]}" for w in "月火水木金土日"))
    L.append("[試験場別] " + "  ".join(f"{k}:{v}" for k, v in by_prof.items()))
    L.append("\n[空いた枠は何日先の受験日か]")
    for _, _, lab in buckets:
        L.append(f"  {lab:<8} {'#' * lead[lab]} {lead[lab]}")
    if open_dates:
        L.append("\n[空きが出た受験日 上位]")
        for dt, n in open_dates.most_common(10):
            L.append(f"  {dt:%m/%d}({'月火水木金土日'[dt.weekday()]}) {n}回")
    text = "\n".join(L)
    (BASE / "analysis.txt").write_text(text, encoding="utf-8")
    if not quiet:
        print(text)
    return text


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "record" and len(sys.argv) > 2:
        record(sys.argv[2])
    elif cmd == "check":
        check([a for a in sys.argv[2:] if not a.startswith("--")] or None)
    elif cmd == "explore" and len(sys.argv) > 2:
        explore(sys.argv[2])
    elif cmd == "relaytest":
        MAINT = 'id="maintenance"'
        out = DEBUG / "relaytest.txt"; lines = [f"PROXY_URL={PROXY_URL} token_set={bool(PROXY_TOKEN)}"]
        try:
            r = urllib.request.Request(PROXY_URL, headers={"x-proxy-token": PROXY_TOKEN})
            lines.append("GET: " + urllib.request.urlopen(r, timeout=40).read().decode()[:500])
        except Exception as e:
            lines.append(f"GET error: {e} {getattr(e, 'read', lambda: b'')()[:300]}")
        try:
            p = json.dumps({"url": "https://license-test.tokyo-madoguchi-yoyaku.com/police-pref-tokyo/index.html?lang=ja", "method": "GET", "headers": {"user-agent": "Mozilla/5.0"}}).encode()
            r = urllib.request.Request(PROXY_URL, data=p, method="POST", headers={"content-type": "application/json", "x-proxy-token": PROXY_TOKEN})
            d = json.loads(urllib.request.urlopen(r, timeout=40).read())
            body = base64.b64decode(d["body_b64"]).decode("utf-8", "replace")
            lines.append(f"license-test status={d['status']} maintenance={MAINT in body} len={len(body)}")
            lines.append(body[:1500])
        except Exception as e:
            lines.append(f"POST error: {e}")
        out.write_text("\n".join(lines), encoding="utf-8"); print("\n".join(lines))
    elif cmd == "relayget":
        for i, url in enumerate(sys.argv[2:]):
            p = json.dumps({"url": url, "method": "GET", "headers": {"user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1", "accept-language": "ja"}}).encode()
            r = urllib.request.Request(PROXY_URL, data=p, method="POST", headers={"content-type": "application/json", "x-proxy-token": PROXY_TOKEN})
            try:
                d = json.loads(urllib.request.urlopen(r, timeout=40).read())
                body = base64.b64decode(d["body_b64"]).decode("utf-8", "replace")
                txt = f"URL {url}\nstatus {d['status']}\nheaders {json.dumps(d['headers'], ensure_ascii=False)}\nset-cookie {d.get('set_cookie')}\n\n{body}"
            except Exception as e:
                txt = f"URL {url}\nERROR {e}"
            (DEBUG / f"relayget_{i}.txt").write_text(txt, encoding="utf-8")
            print(txt[:300])
    elif cmd == "analyze":
        analyze()
    elif cmd == "test-notify":
        notify("Menkyo: test", "通知テストです。届いていればOK")
        print("送信しました")
    else:
        print(__doc__)
