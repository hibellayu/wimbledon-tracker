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

# draw-round-X → 輪次推算（ATP Tour draw 頁的 CSS 命名）
DRAW_ROUND_TO_TOURNAMENT_ROUND = {
    "draw-round-1": "round of 128",
    "draw-round-2": "round of 64",
    "draw-round-3": "round of 32",
    "draw-round-4": "round of 16",
    "draw-round-5": "quarterfinal",
    "draw-round-6": "semifinal",
    "draw-round-7": "final",
    "draw round 1": "round of 128",
    "draw round 2": "round of 64",
    "draw round 3": "round of 32",
    "draw round 4": "round of 16",
    "draw round 5": "quarterfinal",
    "draw round 6": "semifinal",
    "draw round 7": "final",
}

# 溫網 2026 各輪預計日期
ROUND_DATES = {
    "round of 128": [date(2026, 6, 29), date(2026, 6, 30)],
    "round of 64":  [date(2026, 7, 1),  date(2026, 7, 2)],
    "round of 32":  [date(2026, 7, 3),  date(2026, 7, 4)],
    "round of 16":  [date(2026, 7, 6),  date(2026, 7, 7)],
    "quarterfinal": [date(2026, 7, 8),  date(2026, 7, 9)],
    "semifinal":    [date(2026, 7, 10), date(2026, 7, 11)],
    "final":        [date(2026, 7, 12), date(2026, 7, 13)],
}

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
    """縮寫/含種子號碼的選手名 → 清理後名稱（追蹤選手展開全名）"""
    clean = re.sub(r'\(\d+\)', '', raw).strip()
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


def estimate_match_date(round_key, today):
    """根據輪次找最近未到的比賽日"""
    dates = ROUND_DATES.get(round_key, [today])
    for d in dates:
        if d >= today:
            return d
    return dates[-1] if dates else today


def get_round_key_from_classes(classes):
    """從 CSS class 清單推算輪次 key"""
    class_str = " ".join(classes).lower()
    for css_key, round_key in DRAW_ROUND_TO_TOURNAMENT_ROUND.items():
        if css_key.replace("-", " ") in class_str or css_key in class_str:
            return round_key
    return ""


def find_match_container(name_link):
    """
    從選手名稱的 <a> 往上找「包含兩個 div.name a 的最近父元素」
    這個父元素就是比賽的 match block
    """
    el = name_link
    for _ in range(10):
        el = el.parent
        if el is None or el.name in ["html", "body"]:
            return None, []
        players_in_el = el.select("div.name a")
        if len(players_in_el) == 2:
            return el, players_in_el
        if len(players_in_el) > 2:
            # 太大了，縮小範圍
            continue
    return None, []


def has_match_score(container):
    """判斷比賽是否已有分數（完成）"""
    # 1. 找含 score / sets / result 的元素
    score_els = container.select(
        "[class*='score'], [class*='sets'], [class*='result'], "
        "[class*='won'], [class*='lost']"
    )
    for el in score_els:
        text = el.get_text(strip=True)
        # 分數格式如 "6" "3" 或 "6-4"
        if re.search(r'\b[0-7]\b', text) and len(text) <= 20:
            return True

    # 2. 看 container 的 class 有無 "completed" "finished" 等
    classes = " ".join(container.get("class", [])).lower()
    if any(kw in classes for kw in ["complete", "finish", "result", "past"]):
        return True

    # 3. 掃描文字找純數字分數（嚴格模式：多組 "數字" 之間只有空格）
    # 去掉選手名、種子後，找 "6 3" "7 6" 等連續數字組
    raw = container.get_text(" ", strip=True)
    # 去掉選手名
    for a in container.select("div.name a"):
        raw = raw.replace(a.get_text(strip=True), "")
    # 去掉種子號碼如 (1) (7)
    raw = re.sub(r'\(\d+\)', '', raw)
    # 找多個空格分隔的單數字（比分格式：6 3 / 7 6 / 6 4）
    nums = re.findall(r'\b[0-7]\b', raw)
    if len(nums) >= 4:  # 至少兩盤分數
        return True

    return False


# ── 資料抓取：ATP Tour Order of Play（主要，今日精確時間）───────────────────

def fetch_atp_order_of_play():
    """ATP Tour 每日賽程頁（含精確時間）"""
    matches = []
    url = "https://www.atptour.com/en/scores/current/wimbledon/540/order-of-play"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        print(f"  ATP Order of Play: HTTP {r.status_code}")
        if r.status_code != 200:
            return matches

        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(separator=" ")
        found = [k for k in TRACKED_PLAYERS if k in text.lower()]
        print(f"  OoP tracked players: {found}")

        if not found:
            print(f"  OoP: no tracked players in page")
            return matches

        # 找所有比賽 block
        today = datetime.now(TAIPEI).date()
        for name_link in soup.select("div.name a"):
            player_raw = name_link.get_text(strip=True)
            if not any(k in player_raw.lower() for k in TRACKED_PLAYERS):
                continue

            container, players_els = find_match_container(name_link)
            if not container or len(players_els) != 2:
                continue

            p1 = expand_name(players_els[0].get_text(strip=True))
            p2 = expand_name(players_els[1].get_text(strip=True))

            # 找時間
            time_el = container.select_one("time, [class*='time'], [class*='clock']")
            bst_time_str = time_el.get_text(strip=True) if time_el else ""

            # 找球場
            court_el = container.select_one("[class*='court'], [class*='venue']")
            court = court_el.get_text(strip=True) if court_el else "溫布頓"

            # 解析 BST 時間
            start_taipei = _parse_bst_time(bst_time_str, today)

            # 找輪次
            round_el = container.find_previous(class_=re.compile("round|header", re.I))
            round_text = round_el.get_text(strip=True) if round_el else ""
            round_zh   = translate_round(round_text)

            cat = _detect_category(p1, p2)

            print(f"  OoP ✓: {p1} vs {p2} | {bst_time_str} BST | {court}")
            matches.append({
                "players":      [p1, p2],
                "start_taipei": start_taipei,
                "round":        round_zh,
                "court":        court,
                "category":     cat,
            })

    except Exception as e:
        print(f"  ATP OoP error: {e}")

    return _dedup_matches(matches)


def _parse_bst_time(time_str, match_date):
    """BST 時間字串 → 台北時間"""
    taipei_now = datetime.now(TAIPEI)
    m = re.search(r'(\d{1,2}):(\d{2})', time_str)
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        start_bst    = datetime.combine(match_date, datetime.min.time()).replace(
            hour=h, minute=mn, tzinfo=BST
        )
        return start_bst.astimezone(TAIPEI)
    # 預設：台北 20:00
    return datetime.combine(match_date, datetime.min.time()).replace(hour=20, minute=0, tzinfo=TAIPEI)


def _detect_category(p1, p2):
    """判斷男單 / 女單"""
    wta = {"sabalenka", "andreeva", "rybakina", "osaka"}
    names = (p1 + " " + p2).lower()
    return "女單" if any(w in names for w in wta) else "男單"


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


# ── 資料抓取：ATP Tour Draw（備用，未來賽程）────────────────────────────────

def fetch_atp_draw():
    """ATP Tour draw — 抓尚未有分數的配對（含未來各輪）"""
    matches = []
    url = "https://www.atptour.com/en/scores/current/wimbledon/540/draws"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        print(f"\n  ATP Draw: HTTP {r.status_code}")
        if r.status_code != 200:
            return matches

        soup = BeautifulSoup(r.text, "html.parser")
        today = datetime.now(TAIPEI).date()

        for name_link in soup.select("div.name a"):
            player_raw = name_link.get_text(strip=True)
            if not any(k in player_raw.lower() for k in TRACKED_PLAYERS):
                continue

            container, players_els = find_match_container(name_link)
            if not container or len(players_els) != 2:
                print(f"  Draw: couldn't find match container for {player_raw}")
                continue

            p1 = expand_name(players_els[0].get_text(strip=True))
            p2 = expand_name(players_els[1].get_text(strip=True))

            scored = has_match_score(container)

            # 找 draw-round CSS class
            round_container = container
            round_key = ""
            for _ in range(8):
                round_container = round_container.parent
                if round_container is None:
                    break
                css = round_container.get("class", [])
                round_key = get_round_key_from_classes(css)
                if round_key:
                    break

            round_zh   = translate_round(round_key)
            cat        = _detect_category(p1, p2)
            match_date = estimate_match_date(round_key, today)
            start      = datetime.combine(match_date, datetime.min.time()).replace(
                hour=20, minute=0, tzinfo=TAIPEI
            )

            print(f"  Draw: {p1} vs {p2} | round_key={round_key!r} | scored={scored}")

            if not scored and p1 != p2:
                matches.append({
                    "players":      [p1, p2],
                    "start_taipei": start,
                    "round":        round_zh,
                    "court":        "溫布頓",
                    "category":     cat,
                })

    except Exception as e:
        print(f"  ATP Draw error: {e}")

    return _dedup_matches(matches)


# ── 資料抓取：ESPN Live（即時補充）──────────────────────────────────────────

def fetch_espn_live():
    """ESPN scoreboard — 取得進行中比賽的精確時間"""
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
                        players = [c.get("athlete", {}).get("displayName", "") for c in comp.get("competitors", [])]
                        if len(players) < 2:
                            continue
                        if not any(any(k in p.lower() for k in TRACKED_PLAYERS) for p in players):
                            continue
                        start_str = comp.get("date", event.get("date", ""))
                        if not start_str:
                            continue
                        start_taipei = datetime.fromisoformat(start_str.replace("Z", "+00:00")).astimezone(TAIPEI)
                        round_name   = comp.get("notes", [{}])[0].get("headline", "") if comp.get("notes") else ""
                        p1, p2 = expand_name(players[0]), expand_name(players[1])
                        matches.append({
                            "players":      [p1, p2],
                            "start_taipei": start_taipei,
                            "round":        translate_round(round_name),
                            "court":        comp.get("venue", {}).get("fullName", "") or "溫布頓",
                            "category":     cat,
                        })
                        print(f"  ESPN live: {p1} vs {p2}")
            except Exception as e:
                print(f"  ESPN error: {e}")
    return _dedup_matches(matches)


# ── 事件去重 ID ───────────────────────────────────────────────────────────────

def make_event_id(players, date_str, category):
    """Google Calendar ID：只能用 a-v 和 0-9（base32hex），前綴 tm"""
    key = f"wimbledon2026-{'-'.join(sorted(p.lower().replace(' ','') for p in players))}-{date_str}-{category}"
    return "tm" + hashlib.md5(key.encode()).hexdigest()  # 34 chars, all valid


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

    # [1] 今日賽程（含精確時間）
    print("\n📡 [1] ATP Order of Play（今日精確時間）...")
    matches = fetch_atp_order_of_play()
    print(f"   → {len(matches)} 場")

    # [2] Draw bracket（補充未來場次）
    print("\n📡 [2] ATP Draw（補充未來輪次）...")
    draw_matches = fetch_atp_draw()
    # 合併，去重（同一對選手只加一次）
    existing_pairs = {frozenset(p.lower() for p in m["players"]) for m in matches}
    for m in draw_matches:
        pair = frozenset(p.lower() for p in m["players"])
        if pair not in existing_pairs:
            matches.append(m)
            existing_pairs.add(pair)
    print(f"   → 合計 {len(matches)} 場（Draw 補充後）")

    # [3] ESPN Live（即時補充女單 + 精確時間）
    print("\n📡 [3] ESPN live（即時補充）...")
    live_matches = fetch_espn_live()
    for m in live_matches:
        pair = frozenset(p.lower() for p in m["players"])
        if pair not in existing_pairs:
            matches.append(m)
            existing_pairs.add(pair)
        else:
            # 更新已存在項目的時間（live 時間更準）
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
