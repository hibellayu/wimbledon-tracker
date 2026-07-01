#!/usr/bin/env python3
"""
Wimbledon 2026 重點選手賽程追蹤器
每 2 小時自動抓取賽程，更新 Google Calendar
"""
import os, json, hashlib, re
from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ── 設定 ─────────────────────────────────────────────────────────────────────

TAIPEI = ZoneInfo("Asia/Taipei")
BST    = ZoneInfo("Europe/London")

TRACKED_PLAYERS = [
    "sinner", "alcaraz", "djokovic", "zverev",
    "sabalenka", "andreeva", "rybakina", "osaka",
]

PLAYER_DISPLAY = {
    "sinner":    "Jannik Sinner",
    "alcaraz":   "Carlos Alcaraz",
    "djokovic":  "Novak Djokovic",
    "zverev":    "Alexander Zverev",
    "sabalenka": "Aryna Sabalenka",
    "andreeva":  "Mirra Andreeva",
    "rybakina":  "Elena Rybakina",
    "osaka":     "Naomi Osaka",
}

CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")

ROUND_MAP = {
    "round of 128":  "第一輪", "first round":  "第一輪", "1st round": "第一輪", "round 1": "第一輪",
    "round of 64":   "第二輪", "second round": "第二輪", "2nd round": "第二輪", "round 2": "第二輪",
    "round of 32":   "第三輪", "third round":  "第三輪", "3rd round": "第三輪", "round 3": "第三輪",
    "round of 16":   "第四輪", "fourth round": "第四輪", "4th round": "第四輪", "round 4": "第四輪",
    "quarterfinal":  "四強賽", "quarter-final": "四強賽", "quarter finals": "四強賽", "qf": "四強賽",
    "semifinal":     "準決賽", "semi-final":   "準決賽", "semi finals":    "準決賽", "sf": "準決賽",
    "final":         "決賽",
}

# 溫網 2026 各輪對應日期（估算，以台北時間午後場次 20:00 為預設）
# 假設首日 6/29（一）
ROUND_DATES = {
    "round of 128": [date(2026, 6, 29), date(2026, 6, 30)],
    "first round":  [date(2026, 6, 29), date(2026, 6, 30)],
    "round of 64":  [date(2026, 7, 1),  date(2026, 7, 2)],
    "second round": [date(2026, 7, 1),  date(2026, 7, 2)],
    "round of 32":  [date(2026, 7, 3),  date(2026, 7, 4)],
    "third round":  [date(2026, 7, 3),  date(2026, 7, 4)],
    "round of 16":  [date(2026, 7, 6),  date(2026, 7, 7)],
    "fourth round": [date(2026, 7, 6),  date(2026, 7, 7)],
    "quarterfinal": [date(2026, 7, 8),  date(2026, 7, 9)],
    "quarter-final":[date(2026, 7, 8),  date(2026, 7, 9)],
    "semifinal":    [date(2026, 7, 10), date(2026, 7, 11)],
    "semi-final":   [date(2026, 7, 10), date(2026, 7, 11)],
    "final":        [date(2026, 7, 12), date(2026, 7, 13)],
}

HEADERS_BROWSER = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
}
HEADERS_JSON = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
}


# ── 資料抓取：Wimbledon JSON（主要）─────────────────────────────────────────

def fetch_wimbledon_json():
    """嘗試 Wimbledon.com 的 JSON schedule endpoint"""
    matches = []
    tournament_start = date(2026, 6, 29)
    today = datetime.now(TAIPEI).date()
    day_num = (today - tournament_start).days + 1

    for d in range(max(1, day_num), min(14, day_num + 3)):
        url = f"https://www.wimbledon.com/en_GB/scores/json/schedule/day.json?d={d}"
        try:
            r = requests.get(url, headers=HEADERS_JSON, timeout=10)
            ct = r.headers.get("Content-Type", "")
            print(f"  Wimbledon JSON day{d}: HTTP {r.status_code} | Content-Type: {ct}")
            if r.status_code != 200:
                continue

            # 先看原始回應確認是否為 JSON
            raw = r.text[:300]
            print(f"  Preview: {repr(raw[:200])}")

            if not raw.strip().startswith("{") and not raw.strip().startswith("["):
                print(f"  → 非 JSON 格式，略過")
                continue

            data = r.json()
            print(f"  Keys: {list(data.keys())[:10]}")

            # 根據實際結構解析（待看到 Keys 後補充）
            raw_str = json.dumps(data).lower()
            found = [k for k in TRACKED_PLAYERS if k in raw_str]
            print(f"  Players in JSON: {found}")

            matches.extend(parse_wimbledon_schedule(data, d))

        except Exception as e:
            print(f"  Wimbledon JSON day{d} error: {e}")

    return matches


def parse_wimbledon_schedule(data, day_num):
    """解析 Wimbledon JSON 結構（根據實際 keys 調整）"""
    matches = []
    # 遞迴搜尋包含選手名稱的結構
    raw = json.dumps(data)
    for player in TRACKED_PLAYERS:
        if player in raw.lower():
            print(f"  ✓ Found {player} in Wimbledon JSON day{day_num}")
    return matches


# ── 資料抓取：ATP Tour 簽表（備用 1）────────────────────────────────────────

def fetch_atp_draws():
    """ATP Tour Wimbledon draw — 含完整 HTML 診斷"""
    matches = []

    urls = [
        ("ATP", "https://www.atptour.com/en/scores/current/wimbledon/540/draws"),
        ("WTA", "https://www.wtatennis.com/tournaments/1114/wimbledon/2026/draws"),
    ]

    for label, url in urls:
        try:
            r = requests.get(url, headers=HEADERS_BROWSER, timeout=15)
            print(f"\n  {label} draws: HTTP {r.status_code}")
            if r.status_code != 200:
                continue

            soup = BeautifulSoup(r.text, "html.parser")
            text = soup.get_text(separator=" ")
            found = [k for k in TRACKED_PLAYERS if k in text.lower()]
            print(f"  {label}: tracked players = {found}")

            # === 診斷：印出 HTML 結構摘要 ===
            print(f"\n  === {label} HTML 結構診斷 ===")
            # 尋找含有選手名稱的父節點
            for player in found[:2]:  # 只看前 2 個
                # 找最近的含有選手名的元素
                els = soup.find_all(string=re.compile(player, re.I))
                for el in els[:3]:
                    parent = el.parent
                    grandparent = parent.parent if parent else None
                    print(f"  Player '{player}' in <{parent.name}> class={parent.get('class','')}")
                    if grandparent:
                        print(f"    parent: <{grandparent.name}> class={grandparent.get('class','')}")
                    print(f"    text: {el.parent.get_text(' ', strip=True)[:120]}")

            # 嘗試解析
            found_matches = parse_atp_draw(soup, label)
            matches.extend(found_matches)

        except Exception as e:
            print(f"  {label} error: {e}")

    return matches


def parse_atp_draw(soup, label):
    """解析 ATP/WTA draw bracket，找出還未比賽的場次"""
    matches = []
    today = datetime.now(TAIPEI).date()
    cat = "男單" if label == "ATP" else "女單"

    # ATP Tour draw 的常見 CSS class 模式
    selectors = [
        ".draw-match", ".draw-item", "[class*='draw-match']",
        "[class*='match-node']", "[class*='bracket-match']",
        "li.draw", "div.match",
    ]

    blocks = []
    for sel in selectors:
        blocks = soup.select(sel)
        if blocks:
            print(f"  {label}: using selector '{sel}', found {len(blocks)} blocks")
            break

    if not blocks:
        print(f"  {label}: no draw block found with known selectors")
        # 嘗試任何含有 vs 或選手名的段落
        for el in soup.find_all(["div", "li", "tr"]):
            el_text = el.get_text(" ", strip=True).lower()
            if any(k in el_text for k in TRACKED_PLAYERS) and len(el_text) < 300:
                print(f"  Candidate block: {el.get_text(' ', strip=True)[:150]}")
        return matches

    for block in blocks:
        block_text = block.get_text(" ", strip=True)
        block_lower = block_text.lower()
        if not any(k in block_lower for k in TRACKED_PLAYERS):
            continue

        # 判斷是否為未完成的比賽（無分數格式）
        has_score = bool(re.search(r'\b[0-6]\s+[0-6]\b', block_text))

        # 抓選手名
        player_els = block.select(
            ".player-name, .name, [class*='player'], [class*='name'], span, strong"
        )
        players = [e.get_text(strip=True) for e in player_els if e.get_text(strip=True)]
        players = [p for p in players if len(p) > 2 and not p.isdigit()][:2]

        # 抓輪次
        round_text = ""
        round_el = block.find_previous(["h2", "h3", "h4", "div"], class_=re.compile("round|header", re.I))
        if round_el:
            round_text = round_el.get_text(strip=True)
        round_zh = translate_round(round_text)

        # 估算比賽日期
        match_date = estimate_match_date(round_text, today)
        start_taipei = datetime.combine(match_date, datetime.min.time()).replace(
            hour=20, minute=0, tzinfo=TAIPEI
        )

        print(f"  {label} match: {players} | round={round_text} | score={has_score} | date={match_date}")

        if len(players) == 2 and not has_score:
            matches.append({
                "players":      players,
                "start_taipei": start_taipei,
                "round":        round_zh,
                "court":        "溫布頓",
                "category":     cat,
            })
            print(f"  ✓ Upcoming: {players[0]} vs {players[1]}")

    return matches


def estimate_match_date(round_text, today):
    """根據輪次估算比賽日期（取該輪第一個未到的日期）"""
    round_lower = round_text.lower().strip()
    for key, dates in ROUND_DATES.items():
        if key in round_lower:
            for d in dates:
                if d >= today:
                    return d
            return dates[-1]
    return today


# ── 資料抓取：ESPN 日期版（備用 2）──────────────────────────────────────────

def fetch_espn_by_date():
    """ESPN scoreboard with date param — 只取有比賽資料的"""
    matches = []
    taipei_now = datetime.now(TAIPEI)

    for days in range(0, 3):
        date_str = (taipei_now + timedelta(days=days)).strftime("%Y%m%d")
        for tour, cat in [("atp", "男單"), ("wta", "女單")]:
            url = f"https://site.api.espn.com/apis/site/v2/sports/tennis/{tour}/scoreboard?dates={date_str}"
            try:
                r = requests.get(url, headers=HEADERS_JSON, timeout=10)
                if r.status_code != 200:
                    continue
                data = r.json()
                for event in data.get("events", []):
                    if not any(kw in event.get("name", "").lower() for kw in ["wimbledon", "championship"]):
                        continue
                    comps = event.get("competitions", [])
                    if not comps:
                        continue
                    print(f"  ESPN {tour} {date_str}: {len(comps)} matches in Wimbledon event")
                    for comp in comps:
                        m = parse_espn_comp(comp, event, cat)
                        if m:
                            matches.append(m)
            except Exception as e:
                print(f"  ESPN error: {e}")
    return matches


def parse_espn_comp(comp, event, cat):
    try:
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            return None
        players = [c.get("athlete", {}).get("displayName", "") for c in competitors]
        players_lower = [p.lower() for p in players]
        if not any(any(key in p for p in players_lower) for key in TRACKED_PLAYERS):
            return None
        start_str = comp.get("date", event.get("date", ""))
        if not start_str:
            return None
        start_taipei = datetime.fromisoformat(start_str.replace("Z", "+00:00")).astimezone(TAIPEI)
        round_name = comp.get("notes", [{}])[0].get("headline", "") if comp.get("notes") else ""
        return {
            "players":      players,
            "start_taipei": start_taipei,
            "round":        translate_round(round_name),
            "court":        comp.get("venue", {}).get("fullName", "") or "溫布頓",
            "category":     cat,
        }
    except Exception:
        return None


def translate_round(raw):
    raw_lower = raw.lower().strip()
    for key, zh in ROUND_MAP.items():
        if key in raw_lower:
            return zh
    return raw or "待定"


# ── 事件去重 ID ───────────────────────────────────────────────────────────────

def make_event_id(players, date_str, category):
    key = f"wimbledon2026-{'-'.join(sorted(p.lower().replace(' ','') for p in players))}-{date_str}-{category}"
    return "wb" + hashlib.md5(key.encode()).hexdigest()[:12]


# ── Google Calendar ───────────────────────────────────────────────────────────

def get_calendar_service():
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_json:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON 環境變數未設定")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(creds_json),
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    return build("calendar", "v3", credentials=creds)


def event_exists(service, event_id):
    try:
        service.events().get(calendarId=CALENDAR_ID, eventId=event_id).execute()
        return True
    except Exception:
        return False


def create_calendar_event(service, match):
    players = match["players"]
    start   = match["start_taipei"]
    end     = start + timedelta(hours=3)
    round_  = match["round"]
    court   = match["court"]
    cat     = match["category"]

    date_str = start.strftime("%Y%m%d")
    event_id = make_event_id(players, date_str, cat)

    if event_exists(service, event_id):
        print(f"  ⏭️  已存在：{players[0]} vs {players[1]}")
        return False

    title = f"🎾 [{round_}] {cat} · {players[0]} vs {players[1]}"
    body = {
        "id":       event_id,
        "summary":  title,
        "location": f"溫布頓 · {court}",
        "start": {"dateTime": start.isoformat(), "timeZone": "Asia/Taipei"},
        "end":   {"dateTime": end.isoformat(),   "timeZone": "Asia/Taipei"},
        "description": f"溫布頓 2026 · {cat}\n{round_} · {court}\n{players[0]} vs {players[1]}",
        "colorId": "11",
    }

    service.events().insert(calendarId=CALENDAR_ID, body=body).execute()
    print(f"  ✅ 新增：{title} ({start.strftime('%m/%d %H:%M')})")
    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n🎾 溫布頓賽程追蹤器啟動")
    print(f"⏰ 執行時間：{datetime.now(TAIPEI).strftime('%Y-%m-%d %H:%M')} 台北時間")

    # 優先：Wimbledon 官網 JSON
    print("\n📡 [1] Wimbledon JSON schedule...")
    matches = fetch_wimbledon_json()
    print(f"   → {len(matches)} 場")

    # 次選：ATP/WTA 官方簽表
    if not matches:
        print("\n📡 [2] ATP/WTA draws 頁面...")
        matches = fetch_atp_draws()
        print(f"   → {len(matches)} 場")

    # 備用：ESPN（只有 live 時有資料）
    if not matches:
        print("\n📡 [3] ESPN scoreboard...")
        matches = fetch_espn_by_date()
        print(f"   → {len(matches)} 場")

    if not matches:
        print("\n⚠️  本次無可用賽程資料（休賽日或所有來源受限）")
        return

    print(f"\n📅 更新 Google Calendar（{len(matches)} 場）...")
    try:
        service = get_calendar_service()
        added   = sum(1 for m in matches if create_calendar_event(service, m))
        print(f"\n✅ 完成：新增 {added} 場，跳過 {len(matches) - added} 場")
    except Exception as e:
        print(f"❌ Google Calendar 錯誤：{e}")
        raise


if __name__ == "__main__":
    main()
