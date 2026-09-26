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

from playwright.sync_api import sync_playwright

from monitor import START_URL, new_page, settle, do_step

MODE = os.environ.get("MODE", "observe")            # observe（10/2まで）/ hunt（卒業後）
WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
SUMMARY_HOURS = {8, 22}                              # この時刻台の最初の実行でまとめを送る
DETAIL_DAYS = 3                                      # 早い順に何日分、残り人数を読むか
MONTHS = 4

SITES = {"fuchu": "府中試験場", "samezu": "鮫洲試験場", "koto": "江東試験場"}
KINDS = {"both": "免許証及びマイナ免許証の両方", "only": "免許証のみ"}
JP = {"fuchu": "府中", "samezu": "鮫洲", "koto": "江東", "both": "両方", "only": "免許証のみ"}
W = "月火水木金土日"

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)
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
        time.sleep(0.7)
    return out


def goto_month(page, y, mo):
    for _ in range(MONTHS * 2):
        cy, cm, _ = _ym(page)
        if (cy, cm) == (y, mo):
            return
        sel = ".ui-datepicker-next" if (cy, cm) < (y, mo) else ".ui-datepicker-prev"
        page.locator(sel).first.click()
        time.sleep(0.6)
    raise RuntimeError(f"{y}/{mo} に移動できない")


SLOT_RE = re.compile(r"(午前|午後)試験（受付時間\s*(\d{1,2}:\d{2})）.*?残り\s*(\d+)\s*名")


def read_counts(page, d):
    y, mo, dd = map(int, d.split("-"))
    goto_month(page, y, mo)
    page.locator("table.ui-datepicker-calendar td:not(.ui-datepicker-unselectable):not(.ui-datepicker-other-month) a",
                 has_text=re.compile(rf"^{dd}$")).first.click()
    time.sleep(1.5)
    settle(page)
    txt = page.inner_text("body")
    txt = txt[txt.find("受付時間を選択"):] if "受付時間を選択" in txt else txt
    return {am: int(n) for am, _, n in SLOT_RE.findall(txt)}


def scan_one(browser, site, kind):
    page = new_page(browser, viewport={"width": 1280, "height": 900})
    try:
        page.goto(START_URL, timeout=45000)
        settle(page)
        for s in steps_for(site, kind):
            do_step(page, s)
            settle(page)
        dates = read_dates(page)
        counts = {}
        for d in dates[:DETAIL_DAYS]:
            try:
                counts[d] = read_counts(page, d)
            except Exception as e:
                counts[d] = {"error": str(e)[:80]}
        return {"dates": dates, "counts": counts}
    finally:
        page.context.close()


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


def diff(prev, cur, now):
    """前回との差分をイベントにする"""
    ev = []
    pd, cd = set(prev.get("dates", [])), set(cur.get("dates", []))
    prev_first = min(pd) if pd else None
    for d in sorted(cd - pd):
        ev.append([now, "date_open", d, "", "", "", "earlier" if prev_first and d < prev_first else ""])
    for d in sorted(pd - cd):
        ev.append([now, "date_close", d, "", "", "", ""])
    for d, c in cur.get("counts", {}).items():
        pc = prev.get("counts", {}).get(d, {})
        for slot, n in c.items():
            if slot == "error" or slot not in pc or pc[slot] == n:
                continue
            ev.append([now, "count_up" if n > pc[slot] else "count_down", d, slot, pc[slot], n, ""])
    return ev


def run():
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    prev_all = json.loads(LATEST.read_text(encoding="utf-8")) if LATEST.exists() else {}
    cur_all, scan_rows, ev_rows, alerts = {}, [], [], []
    base = baseline()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        for site in SITES:
            for kind in KINDS:
                key = f"{site}_{kind}"
                t0 = time.time()
                try:
                    cur = scan_one(browser, site, kind)
                    first = cur["dates"][0] if cur["dates"] else ""
                    fc = cur["counts"].get(first, {})
                    scan_rows.append([now, site, kind, "ok", first, len(cur["dates"]),
                                      fc.get("午前", ""), fc.get("午後", ""), round(time.time() - t0, 1)])
                    prev = prev_all.get(key)
                    if prev and "dates" in prev:
                        for e in diff(prev, cur, now):
                            ev_rows.append([e[0], site, kind] + e[1:])
                            if e[1] == "date_open" and e[6] == "earlier":
                                b = base.get(key)
                                cc = cur["counts"].get(e[2])
                                msg = (f"**{JP[site]}・{JP[kind]}** で **{fmt_d(e[2])}** が選べるようになりました"
                                       f"（直前の最短 {fmt_d(prev['dates'][0])}"
                                       + (f"、記録開始時 {fmt_d(b)}" if b else "") + "）"
                                       + (f"　残り {_slot(cc)}" if cc else ""))
                                if site == FAV:
                                    msg = "⭐🔥 **【江東】** " + msg
                                elif b and e[2] < b:
                                    msg = "🔥 " + msg
                                else:
                                    msg = "🟢 " + msg
                                alerts.append((site != FAV, msg))
                    cur_all[key] = cur
                except Exception as e:
                    scan_rows.append([now, site, kind, "error", "", "", "", "", round(time.time() - t0, 1)])
                    cur_all[key] = dict(prev_all.get(key, {}), error=f"{e.__class__.__name__}: {str(e)[:120]}")
                    print(key, "error", e)
                time.sleep(2)
        browser.close()

    LATEST.write_text(json.dumps(cur_all, ensure_ascii=False, indent=1), encoding="utf-8")
    append(SCANS, ["time", "site", "kind", "result", "earliest", "n_dates", "am_left", "pm_left", "sec"], scan_rows)
    if ev_rows:
        append(EVENTS, ["time", "site", "kind", "event", "date", "slot", "old", "new", "note"], ev_rows)
    print(f"{now} scan done: {sum(r[3] == 'ok' for r in scan_rows)}/6 ok, {len(ev_rows)} events")

    errors = [r for r in scan_rows if r[3] == "error"]
    if len(errors) == 6:
        _notify_error_once("6パターンすべて失敗しました。サイトの画面が変わった可能性があります。")
    if alerts:
        head = "@here " if MODE == "hunt" else ""
        tail = "\n今すぐ予約サイトへ！" if MODE == "hunt" else "\n（調査期間中：記録のみ）"
        discord(head + "\n".join(m for _, m in sorted(alerts, key=lambda a: a[0])) + tail)
    maybe_summary()


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


def summary(hours=None):
    latest = json.loads(LATEST.read_text(encoding="utf-8")) if LATEST.exists() else {}
    scans, events = _rows(SCANS), _rows(EVENTS)
    base = baseline()
    since = datetime.now() - timedelta(hours=hours) if hours else None
    if since:
        events = [e for e in events if datetime.fromisoformat(e["time"]) >= since]
    L = [f"📋 **本免 学科試験 空き状況**（{datetime.now():%m/%d %H:%M}）", ""]

    # --- 第一希望：江東 ---
    L.append(f"⭐ **{JP[FAV]}試験場（第一希望）**")
    for kind in ["only", "both"]:
        k = f"{FAV}_{kind}"; s = latest.get(k, {})
        if not s.get("dates"):
            L.append(f"> {JP[kind]}：{'空きなし' if 'dates' in s else '取得失敗'}")
            continue
        f = s["dates"][0]; c = s.get("counts", {}).get(f, {})
        b = base.get(k)
        if b and f < b:
            gain = (date.fromisoformat(b) - date.fromisoformat(f)).days
            L.append(f"> 🔥 {JP[kind]}：**{fmt_d(f)}**　{_slot(c)}　← **記録開始時（{fmt_d(b)}）より{gain}日早い！**")
        else:
            L.append(f"> {JP[kind]}：**{fmt_d(f)}**　{_slot(c)}")
        nxt = [f"{fmt_d(d)} {c2.get('午前', '?')}/{c2.get('午後', '?')}" for d, c2 in list(s.get("counts", {}).items())[1:]]
        if nxt:
            L.append(f">  　次点：" + "、".join(nxt))
    L.append("")

    # --- その他の試験場 ---
    L.append("**その他の試験場**（最短日　午前/午後の残り）")
    for site in ORDER[1:]:
        cells = []
        for kind in ["only", "both"]:
            k = f"{site}_{kind}"; s = latest.get(k, {})
            if s.get("dates"):
                f = s["dates"][0]; c = s.get("counts", {}).get(f, {})
                mark = "🔥" if base.get(k) and f < base[k] else ""
                txt = f"{mark}{fmt_d(f)} {c.get('午前', '?')}/{c.get('午後', '?')}"
                cells.append(f"{JP[kind]} **{txt}**" if mark else f"{JP[kind]} {txt}")
            else:
                cells.append(f"{JP[kind]} {'空きなし' if 'dates' in s else '取得失敗'}")
        L.append(f"・{JP[site]}　" + "　｜　".join(cells))

    # --- 早い日程（キャンセル等で出た枠） ---
    opens = [e for e in events if e["event"] == "date_open"]
    early = [e for e in opens if base.get(f"{e['site']}_{e['kind']}") and e["date"] < base[f"{e['site']}_{e['kind']}"]]
    early.sort(key=lambda e: (e["site"] != FAV, e["time"]))
    span = f"直近{hours}時間" if hours else "記録開始から"
    L.append("")
    if early:
        L.append(f"🔥 **{span}、記録開始時より早い日程が出た回数：{len(early)}回**")
        for e in early[:10]:
            star = "⭐" if e["site"] == FAV else "　"
            L.append(f"{star}{e['time'][5:]}　{JP[e['site']]}・{JP[e['kind']]} → **{fmt_d(e['date'])}**")
    else:
        L.append(f"{span}、記録開始時より早い日程はまだ出ていません")

    # --- 統計 ---
    ups = [e for e in events if e["event"] == "count_up"]
    cancels = sum(int(e["new"]) - int(e["old"]) for e in ups)
    n_scan = len({r["time"] for r in scans}); ok = sum(r["result"] == "ok" for r in scans)
    L.append("")
    L.append(f"📊 キャンセルの動き：満席の日が空いた {len(opens)}回　／　残り人数が増えた {len(ups)}回（計{cancels}名分）")
    hrs = Counter(datetime.fromisoformat(e["time"]).hour for e in opens + ups)
    if hrs:
        L.append("　時間帯別：" + "  ".join(f"{h}時:{n}" for h, n in sorted(hrs.items())))
    L.append(f"　スキャン {n_scan}回（成功 {ok}/{len(scans)}）・記録開始 {scans[0]['time'][5:] if scans else '-'}")
    L.append("🎯 本番モード" if MODE == "hunt" else "🔍 調査モード（10/2まで記録のみ）")
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


def loop(hours=5.6, interval_min=15):
    _code_changed()
    """interval_minごとにスキャン→保存をくり返す（GitHubの1ジョブ上限6時間の内側で止める）"""
    end = time.time() + hours * 3600
    n = 0
    while time.time() < end - 60:
        t0 = time.time()
        try:
            run()
        except Exception as e:
            print("run失敗:", e)
            _notify_error_once(f"スキャン処理でエラー: {e.__class__.__name__}")
        commit_push()
        n += 1
        if _code_changed():
            left = (end - time.time()) / 3600
            print("watch.py が更新されたので再起動")
            os.execv(sys.executable, [sys.executable, __file__, "loop", f"{left:.3f}"])
        wait = interval_min * 60 - (time.time() - t0)
        if time.time() + wait >= end:
            break
        time.sleep(max(30, wait))
    print(f"loop終了: {n}回")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "run":
        run()
    elif cmd == "loop":
        loop(float(sys.argv[2]) if len(sys.argv) > 2 else 5.6)
    elif cmd == "report":
        discord(summary())
    elif cmd == "test":
        (DATA / "discord_test.txt").write_text(f"{datetime.now():%m/%d %H:%M} webhook_set={bool(WEBHOOK)}", encoding="utf-8")
        discord("✅ 本免ウォッチの通知テストです。これが見えていれば設定OK")
        discord(summary())
    else:
        print(__doc__)
