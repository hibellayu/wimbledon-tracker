#!/usr/bin/env python3
"""
Wimbledon 2026 重點選手賽程追蹤器
主要來源：tennis-db.com（靜態 HTML，ATP + WTA）
補充來源：ESPN live（即時比賽時間）
每 2 小時自動執行，更新 Google Calendar
"""
import os, json, hashlib, re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ── 設定 ─────────────────────────────────────────────────────────────────────

TAIPEI = ZoneInfo("Asia/Taipei")
BST    = ZoneInfo("Europe/London")   # UTC+1（夏令，Wimbledon 期間）

TRACKED_PLAYERS = [
    "sinner", "djokovic", "zverev",         # 男單（Alcaraz 腕傷退賽）
    "sabalenka", "andreeva", "rybakina", "osaka",  # 女單
]

PLAYER_DISPLAY = {
    "sinner":    "Jannik Sinner",
    "djokovic":  "Novak Djokovic",
    "zverev":    "Alexander Zverev",
    "sabalenka": "Aryna Sabalenka",
    "andreeva":  "Mirra Andreeva",
    "rybakina":  "Elena Rybakina",
    "osaka":     "Naomi Osaka",
}

CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")

ROUND_MAP = {
    "round of 128": "第一輪", "first round":  "第一輪", "1st round": "第一輪",
    "round of 64":  "第二輪", "second round": "第二輪", "2nd round": "第二輪",
    "round of 32":  "第三輪", "third round":  "第三輪", "3rd round": "第三輪",
    "round of 16":  "第四輪", "fourth round": "第四輪", "4th round": "第四輪",
    "quarterfinal": "四強賽", "quarter-final": "四強賽", "qf": "四強賽",
    "semifinal":    "準決賽", "semi-final":   "準決賽", "sf": "準決賽",
    "final":        "決賽",
}

# tennis-db.com 的輪次代碼
TENNISDB_ROUND_MAP = [
    ("R128", "第一輪"),
    ("R64",  "第二輪"),
    ("R32",  "第三輪"),
    ("R16",  "第四輪"),
    ("QF",   "四強賽"),
    ("SF",   "準決賽"),
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
}
HEADERS_JSON = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
}


# ── 輔助函數 ──────────────────────────────────────────────────────────────────

def expand_name(raw):
    """縮寫/含排名號碼的選手名 → 清理後名稱（追蹤選手展開全名）"""
    clean = re.sub(r'#\d+', '', raw).strip()
    clean_lower = clean.lower()
    for key, full in PLAYER_DISPLAY.items():
        if key in clean_lower:
            return full
    return clean


def translate_round(raw):
    raw_lower = raw.lower().strip()
    for key, zh in ROUND_MAP.items():
        if key in raw_lower:
            return zh
    return raw or "待定"


def _dedup_matches(matches):
    """同一對選手只保留一筆"""
    seen = set()
    out  = []
    for m in matches:
        key = frozenset(p.lower() for p in m["players"])
        if key not in seen:
            seen.add(key)
            out.append(m)
    return out


# ── 資料抓取：tennis-db.com（主要來源，ATP + WTA）────────────────────────────

def fetch_tennisdb():
    """
    tennis-db.com — 靜態 HTML，同時抓 ATP 男單與 WTA 女單
    - 有精確的 BST 開賽時間（"Starts at 1:30 PM"）
    - 自動略過已完賽（有 Match data 連結）和歷史賽季（?season=）
    """
    matches = []
    sources = [
        (
            "https://tennis-db.com/tournaments/256/wimbledon",
            "男單",
            "/players/",
        ),
        (
            "https://tennis-db.com/wta/tournaments/balldontlie_wta:50/wimbledon",
            "女單",
            "/wta/players/",
        ),
    ]
    today = datetime.now(TAIPEI).date()

    for url, category, player_prefix in sources:
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            print(f"  tennisdb {category}: HTTP {r.status_code}, {len(r.text):,} bytes")
            if r.status_code != 200:
                continue

            soup = BeautifulSoup(r.text, "html.parser")
            seen_pairs = set()

            for tr in soup.find_all("tr"):
                # 跳過歷史賽季的列（含 ?season= 連結）
                if any("?season=" in (a.get("href", "")) for a in tr.find_all("a")):
                    continue

                # 取得選手連結（排除 H2H 和 match data 連結）
                player_links = [
                    a for a in tr.find_all("a", href=True)
                    if player_prefix in a.get("href", "")
                    and "/rivalries/" not in a.get("href", "")
                    and "/matches/" not in a.get("href", "")
                ]
                if len(player_links) < 2:
                    continue

                p1_raw = player_links[0].get_text(strip=True)
                p2_raw = player_links[1].get_text(strip=True)

                # 確認是追蹤選手之一
                if not (any(k in p1_raw.lower() for k in TRACKED_PLAYERS) or
                        any(k in p2_raw.lower() for k in TRACKED_PLAYERS)):
                    continue

                row_text = tr.get_text(" ", strip=True)

                # 跳過已完賽（有 "Match data" 連結）
                if tr.find("a", string=lambda s: s and "Match data" in s):
                    continue

                # 只保留未開賽（score = 0–0）
                if "0–0" not in row_text:   # "0–0" U+2013
                    continue

                # 解析日期（只要 2026 年的）
                date_m = re.search(
                    r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d+),\s+2026',
                    row_text
                )
                if not date_m:
                    continue
                match_date = datetime.strptime(date_m.group(0), "%b %d, %Y").date()
                if match_date < today:
                    continue

                # 解析輪次
                round_zh = "待定"
                for code, name in TENNISDB_ROUND_MAP:
                    if code in row_text:
                        round_zh = name
                        break
                if round_zh == "待定" and re.search(r'\bF\b', row_text):
                    round_zh = "決賽"

                # 解析開賽時間（BST → 台北）
                time_m = re.search(
                    r'[Ss]tarts [Aa]t\s+(\d{1,2}):(\d{2})\s*(AM|PM|am|pm)',
                    row_text
                )
                if time_m:
                    hour   = int(time_m.group(1))
                    minute = int(time_m.group(2))
                    ampm   = time_m.group(3).upper()
                    if ampm == "PM" and hour != 12:
                        hour += 12
                    elif ampm == "AM" and hour == 12:
                        hour = 0
                    # BST (Europe/London, UTC+1) → 台北 (UTC+8)
                    bst_dt = datetime(
                        match_date.year, match_date.month, match_date.day,
                        hour, minute, tzinfo=BST
                    )
                    start_taipei = bst_dt.astimezone(TAIPEI)
                else:
                    # 無精確時間 → 預設 20:00 台北
                    start_taipei = datetime(
                        match_date.year, match_date.month, match_date.day,
                        20, 0, tzinfo=TAIPEI
                    )

                # 去重
                pair_key = tuple(sorted([p1_raw.lower(), p2_raw.lower()]))
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)

                p1 = expand_name(p1_raw)
                p2 = expand_name(p2_raw)

                matches.append({
                    "players":      [p1, p2],
                    "start_taipei": start_taipei,
                    "round":        round_zh,
                    "court":        "溫布頓",
                    "category":     category,
                })
                print(f"  tennisdb ✓ {category}: {p1} vs {p2} | {round_zh} | {start_taipei.strftime('%m/%d %H:%M')} 台北")

        except Exception as e:
            print(f"  tennisdb error ({category}): {e}")
            import traceback; traceback.print_exc()

    return _dedup_matches(matches)


# ── 資料抓取：ESPN Live（即時補充精確時間）───────────────────────────────────

def fetch_espn_live():
    """ESPN scoreboard — 補充即時比賽的精確時間（只在比賽進行中有效）"""
    matches = []
    taipei_now = datetime.now(TAIPEI)
    for days in range(0, 2):
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
                    for comp in event.get("competitions", []):
                        players = [
                            c.get("athlete", {}).get("displayName", "")
                            for c in comp.get("competitors", [])
                        ]
                        if len(players) < 2:
                            continue
                        if not any(any(k in p.lower() for k in TRACKED_PLAYERS) for p in players):
                            continue
                        start_str = comp.get("date", event.get("date", ""))
                        if not start_str:
                            continue
                        start_taipei = datetime.fromisoformat(
                            start_str.replace("Z", "+00:00")
                        ).astimezone(TAIPEI)
                        round_name = (
                            comp.get("notes", [{}])[0].get("headline", "")
                            if comp.get("notes") else ""
                        )
                        p1, p2 = expand_name(players[0]), expand_name(players[1])
                        matches.append({
                            "players":      [p1, p2],
                            "start_taipei": start_taipei,
                            "round":        translate_round(round_name),
                            "court":        comp.get("venue", {}).get("fullName", "") or "溫布頓",
                            "category":     cat,
                        })
                        print(f"  ESPN live ✓: {p1} vs {p2} | {start_taipei.strftime('%m/%d %H:%M')}")
            except Exception as e:
                print(f"  ESPN error: {e}")
    return _dedup_matches(matches)


# ── Google Calendar ───────────────────────────────────────────────────────────

def make_event_id(players, date_str, category):
    """Google Calendar ID：只能用 a-v 和 0-9（base32hex），前綴 tm"""
    key = f"wimbledon2026-{'-'.join(sorted(p.lower().replace(' ','') for p in players))}-{date_str}-{category}"
    return "tm" + hashlib.md5(key.encode()).hexdigest()  # 34 chars，全部合法


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

    cat_code = "MS" if cat == "男單" else "WS"
    title    = f"🎾 [{round_}] {cat_code} · {players[0]} vs {players[1]}"
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

    # [1] tennis-db.com — 靜態 HTML，ATP + WTA 一起抓，有精確 BST 時間
    print("\n📡 [1] tennis-db.com（ATP 男單 + WTA 女單）...")
    matches = fetch_tennisdb()
    existing_pairs = {frozenset(p.lower() for p in m["players"]) for m in matches}
    print(f"   → 找到 {len(matches)} 場")

    # [2] ESPN Live — 比賽進行中時取得更精確的時間
    print("\n📡 [2] ESPN live（即時時間補充）...")
    live_matches = fetch_espn_live()
    for m in live_matches:
        pair = frozenset(p.lower() for p in m["players"])
        if pair not in existing_pairs:
            matches.append(m)
            existing_pairs.add(pair)
        else:
            # 以 live 資料更新時間
            for existing in matches:
                if frozenset(p.lower() for p in existing["players"]) == pair:
                    existing["start_taipei"] = m["start_taipei"]
                    print(f"  ⏰ 更新時間：{m['players'][0]} vs {m['players'][1]} → {m['start_taipei'].strftime('%H:%M')}")
                    break
    print(f"   → 合計 {len(matches)} 場（ESPN 補充後）")

    if not matches:
        print("\n⚠️  本次無可用賽程資料")
        return

    print(f"\n📅 更新 Google Calendar（{len(matches)} 場）...")
    try:
        service = get_calendar_service()
        added   = sum(1 for m in matches if create_calendar_event(service, m))
        print(f"\n✅ 完成：新增 {added} 場，跳過 {len(matches) - added} 場（已存在）")
    except Exception as e:
        print(f"❌ Google Calendar 錯誤：{e}")
        raise


if __name__ == "__main__":
    main()
