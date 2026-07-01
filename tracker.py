#!/usr/bin/env python3
"""
Wimbledon 2026 重點選手賽程追蹤器
每 2 小時自動抓取賽程，更新 Google Calendar
"""
import os, json, hashlib
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
    "round of 128":   "第一輪", "first round":   "第一輪", "1st round": "第一輪", "round 1": "第一輪",
    "round of 64":    "第二輪", "second round":  "第二輪", "2nd round": "第二輪", "round 2": "第二輪",
    "round of 32":    "第三輪", "third round":   "第三輪", "3rd round": "第三輪", "round 3": "第三輪",
    "round of 16":    "第四輪", "fourth round":  "第四輪", "4th round": "第四輪", "round 4": "第四輪",
    "quarterfinal":   "四強賽", "quarter-final": "四強賽", "quarter finals": "四強賽", "qf": "四強賽",
    "semifinal":      "準決賽", "semi-final":    "準決賽", "semi finals":    "準決賽", "sf": "準決賽",
    "final":          "決賽",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
}


# ── 資料抓取：ESPN Schedule（主要）──────────────────────────────────────────

def fetch_espn_schedule():
    """ESPN schedule API — 顯示已排定的賽程（非即時比分）"""
    matches = []
    urls = [
        ("ATP", "https://site.api.espn.com/apis/site/v2/sports/tennis/atp/schedule"),
        ("WTA", "https://site.api.espn.com/apis/site/v2/sports/tennis/wta/schedule"),
        # scoreboard 作為補充（live matches）
        ("ATP-live", "https://site.api.espn.com/apis/site/v2/sports/tennis/atp/scoreboard"),
        ("WTA-live", "https://site.api.espn.com/apis/site/v2/sports/tennis/wta/scoreboard"),
    ]

    for label, url in urls:
        try:
            r = requests.get(url, headers=HEADERS, timeout=12)
            print(f"  ESPN {label}: HTTP {r.status_code}")
            if r.status_code != 200:
                continue
            data = r.json()
            events = data.get("events", [])
            print(f"  ESPN {label}: {len(events)} events")

            for event in events:
                name = event.get("name", "")
                print(f"    Event: {name}")
                name_lower = name.lower()
                if not any(kw in name_lower for kw in ["wimbledon", "championship", "grass"]):
                    continue

                for comp in event.get("competitions", []):
                    competitors = comp.get("competitors", [])
                    player_names = [c.get("athlete", {}).get("displayName", "?") for c in competitors]
                    if player_names:
                        print(f"      Match: {' vs '.join(player_names)}")

                    match = parse_espn_match(comp, event, label)
                    if match:
                        matches.append(match)
        except Exception as e:
            print(f"  ESPN {label} error: {e}")

    return matches


def parse_espn_match(comp, event, label=""):
    try:
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            return None

        players = [c.get("athlete", {}).get("displayName", "") for c in competitors]
        players_lower = [p.lower() for p in players]

        if not any(
            any(key in p for p in players_lower)
            for key in TRACKED_PLAYERS
        ):
            return None

        start_str = comp.get("date", event.get("date", ""))
        if not start_str:
            return None
        start_utc    = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        start_taipei = start_utc.astimezone(TAIPEI)

        round_name = comp.get("notes", [{}])[0].get("headline", "") if comp.get("notes") else ""
        round_zh   = translate_round(round_name)

        series = event.get("series", {}).get("slug", "")
        cat    = "女單" if ("wta" in series.lower() or "WTA" in label) else "男單"

        return {
            "players":      players,
            "start_taipei": start_taipei,
            "round":        round_zh,
            "court":        "溫布頓",
            "category":     cat,
        }
    except Exception as e:
        print(f"    parse_espn_match error: {e}")
        return None


# ── 資料抓取：Wimbledon 官網（備用 1）───────────────────────────────────────

def fetch_wimbledon_official():
    """Wimbledon 官方網站 Order of Play 頁面"""
    matches = []
    url = "https://www.wimbledon.com/en_GB/scores/schedule/index.html"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        print(f"\n  Wimbledon.com: HTTP {r.status_code}")
        if r.status_code != 200:
            return matches

        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(separator=" ")
        found = [k for k in TRACKED_PLAYERS if k in text.lower()]
        print(f"  Wimbledon.com: tracked players found = {found}")

        if not found:
            print("  Wimbledon.com: no tracked players in page, skipping parse")
            return matches

        # 嘗試解析比賽區塊
        taipei_now = datetime.now(TAIPEI)

        for block in soup.find_all(["article", "div", "li"], class_=lambda c: c and any(
            kw in c for kw in ["match", "fixture", "schedule", "event"]
        )):
            block_text = block.get_text(" ", strip=True).lower()
            if not any(key in block_text for key in TRACKED_PLAYERS):
                continue

            # 嘗試抓選手名稱
            player_els = block.select(".player-name, .competitor-name, .name, [class*='player'], [class*='name']")
            if len(player_els) >= 2:
                home = player_els[0].get_text(strip=True)
                away = player_els[1].get_text(strip=True)
                players = [home, away]

                # 嘗試抓時間
                time_el = block.select_one("time, [class*='time'], [class*='clock']")
                if time_el:
                    dt_str = time_el.get("datetime", "") or time_el.get_text(strip=True)
                    try:
                        start_bst    = datetime.fromisoformat(dt_str).replace(tzinfo=BST)
                        start_taipei = start_bst.astimezone(TAIPEI)
                    except Exception:
                        start_taipei = taipei_now.replace(hour=16, minute=0, second=0, microsecond=0)
                else:
                    start_taipei = taipei_now.replace(hour=16, minute=0, second=0, microsecond=0)

                cat = "女單" if any(p.lower() in ["sabalenka", "andreeva", "rybakina", "osaka"] for p in [home.lower(), away.lower()]) else "男單"
                matches.append({
                    "players":      players,
                    "start_taipei": start_taipei,
                    "round":        "待定",
                    "court":        "溫布頓",
                    "category":     cat,
                })
                print(f"  Wimbledon.com ✓: {home} vs {away}")

    except Exception as e:
        print(f"  Wimbledon.com error: {e}")

    return matches


# ── 資料抓取：BBC Sport（備用 2）─────────────────────────────────────────────

def fetch_bbc_schedule():
    """BBC Sport Wimbledon 賽程頁面"""
    matches = []
    urls_to_try = [
        "https://www.bbc.com/sport/tennis/wimbledon/scores-fixtures",
        "https://www.bbc.co.uk/sport/tennis/wimbledon/scores-fixtures",
    ]

    for url in urls_to_try:
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            print(f"\n  BBC Sport ({url}): HTTP {r.status_code}")
            if r.status_code != 200:
                continue

            soup = BeautifulSoup(r.text, "html.parser")
            text = soup.get_text(separator=" ")
            found = [k for k in TRACKED_PLAYERS if k in text.lower()]
            print(f"  BBC: tracked players found = {found}")

            if not found:
                print("  BBC: no tracked players found in page")
                continue

            taipei_now = datetime.now(TAIPEI)
            parsed_count = 0

            # BBC Sport 使用 data-testid 屬性
            selectors = [
                "[data-testid*='fixture']",
                "[data-testid*='match']",
                ".sp-c-fixture",
                "article[class*='fixture']",
                "li[class*='fixture']",
                "div[class*='fixture']",
            ]

            blocks = []
            for sel in selectors:
                blocks = soup.select(sel)
                if blocks:
                    print(f"  BBC: using selector '{sel}', found {len(blocks)} blocks")
                    break

            for block in blocks:
                block_text = block.get_text(" ", strip=True).lower()
                if not any(key in block_text for key in TRACKED_PLAYERS):
                    continue

                # 選手名稱
                name_sels = [
                    "[data-testid*='team-name']", "[data-testid*='player']",
                    ".sp-c-fixture__team-name-trunc", "span[class*='name']",
                    "strong", "b",
                ]
                player_els = []
                for ns in name_sels:
                    player_els = block.select(ns)
                    if len(player_els) >= 2:
                        break

                if len(player_els) < 2:
                    continue

                home = player_els[0].get_text(strip=True)
                away = player_els[1].get_text(strip=True)
                if not home or not away or home == away:
                    continue

                # 時間
                time_el = block.select_one("time, [datetime], [class*='time']")
                if time_el:
                    dt_val = time_el.get("datetime", "") or time_el.get_text(strip=True)
                    try:
                        if "T" in dt_val:
                            start_utc    = datetime.fromisoformat(dt_val.replace("Z", "+00:00"))
                            start_taipei = start_utc.astimezone(TAIPEI)
                        else:
                            # 只有時間字串（如 "14:00"）
                            h, m = map(int, dt_val.split(":"))
                            start_bst    = datetime.now(BST).replace(hour=h, minute=m, second=0, microsecond=0)
                            start_taipei = start_bst.astimezone(TAIPEI)
                    except Exception:
                        start_taipei = taipei_now.replace(hour=16, minute=0, second=0, microsecond=0)
                else:
                    start_taipei = taipei_now.replace(hour=16, minute=0, second=0, microsecond=0)

                cat = "女單" if any(k in (home + away).lower() for k in ["sabalenka", "andreeva", "rybakina", "osaka"]) else "男單"
                matches.append({
                    "players":      [home, away],
                    "start_taipei": start_taipei,
                    "round":        "待定",
                    "court":        "溫布頓",
                    "category":     cat,
                })
                print(f"  BBC ✓: {home} vs {away} ({start_taipei.strftime('%m/%d %H:%M')})")
                parsed_count += 1

            if parsed_count > 0:
                break  # 找到資料就不再試下一個 URL

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

    # 嘗試各資料來源
    print("\n📡 嘗試 ESPN Schedule API...")
    matches = fetch_espn_schedule()
    print(f"   ESPN 找到 {len(matches)} 場追蹤選手比賽")

    if not matches:
        print("\n📡 嘗試 Wimbledon 官網...")
        matches = fetch_wimbledon_official()
        print(f"   Wimbledon.com 找到 {len(matches)} 場")

    if not matches:
        print("\n📡 嘗試 BBC Sport...")
        matches = fetch_bbc_schedule()
        print(f"   BBC 找到 {len(matches)} 場")

    if not matches:
        print("\n⚠️  所有來源均無資料（可能為休賽日、全天賽程已結束，或 API 無法存取）")
        return

    print(f"\n📅 更新 Google Calendar（共 {len(matches)} 場）...")
    try:
        service = get_calendar_service()
        added   = sum(1 for m in matches if create_calendar_event(service, m))
        skipped = len(matches) - added
        print(f"\n✅ 完成：新增 {added} 場，跳過 {skipped} 場（已存在）")
    except Exception as e:
        print(f"❌ Google Calendar 錯誤：{e}")
        raise


if __name__ == "__main__":
    main()
