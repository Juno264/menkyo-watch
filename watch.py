#!/usr/bin/env python3
"""
学科試験 空き状況ウォッチャー（見るだけ・予約はしない）

  python watch.py run      # 6パターン（3試験場×2免許形態）を1回スキャンして記録・通知
  python watch.py report   # これまでの集計をDiscordに送る
  python watch.py test     # Discord通知テスト

記録ファイル:
  data/scans.csv   … 毎回のスキャン結果（パターン別の最短日・空き日数）
  data/events.csv  … 変化（日付が選べるようになった/埋まった、残り人数の増減）
  data/latest.json … 直近の状態
"""
import csv
import subprocess
import json
import os
import re
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, date, timedelta
from pathlib import Path

try:  # ブラウザ操作は調査用コマンドだけで使う。監視ループはAPIのみなのでPlaywright不要
    from playwright.sync_api import sync_playwright
    from monitor import START_URL, new_page, settle, do_step
except ImportError:
    sync_playwright = None

MODE = os.environ.get("MODE", "observe")            # observe（10/2まで）/ hunt（卒業後）
WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
BOOKING_INFO = os.environ.get("BOOKING_INFO", "")   # 予約入力用の個人情報（GitHub Secrets。本番通知のときだけ送る）
MY_DATE = os.environ.get("MY_DATE", "")             # 今持っている予約の日付（例 2026-12-10）。これより早い空きだけ通知
BOOK_URL = "https://license-test.tokyo-madoguchi-yoyaku.com/police-pref-tokyo/index.html?lang=ja"
SUMMARY_HOURS = {8, 20}                              # この時刻台の最初の実行でまとめを送る
# 調査目標「予約できる手前のキャンセルの頻度」を正しく測るため、3試験場ともデータ更新の直後に確認する
FAST = [("koto", "only"), ("fuchu", "only"), ("samezu", "only")]
SLOW = []
FAST_SEC = 60                                          # 江東の通常の確認間隔
PEAK_SEC = 30                                          # キャンセルが多い時間帯の確認間隔
DEFAULT_PEAK_HOURS = {6, 7, 8, 18, 19, 20, 21, 22, 23} # データが貯まるまでの仮のピーク時間帯
SLOW_MIN = 15                                          # 府中・鮫洲の確認間隔（分）
COMMIT_MIN = 15                                        # 保存（git push）の間隔（分）
MIN_GAP_SEC = 10                                       # 連続で問い合わせるときの最小間隔
EXPIRY_MARGIN_SEC = 3                                  # データ更新予定時刻の何秒後に確認するか
FAST_MIN = 1                                           # （レポート表示用）
MAX_CLICKS = 10                                        # 残り0の日が続いても、1回に確認する日数の上限
DETAIL_DAYS = 3                                      # 早い順に何日分、残り人数を読むか
MONTHS = 4

SITES = {"fuchu": "府中試験場", "samezu": "鮫洲試験場", "koto": "江東試験場"}
PLACE = {"fuchu": "270", "samezu": "280", "koto": "250"}
COURSE = {"only": "11", "both": "61"}                  # 教習所卒業等：従来の免許証=11、マイナ/両方=61
API = "https://license-test-tokyo-prd-police-pref-api.tokyo-madoguchi-yoyaku.com"
API_HEADERS = {"user-agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
               "accept": "application/json, text/javascript, */*; q=0.01",
               "content-type": "application/json; charset=UTF-8",
               "origin": "https://license-test.tokyo-madoguchi-yoyaku.com",
               "referer": "https://license-test.tokyo-madoguchi-yoyaku.com/"}
WATCH_MONTHS = ["202610", "202611"]                   # 問い合わせる月（9月・12月は見ない）
NOTIFY_FIRST_N = 5                                     # 残り人数の増加は、早い順この日数までを通知（それより先は記録のみ）
KINDS = {"both": "免許証及びマイナ免許証の両方", "only": "免許証のみ"}
JP = {"fuchu": "府中", "samezu": "鮫洲", "koto": "江東", "both": "両方", "only": "免許証のみ"}
W = "月火水木金土日"

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)
DEBUG_DIR = BASE / "debug"
DEBUG_DIR.mkdir(exist_ok=True)
LATEST = DATA / "latest.json"
SCANS = DATA / "scans.csv"
EVENTS = DATA / "events.csv"
SENT = DATA / "summary_sent.json"


def steps_for(site, kind):
    return [
        {"kind": "click", "text": "学科試験の予約はこちら"},
        {"kind": "click", "text": "「利用規約について」を読み、同意しました。"},
        {"kind": "click", "text": "手続を開始する"},
        {"kind": "click", "text": "空き状況カレンダー"},
        {"kind": "click", "text": "教習所卒業等"},
        {"kind": "click", "text": KINDS[kind]},
        {"kind": "click", "text": SITES[site]},
    ]


CAL_JS = """() => {
  const t = document.querySelector('.ui-datepicker-title');
  const cells = [...document.querySelectorAll('table.ui-datepicker-calendar td')]
    .filter(td => !td.classList.contains('ui-datepicker-other-month'))
    .map(td => ({ d: td.innerText.trim(), ok: !td.classList.contains('ui-datepicker-unselectable') }));
  const nx = document.querySelector('.ui-datepicker-next');
  return { title: t ? t.innerText.trim() : '', cells, nextDisabled: !nx || nx.classList.contains('ui-state-disabled') };
}"""


def _ym(page):
    cal = page.evaluate(CAL_JS)
    m = re.search(r"(\d{4})年\s*(\d{1,2})月", cal["title"])
    if not m:
        raise RuntimeError(f"年月が読めない: {cal['title']!r}")
    return int(m.group(1)), int(m.group(2)), cal


def read_dates(page):
    page.wait_for_selector("table.ui-datepicker-calendar", timeout=20000)
    out = []
    for _ in range(MONTHS):
        y, mo, cal = _ym(page)
        out += [f"{y:04d}-{mo:02d}-{int(c['d']):02d}" for c in cal["cells"] if c["ok"] and c["d"].isdigit()]
        if cal["nextDisabled"]:
            break
        page.locator(".ui-datepicker-next").first.click()
        page.wait_for_function("t => (document.querySelector('.ui-datepicker-title')||{}).innerText !== t", arg=cal["title"], timeout=10000)
    return out


def goto_month(page, y, mo):
    for _ in range(MONTHS * 2):
        cy, cm, _ = _ym(page)
        if (cy, cm) == (y, mo):
            return
        sel = ".ui-datepicker-next" if (cy, cm) < (y, mo) else ".ui-datepicker-prev"
        page.locator(sel).first.click()
        page.wait_for_timeout(300)
    raise RuntimeError(f"{y}/{mo} に移動できない")


SLOT_RE = re.compile(r"(午前|午後)試験（受付時間\s*(\d{1,2}:\d{2})）.*?残り\s*(\d+)\s*名")


def read_counts(page, d):
    y, mo, dd = map(int, d.split("-"))
    goto_month(page, y, mo)
    page.locator("table.ui-datepicker-calendar td:not(.ui-datepicker-unselectable):not(.ui-datepicker-other-month) a",
                 has_text=re.compile(rf"^{dd}$")).first.click()
    try:
        page.get_by_text(re.compile(r"残り\s*\d+\s*名")).first.wait_for(timeout=8000)
        page.wait_for_timeout(500)
    except Exception:
        pass
    txt = page.inner_text("body")
    txt = txt[txt.find("受付時間を選択"):] if "受付時間を選択" in txt else txt
    return {am: int(n) for am, _, n in SLOT_RE.findall(txt)}


def _click_text(page, t):
    loc = page.get_by_text(t, exact=True)
    loc.locator("visible=true").first.click(timeout=20000)


def scan_one(browser, site, kind):
    page = new_page(browser, viewport={"width": 1280, "height": 900})
    try:
        page.goto(START_URL, timeout=45000, wait_until="domcontentloaded")
        for s in steps_for(site, kind):
            _click_text(page, s["text"])
            page.wait_for_load_state("domcontentloaded")
        selectable = read_dates(page)
        # 選べる日でも「従来の免許証」の残りが0名の日がある（別の枠の空きで選べる状態）→ 人数を見て判定
        counts, zero, found = {}, [], 0
        for d in selectable[:MAX_CLICKS]:
            try:
                c = read_counts(page, d)
            except Exception as e:
                counts[d] = {"error": str(e)[:80]}
                continue
            if sum(v for v in c.values() if isinstance(v, int)) > 0:
                counts[d] = c; found += 1
                if found >= DETAIL_DAYS:
                    break
            else:
                zero.append(d)
        dates = [d for d in selectable if d not in zero]
        return {"dates": dates, "counts": counts, "zero": zero}
    finally:
        page.context.close()


def _api(method, path, params=None, body=None):
    url = f"{API}/{path}" + ("?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=API_HEADERS)
    d = json.loads(urllib.request.urlopen(req, timeout=30).read())
    if d.get("code") != "A0001":
        raise RuntimeError(f"API code {d.get('code')}")
    return d


def _slotname(row):
    return "午前" if row.get("starttime", "") < "1000" else "午後"


_MONTH_CACHE = {}   # (site, kind, month) -> 直近のcalgetres応答
_DUE = {}           # (site, kind) -> 次のスキャンで問い合わせる月の集合（Noneなら全部）


def api_scan(site, kind):
    """予約画面のカレンダーと同じデータ（calgetres）を月ごとに取得。1か月1回の問い合わせで全日・全時間帯の残りがわかる"""
    today = date.today()
    months = [m for m in WATCH_MONTHS if m >= f"{today:%Y%m}"]
    counts, ages, cts = {}, [], {}
    due = None                                  # 毎回2か月とも問い合わせる（更新のずれに確実に追従するため）
    fresh = False
    for ym in months:
        cached = _MONTH_CACHE.get((site, kind, ym))
        if due is not None and ym not in due and cached:
            d = cached
        else:
            d = _api("GET", "calgetres", {"date": ym, "coursecode": COURSE[kind], "placecode": PLACE[site], "user": "pub"})
            _MONTH_CACHE[(site, kind, ym)] = d
            fresh = True
        ct = float(d.get("currenttime", time.time()))
        cts[ym] = ct
        ages.append(round(time.time() - ct))
        for row in d.get("body", []):
            ds = f"{row['date'][:4]}-{row['date'][4:6]}-{row['date'][6:]}"
            if ds <= today.isoformat():
                continue
            left = max(0, int(row["capacity"]) - int(row["reservation"]))
            counts.setdefault(ds, {})[_slotname(row)] = left
    dates = sorted(d for d, c in counts.items() if sum(c.values()) > 0)
    return {"dates": dates, "counts": {d: counts[d] for d in dates}, "cache_age": max(ages) if ages else None, "cts": cts, "fresh": fresh}


def live_counts(site, kind, d):
    """最新の残り人数（getres）。空きを見つけたときだけ1回呼ぶ"""
    try:
        r = _api("POST", "getres", body={"date": d.replace("-", ""), "coursecode": COURSE[kind], "placecode": PLACE[site]})
        out = {}
        for row in r.get("body", []):
            out[_slotname(row)] = max(0, int(row["capacity"]) - int(row["reservation"]))
        return out
    except Exception:
        return None


LIVE_LOG = DATA / "live.csv"
EARLY_LOG = DATA / "early.csv"          # 手前の枠の記録（調査の主目的）
TRACK_STEP_SEC = 10                    # 取れる手前の枠は、埋まるまで10秒ごとに追跡
TRACK_MAX_SEC = 600                    # 最長10分
PHANTOM_CHECKS = [30, 60]              # 検知時0名だった枠は、念のため30秒後・60秒後にも確認
_PENDING = []                          # (実行時刻, 検知ID, 検知からの秒数)
_TRACK = {}                            # 検知ID -> 追跡中の情報
EARLY_HEADER = ["detect_id", "detect_time", "site", "kind", "date", "prev_first", "days_earlier",
                "bookable", "seats_at_detect", "max_seats", "bookable_sec", "end"]


def log_live(det_id, site, kind, d, offset, counts, ct_age=None):
    total = sum(v for v in (counts or {}).values()) if counts is not None else ""
    append(LIVE_LOG, ["detect_id", "time", "site", "kind", "date", "offset_sec", "am", "pm", "total", "cache_age"],
           [[det_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), site, kind, d, offset,
             (counts or {}).get("午前", ""), (counts or {}).get("午後", ""), total, ct_age if ct_age is not None else ""]])


def start_track(site, kind, d, prev_first, live):
    """手前の枠を検知したときに呼ぶ。取れる枠なら埋まるまで追跡、0名なら30秒・60秒後に再確認"""
    det_id = f"{datetime.now():%m%d%H%M%S}_{site}_{d}"
    total = sum(live.values()) if live else 0
    log_live(det_id, site, kind, d, 0, live)
    t0 = time.time()
    _TRACK[det_id] = {"t0": t0, "site": site, "kind": kind, "date": d, "prev_first": prev_first,
                      "detect_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                      "bookable": total > 0, "seats0": total, "max": total, "last_ok": 0 if total > 0 else None}
    if total > 0:
        _PENDING.append((t0 + TRACK_STEP_SEC, det_id, TRACK_STEP_SEC))
    else:
        for off in PHANTOM_CHECKS:
            _PENDING.append((t0 + off, det_id, off))
    return det_id


def _finish(det_id, end):
    t = _TRACK.pop(det_id, None)
    if not t:
        return
    days = (date.fromisoformat(t["prev_first"]) - date.fromisoformat(t["date"])).days if t["prev_first"] else ""
    append(EARLY_LOG, EARLY_HEADER, [[det_id, t["detect_time"], t["site"], t["kind"], t["date"], t["prev_first"], days,
                                      int(t["bookable"]), t["seats0"], t["max"],
                                      t["last_ok"] if t["bookable"] else "", end]])


def run_pending():
    now = time.time()
    for p in sorted([p for p in _PENDING if p[0] <= now]):
        _PENDING.remove(p)
        _, det_id, off = p
        t = _TRACK.get(det_id)
        if not t:
            continue
        live = live_counts(t["site"], t["kind"], t["date"])
        log_live(det_id, t["site"], t["kind"], t["date"], off, live)
        total = sum(live.values()) if live is not None else None
        if t["bookable"]:
            if total:                         # まだ取れる
                t["last_ok"] = off; t["max"] = max(t["max"], total)
                if off + TRACK_STEP_SEC <= TRACK_MAX_SEC:
                    _PENDING.append((t["t0"] + off + TRACK_STEP_SEC, det_id, off + TRACK_STEP_SEC))
                else:
                    _finish(det_id, "timeout")
            elif total == 0:                  # 埋まった
                _finish(det_id, "gone")
            else:                             # 通信エラー → 次で再確認
                _PENDING.append((t["t0"] + off + TRACK_STEP_SEC, det_id, off + TRACK_STEP_SEC))
        else:
            if total:
                t["reappeared"] = off; t["max"] = max(t["max"], total)
            if off >= PHANTOM_CHECKS[-1]:
                _finish(det_id, "phantom" if not t.get("reappeared") else f"reappeared@{t['reappeared']}")
    return min((p[0] for p in _PENDING), default=None)


def append(path, header, rows):
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerows(rows)


def fmt_d(d):
    x = date.fromisoformat(d)
    return f"{x.month}/{x.day}({W[x.weekday()]})"


def discord(text):
    if not WEBHOOK:
        print("[discord未設定]\n" + text)
        return
    for i in range(0, len(text), 1900):
        body = json.dumps({"content": text[i:i + 1900], "username": "本免ウォッチ"}).encode()
        req = urllib.request.Request(WEBHOOK, data=body, method="POST",
                                     headers={"content-type": "application/json", "user-agent": "menkyo-watch"})
        try:
            urllib.request.urlopen(req, timeout=20).read()
        except Exception as e:
            print("Discord送信失敗:", e)
            with (DATA / "discord_errors.txt").open("a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%m/%d %H:%M} {e}\n")
        time.sleep(1)


def booking_messages(site, kind, d, counts):
    """本番通知：そのまま予約に進めるよう、手順と入力情報を送る（情報は1項目ずつ別メッセージ＝長押しでコピーしやすい）"""
    slots = [f"{k}試験（受付 {'8:00' if k == '午前' else '11:00'}）残り{n}名" for k, n in (counts or {}).items() if isinstance(n, int) and n > 0]
    msgs = ["\n".join([
        "━━━━━━━━━━━━━━",
        "🚗 **今すぐ予約する手順**",
        f"① 予約サイト：{BOOK_URL}",
        "② （今の予約がある場合）先に「予約状況確認/キャンセル」でキャンセル",
        "③ 利用規約に同意 →「手続を開始する」→「学科試験」",
        f"④「教習所卒業等」→「{KINDS[kind]}」",
        f"⑤ 受験場所「{SITES[site]}」→ 日付 **{fmt_d(d)}**",
        "⑥ 受付時間：" + ("、".join(slots) if slots else "空いている方"),
        "⑦ 下の情報をコピーして入力 → 予約完了画面のQRコードと受付番号を保存",
        "━━━━━━━━━━━━━━",
    ])]
    for line in [l.strip() for l in BOOKING_INFO.splitlines() if l.strip()]:
        msgs.append(line)
    if not BOOKING_INFO:
        msgs.append("（入力情報が未登録です：GitHub Secrets の BOOKING_INFO に登録してください）")
    return msgs


def _is_target(d, prev_first):
    """本番で通知すべき日付か：自分の予約日（MY_DATE）より前、未設定なら直前の最短日より前"""
    if MY_DATE:
        return d < MY_DATE
    return bool(prev_first) and d < prev_first


def diff(prev, cur, now):
    """前回との差分をイベントにする"""
    ev = []
    pd, cd = set(prev.get("dates", [])), set(cur.get("dates", []))
    prev_first = min(pd) if pd else None
    for d in sorted(cd - pd):
        ev.append([now, "date_open", d, "", "", "", "earlier" if prev_first and d < prev_first else ""])
    for d in sorted(pd - cd):
        ev.append([now, "date_close", d, "", "", "", ""])
    # 「選べるけど残り0名」の日の出入り（キャンセル枠がすぐ埋まった名残か、手続き中の仮押さえかを見分ける）
    pz, cz = set(prev.get("zero", [])), set(cur.get("zero", []))
    for d in sorted(cz - pz):
        ev.append([now, "zero_on", d, "", "", "", "from_open" if d in pd else "from_closed"])
    for d in sorted(pz - cz):
        ev.append([now, "zero_off", d, "", "", "", "to_open" if d in cd else "to_closed"])
    for d, c in cur.get("counts", {}).items():
        pc = prev.get("counts", {}).get(d, {})
        for slot, n in c.items():
            if slot == "error" or slot not in pc or pc[slot] == n:
                continue
            if n < pc[slot] and d not in cur.get("dates", [])[:NOTIFY_FIRST_N]:
                continue   # 先の日付の通常の予約（人数減少）は記録しない
            ev.append([now, "count_up" if n > pc[slot] else "count_down", d, slot, pc[slot], n, ""])
    return ev


def recent_phantoms(site, minutes=15):
    """直近に「一瞬写っただけ」と判定した日（再起動直後でも最短日の計算から外すため）"""
    lim = (datetime.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    return {r["date"] for r in _rows(EARLY_LOG) if r["site"] == site and r["bookable"] == "0" and r["detect_time"] >= lim} \
        | {t["date"] for t in _TRACK.values() if t["site"] == site and not t["bookable"]}


def bookable_first(entry):
    """予約できる最短日（一瞬写っただけの日は除く）"""
    ph = set(entry.get("phantom", []))
    return next((d for d in entry.get("dates", []) if d not in ph), None)


def run(targets=None):
    targets = targets or FAST + SLOW
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    prev_all = json.loads(LATEST.read_text(encoding="utf-8")) if LATEST.exists() else {}
    cur_all, scan_rows, ev_rows, alerts = {}, [], [], []
    cancels = []   # キャンセル検知 (site, kind, date, counts, 内容)
    base = baseline()
    if True:
        for site, kind in targets:
            if True:
                key = f"{site}_{kind}"
                t0 = time.time()
                try:
                    cur = api_scan(site, kind)
                    if not cur.pop("fresh", True):
                        cur_all[key] = dict(prev_all.get(key, {}), **{k: v for k, v in cur.items() if k == "cts"}) if prev_all.get(key) else cur
                        continue          # 問い合わせなし（前回と同じデータ）→ 比較も記録もしない
                    prev = prev_all.get(key)
                    # 一瞬写っただけ（最新では0名）の日は、カレンダーに残っている間「phantom」として最短日の計算から外す
                    cur["phantom"] = [d for d in set((prev or {}).get("phantom", [])) | recent_phantoms(site) if d in cur["dates"]]
                    comparable = prev and "dates" in prev and "cts" in prev   # 旧方式（ブラウザ）のデータとは比較しない
                    if comparable:
                        for e in diff(prev, cur, now):
                            ev_rows.append([e[0], site, kind] + e[1:])
                        prev_first = bookable_first(prev)
                        pc = prev.get("counts", {})
                        for d in cur["dates"]:
                            if not prev_first or d >= prev_first:
                                break
                            opened = d not in prev["dates"]
                            grew = d in prev.get("phantom", []) and sum(cur["counts"].get(d, {}).values()) > sum(pc.get(d, {}).values())
                            if not (opened or grew):
                                continue
                            # 最短日より手前に空き → 最新の残り人数で本当に取れるか確かめる
                            live = live_counts(site, kind, d)
                            start_track(site, kind, d, prev_first, live)
                            if live is not None and sum(live.values()) == 0:
                                if d not in cur["phantom"]:
                                    cur["phantom"].append(d)
                                continue
                            cancels.append((site, kind, d, live if live is not None else cur["counts"].get(d),
                                            f"最短日（{fmt_d(prev_first)}）より手前に空き", prev_first, live is not None))
                    first = bookable_first(cur) or ""
                    fc = cur["counts"].get(first, {})
                    scan_rows.append([now, site, kind, "ok", first, len(cur["dates"]),
                                      fc.get("午前", ""), fc.get("午後", ""), cur.get("cache_age", "")])
                    cur_all[key] = cur
                except Exception as e:
                    scan_rows.append([now, site, kind, "error", "", "", "", "", round(time.time() - t0, 1)])
                    cur_all[key] = dict(prev_all.get(key, {}), error=f"{e.__class__.__name__}: {str(e)[:120]}")
                    print(key, "error", e)
                time.sleep(1)

    merged = dict(prev_all); merged.update(cur_all)
    LATEST.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    append(SCANS, ["time", "site", "kind", "result", "earliest", "n_dates", "am_left", "pm_left", "sec_or_cache_age"], scan_rows)
    if ev_rows:
        append(EVENTS, ["time", "site", "kind", "event", "date", "slot", "old", "new", "note"], ev_rows)
    print(f"{now} scan done: {sum(r[3] == 'ok' for r in scan_rows)}/{len(scan_rows)} ok, {len(ev_rows)} events")

    errors = [r for r in scan_rows if r[3] == "error"]
    if errors and len(errors) == len(scan_rows):
        _notify_error_once("スキャンがすべて失敗しました。サイトの画面が変わった可能性があります。")
    if cancels:
        notify_cancels(cancels, base)
    maybe_summary()
    return cur_all


def notify_cancels(cancels, base):
    """手前に出た、最新確認で取れる枠だけを通知する（run() で確認済み）"""
    cancels.sort(key=lambda c: (c[0] != FAV, c[2]))          # 江東を先頭、次に早い日
    lines, ok_cancels = [], []
    for site, kind, d, counts, what, prev_first, confirmed in cancels[:8]:
        what += "（最新確認済）" if confirmed else "（最新は確認できず）"
        b = base.get(f"{site}_{kind}")
        early = " 🔥**記録開始時より早い**" if b and d < b else ""
        tag = "⭐ **【江東】**" if site == FAV else f"**{JP[site]}**"
        lines.append(f"{tag} **{fmt_d(d)}**　{what}" + (f"（残り {_slot(counts)}）" if counts else "") + early)
        ok_cancels.append((site, kind, d, counts, what))
    if not lines:
        return
    if MODE != "hunt":
        discord("🟢 **予約できる手前の枠が出ました**（調査期間中・予約はまだできません）\n" + "\n".join(lines))
        return
    # 本番：即時通知＋予約リンクと入力情報（今の予約日 MY_DATE より早いものがあればそれを案内）
    target = [c for c in ok_cancels if not MY_DATE or c[2] < MY_DATE]
    discord(("@here " if target else "") + "🚨 **キャンセル枠が出ました！**\n" + "\n".join(lines)
            + ("" if target else f"\n（今の予約日 {fmt_d(MY_DATE)} より早い枠ではないため、予約案内は省略）"))
    if target:
        site, kind, d, counts, _ = target[0][:5]
        for m in booking_messages(site, kind, d, counts):
            discord(m)


def _notify_error_once(msg):
    flag = DATA / "error.flag"
    if flag.exists() and time.time() - flag.stat().st_mtime < 6 * 3600:
        return
    flag.write_text(msg)
    discord("⚠️ " + msg)


def maybe_summary():
    now = datetime.now()
    if now.hour not in SUMMARY_HOURS:
        return
    sent = json.loads(SENT.read_text()) if SENT.exists() else {}
    k = f"{now:%Y-%m-%d}_{now.hour}"
    if sent.get(k):
        return
    discord(summary())
    sent[k] = True
    SENT.write_text(json.dumps(sent))


def _rows(path):
    return list(csv.DictReader(path.open(encoding="utf-8"))) if path.exists() else []


DATE_EVENTS_VALID_FROM = "2026-09-26 21:57"   # これより前の日付の出入り記録は「残り0名の日」を誤判定していたため集計から除外
FAV = "koto"   # 第一希望の試験場（レポートの一番上に強調表示）
ORDER = ["koto", "fuchu", "samezu"]


def baseline():
    """記録開始時（最初に成功したスキャン）の最短日。これより早い日＝キャンセル等で前進した枠"""
    base = {}
    for r in _rows(SCANS):
        k = f"{r['site']}_{r['kind']}"
        if k not in base and r["result"] == "ok" and r["earliest"]:
            base[k] = r["earliest"]
    return base


def _slot(c):
    return f"午前{c.get('午前', '?')} / 午後{c.get('午後', '?')}"


def _early_rows():
    return _rows(EARLY_LOG)


def _early_stats(rows):
    n = len(rows)
    book = [r for r in rows if r["bookable"] == "1"]
    phantom = [r for r in rows if r["bookable"] == "0"]
    unknown = [r for r in rows if r["bookable"] == ""]
    by_site = Counter(r["site"] for r in rows)
    return n, book, phantom, unknown, by_site


def summary(hours=12):
    latest = json.loads(LATEST.read_text(encoding="utf-8")) if LATEST.exists() else {}
    scans = _rows(SCANS)
    start = datetime.fromisoformat(scans[0]["time"]) if scans else datetime.now()
    span_h = max(1.0, (datetime.now() - start).total_seconds() / 3600)
    rows = _early_rows()
    since = datetime.now() - timedelta(hours=hours)
    recent = [r for r in rows if datetime.fromisoformat(r["detect_time"]) >= since]

    L = [f"📋 **本免 学科試験 調査レポート**（{datetime.now():%m/%d %H:%M}）",
         "🎯 調査目標：**予約できる「手前」のキャンセルがどれくらい出るか**", ""]

    def block(title, rs, hours_span):
        n, book, phantom, unknown, by_site = _early_stats(rs)
        per_day = n / hours_span * 24
        L.append(f"**{title}**")
        L.append(f"・手前の枠の出現：**{n}回**（" + "・".join(f"{JP[s_]}{by_site[s_]}" for s_ in ORDER) + f"）≒ 1日あたり{per_day:.1f}回")
        L.append(f"・うち **予約できた（最新で1名以上）：{len(book)}回**")
        L.append(f"・カレンダーに一瞬写っただけ（最新では0名）：{len(phantom)}回")
        if unknown:
            L.append(f"・確認できていない（詳細記録の開始前）：{len(unknown)}回")
        secs = [int(r["bookable_sec"]) for r in book if r["bookable_sec"] not in ("", None)]
        if secs:
            ss = sorted(secs)
            fmt = lambda x: "10秒未満" if x < TRACK_STEP_SEC else f"約{x}秒"
            L.append(f"・予約できた枠が空いていた時間：中央値 {fmt(ss[len(ss) // 2])}（最短 {fmt(ss[0])}・最長 {fmt(ss[-1])}）"
                     + ("　※10分以上空いていた枠あり" if any(r["end"] == "timeout" for r in book) else ""))
        days = [int(r["days_earlier"]) for r in book if r["days_earlier"]]
        if days:
            L.append(f"・最短日より何日手前だったか：平均{sum(days) / len(days):.0f}日（最大{max(days)}日）")

    block(f"直近{hours}時間", recent, min(hours, span_h))
    L.append("")
    block(f"記録開始から（{start:%m/%d %H:%M}〜、約{span_h:.0f}時間）", rows, span_h)

    book_all = [r for r in rows if r["bookable"] == "1"]
    if book_all:
        L.append("")
        L.append("**予約できた手前の枠（新しい順）**")
        for r in sorted(book_all, key=lambda r: r["detect_time"], reverse=True)[:10]:
            star = "⭐" if r["site"] == FAV else "・"
            dur = f"{r['bookable_sec']}秒以上" if r["end"] == "timeout" else (
                ("10秒未満" if int(r["bookable_sec"]) < TRACK_STEP_SEC else f"約{r['bookable_sec']}秒") if r["bookable_sec"] else "?")
            L.append(f"{star}{r['detect_time'][5:16]}　{JP[r['site']]} **{fmt_d(r['date'])}**（{r['days_earlier']}日手前）"
                     f"　{r['seats_at_detect']}名分・空いていた時間 {dur}")

    hrs = Counter(datetime.fromisoformat(r["detect_time"]).hour for r in rows)
    if hrs:
        L.append("")
        L.append("手前の枠が出た時間帯：" + "  ".join(f"{h}時:{n}" for h, n in sorted(hrs.items()))
                 + ("（予約できた枠：" + "  ".join(f"{h}時:{n}" for h, n in sorted(Counter(datetime.fromisoformat(r['detect_time']).hour for r in book_all).items())) + "）" if book_all else ""))

    # 今の最短日
    L.append("")
    L.append("**今の最短日**（免許証のみ・午前/午後の残り）")
    for site in ORDER:
        s_ = latest.get(f"{site}_only", {})
        if bookable_first(s_):
            f = bookable_first(s_); c = s_.get("counts", {}).get(f, {})
            L.append(f"{'⭐' if site == FAV else '・'}{JP[site]}　**{fmt_d(f)}**　{c.get('午前', '?')}/{c.get('午後', '?')}")
        else:
            L.append(f"・{JP[site]}　{'10〜11月は空きなし' if 'dates' in s_ else '取得失敗'}")

    L.append("")
    L.append(f"⏱ 確認のしかた：3試験場とも、カレンダーのデータが作り直される予定（5分ごと）の約3秒後と、"
             f"一定間隔（江東{PEAK_SEC}〜{FAST_SEC}秒・他{OTHER_SEC}秒）の両方で確認")
    L.append("🎯 本番モード：取れる手前の枠はその場で通知" if MODE == "hunt"
             else "🔍 調査モード（10/2まで）：取れる手前の枠だけその場で通知、このレポートは8時・20時")
    return "\n".join(L)


def commit_push():
    cmds = [["git", "add", "-A", "--", "data"],
            ["git", "commit", "-q", "-m", f"data {datetime.now():%m/%d %H:%M}"]]
    for c in cmds:
        if subprocess.run(c).returncode != 0:
            return
    for _ in range(3):
        if subprocess.run(["git", "pull", "-q", "--rebase", "-X", "theirs", "origin", "main"]).returncode == 0 and \
           subprocess.run(["git", "push", "-q", "origin", "HEAD:main"]).returncode == 0:
            return
        time.sleep(5)


_CODE_HASH = None


def _code_changed():
    import hashlib
    global _CODE_HASH
    h = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if _CODE_HASH is None:
        _CODE_HASH = h
    return h != _CODE_HASH


CACHE_LOG = DATA / "cache.csv"


class CacheClock:
    """calgetres のデータ作成時刻（currenttime）を記録し、更新周期（TTL）を推定して次の更新時刻を予測する"""

    def __init__(self):
        self.cts = defaultdict(list)          # (site, month) -> 観測した作成時刻（重複なし）
        for r in _rows(CACHE_LOG):
            self.cts[(r["site"], r["month"])].append(float(r["currenttime"]))
        for k in self.cts:
            self.cts[k] = sorted(set(self.cts[k]))[-200:]

    def observe(self, site, cts):
        rows = []
        for m, ct in cts.items():
            lst = self.cts[(site, m)]
            if not lst or abs(ct - lst[-1]) > 1:
                lst.append(ct)
                rows.append([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), site, m, f"{ct:.1f}"])
        if rows:
            append(CACHE_LOG, ["time", "site", "month", "currenttime"], rows)

    def ttl(self, site, month):
        lst = self.cts.get((site, month), [])
        diffs = [b - a for a, b in zip(lst, lst[1:]) if 5 <= b - a <= 1800]
        return min(diffs) if len(diffs) >= 3 else None

    def next_refresh(self, site, month):
        lst, t = self.cts.get((site, month), []), self.ttl(site, month)
        if not lst or not t:
            return None
        return lst[-1] + t + EXPIRY_MARGIN_SEC

    def summary(self, site):
        return {m: self.ttl(s_, m) for (s_, m) in self.cts if s_ == site}


_PEAK_CACHE = {}


def peak_hours():
    """キャンセルが多い時間帯。36時間以上データが貯まったら実測から決める"""
    h = datetime.now().strftime("%Y%m%d%H")
    if h in _PEAK_CACHE:
        return _PEAK_CACHE[h]
    ev = [e for e in _rows(EVENTS) if e["event"] in ("count_up", "date_open") and e["time"] >= DATE_EVENTS_VALID_FROM]
    hours = DEFAULT_PEAK_HOURS
    if ev:
        span = datetime.fromisoformat(ev[-1]["time"]) - datetime.fromisoformat(ev[0]["time"])
        if span >= timedelta(hours=36):
            c = Counter(datetime.fromisoformat(e["time"]).hour for e in ev)
            avg = sum(c.values()) / 24
            hours = {hh for hh, n in c.items() if n >= avg} or DEFAULT_PEAK_HOURS
    _PEAK_CACHE.clear(); _PEAK_CACHE[h] = hours
    return hours


def dispatch_next():
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        return
    r = subprocess.run(["gh", "workflow", "run", "loop.yml", "-R", repo, "--ref", "main"])
    print("次のループを起動:", "OK" if r.returncode == 0 else "失敗")


CACHE_TTL = 300          # 予約サイトのカレンダーデータは5分で作り直される（観測値）
OTHER_SEC = 60           # 府中・鮫洲の通常の確認間隔


def loop(hours=5.6):
    """各試験場を「前回のデータ作成時刻＋5分＋3秒」と「一定間隔（江東30〜60秒、他60秒）」の早い方で確認。
    データの作り直しのタイミングがずれることがあるため、一定間隔の確認も併用する。終了2分前に次のループを起動"""
    _code_changed()
    clock = CacheClock()
    end = time.time() + hours * 3600
    now = time.time()
    next_due = {t: now for t in FAST + SLOW}
    next_commit = now + COMMIT_MIN * 60
    dispatched, n = False, 0
    while True:
        now = time.time()
        if now >= end - 30 and not (_TRACK and now < end + 10 * 60):   # 追跡中なら最長10分延長
            break
        if not dispatched and now >= end - 120:
            dispatch_next(); dispatched = True
        targets = [t for t, due in next_due.items() if due <= now]
        if targets:
            try:
                res = run(targets)
                n += 1
            except Exception as e:
                res = {}
                print("run失敗:", e)
                _notify_error_once(f"スキャン処理でエラー: {e.__class__.__name__}")
            t_now = time.time()
            for site, kind in targets:
                interval = (PEAK_SEC if datetime.now().hour in peak_hours() else FAST_SEC) if site == FAV else OTHER_SEC
                nxt = t_now + interval
                cts = (res.get(f"{site}_{kind}") or {}).get("cts", {})
                clock.observe(site, cts)
                for ct in cts.values():
                    exp = ct + CACHE_TTL + EXPIRY_MARGIN_SEC
                    while exp < t_now + MIN_GAP_SEC:
                        exp += CACHE_TTL
                    nxt = min(nxt, exp)       # データの作り直し予定の直後に前倒し
                next_due[(site, kind)] = nxt
        next_pending = run_pending()
        if now >= next_commit:
            commit_push()
            next_commit = now + COMMIT_MIN * 60
            if _code_changed() and not _TRACK:     # 追跡中は再起動を先送り
                left = (end - time.time()) / 3600
                print("watch.py が更新されたので再起動")
                os.execv(sys.executable, [sys.executable, __file__, "loop", f"{left:.4f}"])
        wake = min(min(next_due.values()), next_commit, max(end - 30, now + 1), (end - 120) if not dispatched else end,
                   next_pending or float("inf"))
        time.sleep(max(1.0, wake - time.time()))
    for det_id in list(_TRACK):
        _finish(det_id, "interrupted")
    commit_push()
    print(f"loop終了: {n}回")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "run":
        t0 = time.time(); run(); print(f"所要 {time.time() - t0:.0f}秒")
    elif cmd == "loop":
        loop(float(sys.argv[2]) if len(sys.argv) > 2 else 5.6)
    elif cmd == "report":
        discord(summary())
    elif cmd == "debugdate":
        site, kind, d = sys.argv[2], sys.argv[3], sys.argv[4]
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True); page = new_page(b, viewport={"width": 1280, "height": 900})
            page.goto(START_URL, wait_until="domcontentloaded")
            for st in steps_for(site, kind):
                _click_text(page, st["text"]); page.wait_for_load_state("domcontentloaded")
            ds = read_dates(page)
            c = read_counts(page, d) if d in ds else "not selectable"
            (DEBUG_DIR / "debugdate.txt").write_text(f"{d} in dates={d in ds}\ncounts={c}\n\n" + page.inner_text("body"), encoding="utf-8")
            page.screenshot(path=str(DEBUG_DIR / "debugdate.png"), full_page=True)
    elif cmd == "netlog":
        site, kind = sys.argv[2], sys.argv[3]
        log = []
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True); page = new_page(b, viewport={"width": 1280, "height": 900})
            def on_resp(r):
                req = r.request
                if req.resource_type in ("xhr", "fetch", "document"):
                    try:
                        body = r.text()[:3000]
                    except Exception:
                        body = "(binary)"
                    log.append({"step": step[0], "type": req.resource_type, "method": req.method, "url": req.url,
                                "post": (req.post_data or "")[:1000], "status": r.status,
                                "req_headers": {k: v for k, v in req.headers.items() if k.lower() in ("content-type", "cookie", "x-requested-with", "referer")},
                                "body": body})
            step = ["goto"]
            page.on("response", on_resp)
            page.goto(START_URL, wait_until="domcontentloaded"); page.wait_for_timeout(1500)
            for st in steps_for(site, kind):
                step[0] = st["text"]; _click_text(page, st["text"]); page.wait_for_load_state("domcontentloaded"); page.wait_for_timeout(1500)
            step[0] = "next-month"; page.locator(".ui-datepicker-next").first.click(); page.wait_for_timeout(2000)
            ds = read_dates(page)
            step[0] = "click-date"
            if ds:
                read_counts(page, ds[0])
            page.wait_for_timeout(1500)
            cookies = page.context.cookies()
        (DEBUG_DIR / "netlog.json").write_text(json.dumps({"log": log, "cookies": [{k: c[k] for k in ("name", "domain", "path")} for c in cookies]}, ensure_ascii=False, indent=1), encoding="utf-8")
    elif cmd == "grepjs":
        out = {}
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True); page = new_page(b)
            page.goto(START_URL, wait_until="domcontentloaded")
            for t in ["学科試験の予約はこちら", "「利用規約について」を読み、同意しました。", "手続を開始する"]:
                _click_text(page, t); page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(1500)
            for label, url in [("booking", page.url), ("calendar", page.url.replace("/01/html/", "/calendar/01/html/"))]:
                page.goto(url, wait_until="domcontentloaded"); page.wait_for_timeout(1500)
                out[label] = page.evaluate("""async () => { const r = {};
                  for (const s of document.querySelectorAll('script[src]')) {
                    const t = await (await fetch(s.src)).text();
                    const hits = []; let i = -1;
                    for (const kw of ['userInfo']) {
                      let i = -1; while ((i = t.indexOf(kw, i + 1)) >= 0 && hits.length < 20) hits.push('[' + kw + '] ' + t.slice(Math.max(0, i - 250), i + 350)); }
                    r[s.src] = hits; }
                  return r; }""")
        (DEBUG_DIR / "grepjs.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    elif cmd == "compare":
        # カレンダーのデータ（calgetres）と最新（getres）を全日付で比較（1回限りの調査用）
        site, kind = sys.argv[2], sys.argv[3]
        out = {"at": datetime.now().strftime("%H:%M:%S"), "rows": []}
        for ym in WATCH_MONTHS:
            d = _api("GET", "calgetres", {"date": ym, "coursecode": COURSE[kind], "placecode": PLACE[site], "user": "pub"})
            out[f"age_{ym}"] = round(time.time() - float(d["currenttime"]))
            cal = defaultdict(dict)
            for row in d["body"]:
                cal[row["date"]][_slotname(row)] = (int(row["capacity"]), int(row["reservation"]))
            for ds in sorted(cal):
                if ds <= datetime.now().strftime("%Y%m%d"):
                    continue
                lv = _api("POST", "getres", body={"date": ds, "coursecode": COURSE[kind], "placecode": PLACE[site]})
                live = {_slotname(r): (int(r["capacity"]), int(r["reservation"])) for r in lv.get("body", [])}
                out["rows"].append({"date": ds, "cal": cal[ds], "live": live})
                time.sleep(0.5)
        (DEBUG_DIR / "compare.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    elif cmd == "testhunt":
        discord("🧪 **本番通知のテスト（実際の空きではありません）**")
        for m in [f"@here ⭐🔥 **【江東】** **キャンセル枠が出ました！** 江東・両方　**{fmt_d('2026-11-19')}**　残り 午前1 / 午後0"] + \
                 booking_messages("koto", "both", "2026-11-19", {"午前": 1, "午後": 0}):
            discord(m)
    elif cmd == "test":
        (DEBUG_DIR / "discord_test.txt").write_text(f"{datetime.now():%m/%d %H:%M} webhook_set={bool(WEBHOOK)}", encoding="utf-8")
        discord("✅ 本免ウォッチの通知テストです。これが見えていれば設定OK")
        discord(summary())
    else:
        print(__doc__)
