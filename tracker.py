#!/usr/bin/env python3
"""
Wimbledon 2026 重點選手賽程追蹤器
每 2 小時自動抓取賽程，更新 Google Calendar
"""
import os, json, hashlib, re
from datetime import datetime, timezone, timedelta
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

HEADERS_BROWSER = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
}
HEADERS_JSON = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
}


# ── 資料抓取：ESPN with date（主要）─────────────────────────────────────────

def fetch_espn_by_date():
    """ESPN scoreboard 加上日期參數，取今明兩天的賽程"""
    matches = []
    taipei_now = datetime.now(TAIPEI)

    for days in range(0, 3):
        date_str = (taipei_now + timedelta(days=days)).strftime("%Y%m%d")
        for tour, label in [("atp", "男"), ("wta", "女")]:
            url = f"https://site.api.espn.com/apis/site/v2/sports/tennis/{tour}/scoreboard?dates={date_str}"
            try:
                r = requests.get(url, headers=HEADERS_JSON, timeout=10)
                print(f"  ESPN {label}單 {date_str}: HTTP {r.status_code}")
                if r.status_code != 200:
                    continue
                data   = r.json()
                events = data.get("events", [])

                for event in events:
                    name = event.get("name", "")
                    print(f"    Event: {name} ({len(event.get('competitions', []))} comps)")
                    if not any(kw in name.lower() for kw in ["wimbledon", "championship"]):
                        continue
                    cat = "女單" if tour == "wta" else "男單"
                    for comp in event.get("competitions", []):
                        m = parse_espn_comp(comp, event, cat)
                        if m:
                            matches.append(m)
            except Exception as e:
                print(f"  ESPN error ({date_str} {label}): {e}")
    return matches


def parse_espn_comp(comp, event, cat):
    try:
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            return None

        players = [c.get("athlete", {}).get("displayName", "") for c in competitors]
        players_lower = [p.lower() for p in players]
        print(f"      Players: {players}")

        if not any(any(key in p for p in players_lower) for key in TRACKED_PLAYERS):
            return None

        start_str = comp.get("date", event.get("date", ""))
        if not start_str:
            return None
        start_utc    = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        start_taipei = start_utc.astimezone(TAIPEI)

        round_name = ""
        if comp.get("notes"):
            round_name = comp["notes"][0].get("headline", "")
        round_zh = translate_round(round_name)

        return {
            "players":      players,
            "start_taipei": start_taipei,
            "round":        round_zh,
            "court":        comp.get("venue", {}).get("fullName", "") or "溫布頓",
            "category":     cat,
        }
    except Exception as e:
        print(f"      parse_espn_comp error: {e}")
        return None


# ── 資料抓取：Wimbledon.com 嵌入 JSON（備用 1）──────────────────────────────

def fetch_wimbledon_json():
    """嘗試從 Wimbledon.com 頁面的 __NEXT_DATA__ 或 JSON API 取得資料"""
    matches = []

    # 1. 嘗試直接 JSON schedule endpoint
    taipei_now = datetime.now(TAIPEI)
    tournament_start = datetime(2026, 6, 29, tzinfo=TAIPEI)
    day_num = (taipei_now.date() - tournament_start.date()).days + 1

    json_urls = []
    for d in range(max(1, day_num), min(14, day_num + 3)):
        json_urls.append(f"https://www.wimbledon.com/en_GB/scores/json/schedule/day.json?d={d}")
        json_urls.append(f"https://www.wimbledon.com/en_GB/scores/json/schedule/day{d}.json")

    for url in json_urls:
        try:
            r = requests.get(url, headers=HEADERS_JSON, timeout=10)
            print(f"  Wimbledon JSON {url}: HTTP {r.status_code}")
            if r.status_code == 200:
                data = r.json()
                print(f"    Keys: {list(data.keys())[:8]}")
                # 嘗試解析
                found = parse_wimbledon_json(data, taipei_now)
                matches.extend(found)
                if found:
                    return matches
        except Exception as e:
            if "Expecting value" not in str(e):  # 只印非 JSON 解析錯誤
                print(f"  Wimbledon JSON error: {e}")

    # 2. 嘗試從 HTML 頁面抽取 __NEXT_DATA__
    try:
        r = requests.get(
            "https://www.wimbledon.com/en_GB/scores/schedule/index.html",
            headers=HEADERS_BROWSER, timeout=15
        )
        print(f"\n  Wimbledon HTML: HTTP {r.status_code}")
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            next_data = soup.find("script", id="__NEXT_DATA__")
            if next_data:
                print("  Wimbledon: found __NEXT_DATA__")
                try:
                    page_data = json.loads(next_data.string)
                    found = parse_wimbledon_json(page_data, taipei_now)
                    matches.extend(found)
                except Exception as e:
                    print(f"  __NEXT_DATA__ parse error: {e}")
            else:
                # 找其他 JSON 嵌入
                scripts = soup.find_all("script", type="application/json")
                print(f"  Wimbledon: {len(scripts)} JSON scripts in page")
                for sc in scripts[:3]:
                    print(f"    Script preview: {(sc.string or '')[:200]}")
    except Exception as e:
        print(f"  Wimbledon HTML error: {e}")

    return matches


def parse_wimbledon_json(data, taipei_now):
    """遞迴搜尋 Wimbledon JSON 中的選手名稱"""
    matches = []
    raw = json.dumps(data).lower()
    found = [k for k in TRACKED_PLAYERS if k in raw]
    if found:
        print(f"  Wimbledon JSON: tracked players found = {found}")
    return matches


# ── 資料抓取：ATP Tour 官網（備用 2）────────────────────────────────────────

def fetch_atp_draws():
    """ATP Tour 溫布頓簽表頁面（靜態 HTML）"""
    matches = []
    url = "https://www.atptour.com/en/scores/current/wimbledon/540/draws"
    try:
        r = requests.get(url, headers=HEADERS_BROWSER, timeout=15)
        print(f"\n  ATP Tour: HTTP {r.status_code}")
        if r.status_code != 200:
            return matches

        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(separator=" ")
        found = [k for k in TRACKED_PLAYERS if k in text.lower()]
        print(f"  ATP Tour: tracked players = {found}")

        if not found:
            print(f"  ATP Tour page preview: {text[:500]}")
            return matches

        taipei_now = datetime.now(TAIPEI)
        # ATP draw shows matches; without exact time, use BST 12:00 as placeholder
        for player_key in TRACKED_PLAYERS:
            idx = text.lower().find(player_key)
            if idx < 0:
                continue
            # 找 vs 附近的兩個選手
            snippet = text[max(0, idx-100):idx+200]
            vs_idx = snippet.lower().find(" vs ")
            if vs_idx < 0:
                vs_idx = snippet.lower().find(" d. ")  # 已完成的比賽格式
            print(f"  ATP snippet for {player_key}: ...{snippet.strip()[:150]}...")

    except Exception as e:
        print(f"  ATP Tour error: {e}")

    return matches


# ── 資料抓取：BBC Sport（備用 3）─────────────────────────────────────────────

def fetch_bbc_schedule():
    """BBC Sport 溫布頓頁面"""
    matches = []
    urls = [
        "https://www.bbc.com/sport/tennis/wimbledon",
        "https://www.bbc.co.uk/sport/tennis/wimbledon",
        "https://www.bbc.com/sport/tennis",
    ]
    for url in urls:
        try:
            r = requests.get(url, headers=HEADERS_BROWSER, timeout=15)
            print(f"  BBC ({url}): HTTP {r.status_code}")
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                text = soup.get_text(separator=" ")
                found = [k for k in TRACKED_PLAYERS if k in text.lower()]
                print(f"  BBC: players found = {found}")
                break
        except Exception as e:
            print(f"  BBC error: {e}")
    return matches


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

    print("\n📡 [1] ESPN Scoreboard（含日期參數）...")
    matches = fetch_espn_by_date()
    print(f"   → {len(matches)} 場")

    if not matches:
        print("\n📡 [2] Wimbledon 官網 JSON...")
        matches = fetch_wimbledon_json()
        print(f"   → {len(matches)} 場")

    if not matches:
        print("\n📡 [3] ATP Tour 簽表頁...")
        matches = fetch_atp_draws()
        print(f"   → {len(matches)} 場")

    if not matches:
        print("\n📡 [4] BBC Sport（診斷用）...")
        fetch_bbc_schedule()
        print("\n⚠️  所有來源均無可用賽程資料，本次跳過 Calendar 更新")
        return

    print(f"\n📅 更新 Google Calendar（共 {len(matches)} 場）...")
    try:
        service = get_calendar_service()
        added   = sum(1 for m in matches if create_calendar_event(service, m))
        print(f"\n✅ 完成：新增 {added} 場，跳過 {len(matches) - added} 場（已存在）")
    except Exception as e:
        print(f"❌ Google Calendar 錯誤：{e}")
        raise


if __name__ == "__main__":
    main()
