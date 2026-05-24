import hashlib
import logging
import os
import re
import sqlite3
import time
import urllib.parse
import traceback
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

# =====================
# CONFIGURATION
# =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

HEADERS = {"User-Agent": "Mozilla/5.0"}
IST = timezone(timedelta(hours=5, minutes=30))
# Expanded result phrases to catch all Cricbuzz match-end scenarios
RESULT_PHRASES = ["won by", "win by", "match drawn", "match tied", "abandoned", "no result", "beat", "beats"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

def get_ist_now():
    return datetime.now(IST)

# =====================
# DATABASE SETUP
# =====================
try:
    conn = sqlite3.connect("cricket_final.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY)")
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS state (m_id TEXT PRIMARY KEY, last_over REAL, last_wickets INTEGER, toss_done INTEGER DEFAULT 0, innings INTEGER DEFAULT 1)"
    )
    cursor.execute("CREATE TABLE IF NOT EXISTS daily_logs (date TEXT PRIMARY KEY)")
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS tracking_config (m_id TEXT PRIMARY KEY, match_name TEXT, is_active INTEGER DEFAULT 1)"
    )

    try: cursor.execute("ALTER TABLE state ADD COLUMN last_wicket_over REAL DEFAULT -10.0")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE state ADD COLUMN innings INTEGER DEFAULT 1")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE state ADD COLUMN last_double_strike_wk INTEGER DEFAULT 0")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE state ADD COLUMN last_score INTEGER DEFAULT 0")
    except sqlite3.OperationalError: pass
    
    conn.commit()
except Exception as e:
    logger.error(f"Database Initialization Error: {e}")

match_state = {}
last_update_id = None

# =====================
# AI ENGINE
# =====================
def get_pro_edit(match_facts):
    if not GROQ_API_KEY or not match_facts:
        return None

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    event_type = match_facts.get('event_type', '')
    custom_instruction = ""
    current_wickets = match_facts.get('wickets', 0)
    
    # Dynamic Tone Instructions
    if "IMPACT PLAYER" in event_type:
        custom_instruction = "Analyze the strategic impact of this substitution. Is it an offensive move to chase, or defensive to save wickets?"
    elif "STRATEGIC TIMEOUT" in event_type:
        custom_instruction = "Analyze the current run rate and the momentum. Who is winning this phase of the game?"
    elif "BIG OVER" in event_type:
        custom_instruction = "Hype up this massive over! Emphasize the massive momentum shift and how the bowler is getting destroyed."
    elif "DOUBLE_WICKET" in event_type:
        if current_wickets >= 5:
            custom_instruction = "The team has lost another two quick wickets and the lower order is exposed. Emphasize that this is a continuing, disastrous collapse."
        else:
            custom_instruction = "Two quick wickets have fallen! Emphasize the sudden shock and momentum shift in favor of the bowling team."
    elif "POWERPLAY" in event_type:
        custom_instruction = "Summarize the first 6 overs. Did the batting team dominate the field restrictions, or did the bowlers keep it tight?"
    elif "MATCH_END" in event_type:
        custom_instruction = "Write a thrilling match summary celebrating the winning team. Highlight the margin of victory."

    prompt = f"""You are a professional Cricket News Editor for a premium WhatsApp channel.
Rewrite the raw match data into a CRISP, EXCITING NARRATIVE post.

YOUR OUTPUT MUST MIRROR THE TONE AND STRUCTURE OF THESE EXAMPLES:

EXAMPLE 1 (Toss):
🏏 TOSS UPDATE – ENG vs SL 🏏
Sri Lanka have won the toss and elected to bowl first in their Super 8 opener at the Pallekele International Cricket Stadium.

A massive game in Group 2 to kick off the business end. The Lankan Lions will look to exploit the early moisture on a surface that promises plenty of turn. Game on!

EXAMPLE 2 (Match Update):
🏏 10 OVER UPDATE – ENG vs SL 🏏
England find themselves in a tough spot, reaching 68/4 after 10 overs in their Super 8 opener.

Phil Salt (37*) is leading a lone fightback, but Sri Lanka's spinners have dominated, including the massive wicket of captain Harry Brook (14) right at the 10-over mark. The middle order needs to stabilize quickly or risk a complete collapse.

---
STRICT CURRENT FACTS TO USE:
- Match: {match_facts.get('match_name', 'Unknown')}
- Event: {event_type}
- Batting Team (The team currently playing the balls): {match_facts.get('team_batting', 'Unknown')}
- Bowling Team: {match_facts.get('team_bowling', 'Unknown')}
- Current Innings: {match_facts.get('innings', 1)}
- Score: {match_facts.get('score_display', 'Unknown')}
- Official Status / Commentary: {match_facts.get('status_text', '')}

RULES:
1. Exactly 1 Heading and 2 narrative paragraphs.
2. IMPORTANT: Use a double newline (\n\n) between paragraphs.
3. Total Length: 3-4 sentences.
4. TONE INSTRUCTION: {custom_instruction if custom_instruction else "Make the summary engaging and analytical based on the current score."}
5. STRICT: If 'Current Innings' is 2, DO NOT mention who won the toss in your summary. Focus ONLY on the chase and the team currently batting.
6. NEVER invent stats not provided in the 'STRICT CURRENT FACTS' above.
"""

    data = {
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {"role": "system", "content": "You are an elite cricket news editor who mirror's the user's specific writing style examples perfectly. You change your tone based on the context of the game."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7, 
        "max_tokens": 130,
        "top_p": 0.9,
    }

    try:
        res = requests.post(url, headers=headers, json=data, timeout=15)
        res.raise_for_status()
        output = res.json()["choices"][0]["message"]["content"].strip()
        return output.replace("\n\n\n", "\n\n")
    except Exception as e:
        logger.warning("Groq API error: %s", e)
        return None

# =====================
# CORE UTILITIES
# =====================
def send_telegram(raw_text, pro_edit=False, match_facts=None):
    if not raw_text or not BOT_TOKEN or not CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    try:
        requests.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "text": raw_text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": "true",
            },
            timeout=10,
        )
    except requests.RequestException as exc:
        logger.warning("send_telegram raw failed: %s", exc)

    if pro_edit and GROQ_API_KEY and match_facts:
        ai_text = get_pro_edit(match_facts)
        if ai_text:
            try:
                requests.post(
                    url,
                    data={
                        "chat_id": CHAT_ID,
                        "text": ai_text, 
                        "parse_mode": "Markdown",
                        "disable_web_page_preview": "true",
                    },
                    timeout=10,
                )
            except requests.RequestException as exc:
                logger.warning("send_telegram AI failed: %s", exc)

def get_img_link(query):
    safe_query = urllib.parse.quote(f"{query} Cricket Match {get_ist_now().year}")
    return f"https://www.google.com/search?q={safe_query}&tbm=isch"

def overs_to_balls(overs):
    if not overs:
        return 0
    m = re.match(r"^(\d+)(?:\.(\d))?$", str(overs).strip())
    if not m:
        return 0
    whole = int(m.group(1))
    balls = int(m.group(2) or 0)
    balls = min(max(balls, 0), 5)
    return whole * 6 + balls

def stable_event_suffix(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]

def is_international_text_check(text):
    title = text.upper()
    
    if any(phrase in title for phrase in ["INDIAN PREMIER LEAGUE", " IPL ", "TATA IPL", "WPL"]):
        return True

    ipl_teams = [
        "MUMBAI INDIANS", "CHENNAI SUPER KINGS", "ROYAL CHALLENGERS BENGALURU", "ROYAL CHALLENGERS BANGALORE", 
        "KOLKATA KNIGHT RIDERS", "SUNRISERS HYDERABAD", "RAJASTHAN ROYALS", "DELHI CAPITALS",
        "PUNJAB KINGS", "LUCKNOW SUPER GIANTS", "GUJARAT TITANS"
    ]
    if any(team in title for team in ipl_teams):
        return True
        
    ipl_abbrevs = [r"\bRCB\b", r"\bCSK\b", r"\bMI\b", r"\bKKR\b", r"\bSRH\b", r"\bRR\b", r"\bDC\b", r"\bPBKS\b", r"\bLSG\b", r"\bGT\b"]
    for abbrev in ipl_abbrevs:
        if re.search(abbrev, title):
            return True

    if any(x in title for x in [" U19", "TROPHY", "LEAGUE", " XI", "INDIA A", "PAKISTAN A", "ENGLAND LIONS", "HONG KONG", "CHINA"]):
        return False
    
    countries = [
        "INDIA", "AUSTRALIA", "ENGLAND", "NEW ZEALAND", "SOUTH AFRICA",
        "PAKISTAN", "SRI LANKA", "WEST INDIES", "BANGLADESH", "ZIMBABWE",
        "AFGHANISTAN", "IRELAND"
    ]
    return sum(1 for c in countries if c in title) >= 2

def is_result_text(text):
    lower = (text or "").lower()
    return any(phrase in lower for phrase in RESULT_PHRASES)

def is_womens_match(match_name):
    name_up = match_name.upper()
    return "WOMEN" in name_up or " W " in name_up or name_up.endswith(" W") or "WPL" in name_up

def get_teams_from_name(match_name):
    teams = [t.strip() for t in re.split(r'\s+vs\s+|\s+v\s+', match_name, flags=re.IGNORECASE)]
    if len(teams) >= 2:
        return teams[0], teams[1]
    return "Team A", "Team B"

# =====================
# SCRAPING ENGINE HELPERS
# =====================
def scrape_todays_schedule():
    try:
        response = requests.get(
            "https://www.cricbuzz.com/cricket-schedule", headers=HEADERS, timeout=15
        )
        soup = BeautifulSoup(response.text, "html.parser")
        today_str = get_ist_now().strftime("%a %b %d").upper()
        todays_matches = []

        for block in soup.find_all("div", class_="cb-col-100 cb-col cb-schdl"):
            date_header = block.find("div", class_="cb-col-100 cb-col cb-lv-grn-strip")
            if not date_header or today_str not in date_header.get_text().upper():
                continue
            match_list = block.find_next_sibling("div")
            if not match_list:
                continue

            for match in match_list.find_all("div", class_="cb-ovr-flo"):
                match_info = match.get_text(strip=True)
                if is_international_text_check(match_info):
                    todays_matches.append(f"• {match_info}")

        if not todays_matches:
            return "No major matches scheduled for today."
        header = f"📅 *TODAY'S CRICKET SCHEDULE*\n—————————————————\n_{get_ist_now().strftime('%d %B %Y')}_\n\n"
        footer = "\n\n🖼 [Tap for Series Graphics]({})\n—————————————————\n🔔 *Keep notifications ON for live updates!*".format(
            get_img_link("Cricket Schedule")
        )
        return header + "\n".join(todays_matches) + footer
    except Exception as exc:
        logger.warning("Schedule scrape failed: %s", exc)
        return None

def handle_daily_briefing():
    now = get_ist_now()
    today_date = now.strftime("%Y-%m-%d")
    
    if now.hour >= 8:
        row = cursor.execute("SELECT date FROM daily_logs WHERE date=?", (today_date,)).fetchone()
        if not row:
            brief = scrape_todays_schedule()
            if brief:
                send_telegram(brief)
                cursor.execute("INSERT INTO daily_logs (date) VALUES (?)", (today_date,))
                conn.commit()

def _command_matches(text, command):
    return text.strip().startswith(command)

def handle_commands():
    global last_update_id
    if not BOT_TOKEN:
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {"timeout": 5}
    if last_update_id is not None:
        params["offset"] = last_update_id + 1

    try:
        res = requests.get(url, params=params, timeout=10).json()
        if not res.get("ok"):
            return

        for update in res.get("result", []):
            last_update_id = update["update_id"]
            msg_data = update.get("message") or update.get("channel_post")
            if not msg_data:
                continue
            text = msg_data.get("text", "")

            if _command_matches(text, "/tracklist"):
                matches = scrape_match_links()
                if not matches:
                    send_telegram("📭 No LIVE matches found right now.")
                else:
                    report = "📋 *TRACKING MANAGER*\n—————————————————\n"
                    for i, (name, link) in enumerate(matches):
                        m_id = link.split("/")[-2]
                        row = cursor.execute(
                            "SELECT is_active FROM tracking_config WHERE m_id=?", (m_id,)
                        ).fetchone()
                        
                        default_active = 0 if is_womens_match(name) else 1
                        is_active = row[0] if row else default_active
                        
                        status = "✅ Tracking" if is_active == 1 else "❌ Muted"
                        report += f"*{i + 1}.* {name}\nStatus: {status}\nToggle: `/track {i + 1}` or `/stop {i + 1}`\n\n"
                    send_telegram(report)

            elif _command_matches(text, "/track"):
                try:
                    idx = int(text.split()[-1]) - 1
                    matches = scrape_match_links()
                    name, link = matches[idx]
                    m_id = link.split("/")[-2]
                    cursor.execute(
                        "INSERT OR REPLACE INTO tracking_config VALUES (?, ?, 1)",
                        (m_id, name),
                    )
                    conn.commit()
                    send_telegram(f"✅ Now tracking: *{name}*")
                except (ValueError, IndexError):
                    send_telegram("⚠️ Invalid ID. Use /tracklist to see active match numbers.")

            elif _command_matches(text, "/stop"):
                try:
                    idx = int(text.split()[-1]) - 1
                    matches = scrape_match_links()
                    name, link = matches[idx]
                    m_id = link.split("/")[-2]
                    cursor.execute(
                        "INSERT OR REPLACE INTO tracking_config VALUES (?, ?, 0)",
                        (m_id, name),
                    )
                    conn.commit()
                    send_telegram(f"❌ Successfully Muted: *{name}*")
                except (ValueError, IndexError):
                    send_telegram("⚠️ Invalid ID. Use /tracklist to see active match numbers.")

            elif _command_matches(text, "/score"):
                send_telegram("🏏 *Fetching live matches...*")
                matches = scrape_match_links()
                if not matches:
                    send_telegram(
                        "⚠️ There are no relevant matches on the board right now."
                    )
                else:
                    summary_data = []
                    for name, link in matches[:5]:
                        score = scrape_instant_score(link)
                        summary_data.append(f"🔹 *{name}*\n{score}")
                    send_telegram(
                        "🏆 *LIVE MATCHES* 🏆\n—————————————————\n"
                        + "\n\n".join(summary_data)
                    )
    except Exception as e:
        logger.warning("Command Error: %s", e)

# =====================
# SCRAPING ENGINE
# =====================
def scrape_match_links():
    try:
        res = requests.get(
            "https://www.cricbuzz.com/cricket-match/live-scores",
            headers=HEADERS,
            timeout=15,
        )
        soup = BeautifulSoup(res.text, "html.parser")
        matches = []

        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            
            if "/live-cricket-scores/" not in href and "/cricket-scores/" not in href:
                continue

            name = a_tag.get("title", "").strip() or a_tag.get_text(
                separator=" ", strip=True
            )
            if not name or not is_international_text_check(name):
                continue

            full_link = "https://www.cricbuzz.com" + href if href.startswith("/") else href
            if not any(full_link == m[1] for m in matches):
                matches.append((name, full_link))
        return matches
    except Exception as e:
        logger.warning("match links scrape failed: %s", e)
        return []

def scrape_instant_score(match_url):
    try:
        response = requests.get(match_url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(response.text, "html.parser")
        score_div = soup.find(
            "div",
            class_=lambda x: x and (("text-3xl" in x and "font-bold" in x) or "cb-font-20" in x),
        )
        if not score_div:
            return "Score not available yet"

        p = score_div.find_all("div")
        if not p:
            return "Score structure unavailable"

        runs = p[0].get_text(strip=True)
        wickets = p[1].get_text(strip=True).replace("-", "") if len(p) > 1 else "0"
        overs = (
            p[2].get_text(strip=True).replace("(", "").replace(")", "")
            if len(p) > 2
            else ""
        )
        score_str = f"📊 {runs}-{wickets} ({overs} overs)"

        event_text = ""
        status_div = soup.find(
            "div",
            class_=lambda x: x
            and any(c in x for c in ["text-cb-danger", "text-cb-info", "text-cb-success"]),
        )
        if status_div:
            event_text = status_div.get_text(strip=True)

        if is_result_text(event_text):
            return f"{score_str}\n🎯 *Result:* {event_text}"
        return f"{score_str}\n🔥 *Latest:* {event_text}" if event_text else score_str
    except Exception as exc:
        logger.warning("instant score failed for %s: %s", match_url, exc)
        return "Error loading score"

def fetch_toss_update(match_url, match_name):
    if match_url not in match_state:
        match_state[match_url] = {"toss_sent": False}
    if match_state[match_url]["toss_sent"]:
        return

    scorecard_url = match_url.replace("live-cricket-scores", "live-cricket-scorecard").replace("cricket-scores", "live-cricket-scorecard").replace("www.cricbuzz.com", "m.cricbuzz.com")
    
    try:
        response = requests.get(scorecard_url, headers=HEADERS, timeout=15)
        if response.status_code != 200:
            return
        soup = BeautifulSoup(response.text, "html.parser")
        toss_label = soup.find(
            lambda tag: tag.name == "div"
            and "font-bold" in tag.get("class", [])
            and "Toss" in tag.get_text()
        )
        if not toss_label:
            return
        toss_text = toss_label.find_next("div").get_text(strip=True)
        match_state[match_url]["toss_sent"] = True

        msg = f"🪙 *TOSS UPDATE* 🪙\n—————————————————\n🏆 *{match_name}*\n\n🏟 *{toss_text}*\n\n🖼 [Tap for Toss Photos]({get_img_link(match_name + ' Toss')})\n—————————————————\n🏏 _Match starting soon! Get ready!_"
        
        mf = {"match_name": match_name, "event_type": "TOSS", "status_text": toss_text, "innings": 1}
        send_telegram(msg, pro_edit=True, match_facts=mf)
    except Exception as exc:
        logger.warning("fetch_toss_update failed: %s", exc)

def get_live_teams(soup, default_a, default_b):
    """
    BULLETPROOF TEAM DETECTION:
    Scrapes the actual HTML classes 'cb-text-bat' and 'cb-text-bowl' from the live page.
    This guarantees we know exactly who is batting and bowling without regex guessing.
    """
    team_batting = ""
    team_bowling = ""
    
    try:
        # Find the div that contains the batting team name
        bat_div = soup.find(class_=lambda x: x and 'cb-text-bat' in x)
        if bat_div:
            # Usually the parent or previous sibling holds the team name text
            parent = bat_div.find_parent("div", class_="flex")
            if parent:
                # Find the first text element, which is usually the team abbrev
                team_batting = parent.find("div").get_text(strip=True)
                
        # Find the div that contains the bowling team info
        bowl_div = soup.find(class_=lambda x: x and 'cb-text-bowl' in x)
        if bowl_div:
            parent = bowl_div.find_parent("div", class_="flex")
            if parent:
                team_bowling = parent.find("div").get_text(strip=True)
    except Exception:
        pass
        
    # Fallback to defaults if scraping fails
    if not team_batting: team_batting = default_a
    if not team_bowling: team_bowling = default_b
    
    return team_batting, team_bowling

def fetch_match_update(match_url, match_name):
    try:
        response = requests.get(match_url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(response.text, "html.parser")
        m_id = match_url.split("/")[-2] if "/" in match_url else stable_event_suffix(match_name)

        team_a, team_b = get_teams_from_name(match_name)

        # 1. STATUS TEXT
        status_text = ""
        status_div = soup.find("div", class_=lambda x: x and any(c in x for c in ["text-cb-danger", "text-cb-info", "text-cb-success", "cb-text-complete", "cb-text-abandon"]))
        if status_div:
            status_text = status_div.get_text(strip=True)

        if not status_text:
            alt_status = soup.find(lambda tag: tag.name == "div" and tag.get("class") and any(phrase in tag.get_text(strip=True).lower() for phrase in ["won by", "abandoned", "target ", "innings break", "stumps", "no result"]))
            if alt_status and len(alt_status.get_text(strip=True)) < 100:
                status_text = alt_status.get_text(strip=True)

        status_lower = status_text.lower()
        is_match_over = is_result_text(status_lower)

        # 2. LOAD DB STATE
        try:
            row = cursor.execute(
                "SELECT last_over, last_wickets, toss_done, last_wicket_over, innings, last_double_strike_wk, last_score FROM state WHERE m_id=?",
                (m_id,),
            ).fetchone()
            if row:
                last_ov, last_wk, toss_done, last_wk_ov, current_innings, last_double_strike_wk, last_score = row
            else:
                last_ov, last_wk, toss_done, last_wk_ov, current_innings, last_double_strike_wk, last_score = (0.0, 0, 0, -10.0, 1, 0, 0)
        except Exception:
            last_ov, last_wk, toss_done, last_wk_ov, current_innings, last_double_strike_wk, last_score = (0.0, 0, 0, -10.0, 1, 0, 0)

        # 3. SCORE PARSING
        score_div = soup.find("div", class_=lambda x: x and (("text-3xl" in x and "font-bold" in x) or "cb-font-20" in x))
        
        runs, wickets = 0, 0
        overs_raw = ""
        cur_overs, cur_balls = 0.0, 0
        full_score_text = ""

        if score_div:
            full_score_text = score_div.get_text(separator=" ", strip=True)
            
            p = score_div.find_all("div")
            if p:
                runs_text = p[0].get_text(strip=True).replace(",", "")
                runs = int("".join(filter(str.isdigit, runs_text)) or 0)
                if len(p) > 1:
                    w_text = p[1].get_text(strip=True).replace("-", "").replace("/", "")
                    wickets = int(w_text) if w_text.isdigit() else 0
                if len(p) > 2:
                    overs_raw = p[2].get_text(strip=True).replace("(", "").replace(")", "")

                cur_overs = float(overs_raw) if overs_raw.replace(".", "", 1).isdigit() else 0.0
                cur_balls = overs_to_balls(overs_raw)
                
        # 4. INNINGS DETECT & NEW BULLETPROOF TEAM ASSIGNMENT
        if cur_overs < last_ov - 5:
            last_ov = 0.0
            last_wk = 0
            last_wk_ov = -10.0
            last_double_strike_wk = 0  
            last_score = 0
            current_innings = 2

        # Ask Cricbuzz directly who is batting right now
        team_batting, team_bowling = get_live_teams(soup, team_a, team_b)

        score_display = f"{team_batting} {runs}/{wickets}" if team_batting else f"{runs}/{wickets}"

        is_innings_break = (wickets == 10 and not is_match_over) or any(
            phrase in status_lower for phrase in ["innings break", "target", "stumps", "lunch", "tea"]
        )

        # 5. GET COMMENTARY
        commentary_text = ""
        cm = soup.find("div", class_=lambda x: x and "leading-6" in x)
        if cm:
            eb = cm.find_all("div", recursive=False)
            if eb:
                t = eb[0] if "." in overs_raw else eb[-1]
                fl = t.find("div", class_=lambda x: x and "flex" in x and "gap-4" in x)
                if fl:
                    event_divs = fl.find_all("div", recursive=False)
                    if len(event_divs) >= 2:
                        commentary_text = event_divs[1].get_text(strip=True)

        event_text = status_text if status_text else commentary_text
        event_lower = event_text.lower()

        match_facts = {
            "match_name": match_name,
            "event_type": "LIVE UPDATE",
            "team_batting": team_batting,
            "team_bowling": team_bowling,
            "innings": current_innings,
            "score_display": score_display,
            "status_text": status_text,
            "wickets": wickets,
            "raw_data": full_score_text + " " + commentary_text
        }

        messages_to_send = []

        # ==========================================
        # 🚨 MATCH END LOGIC (ABSOLUTE PRIORITY)
        # ==========================================
        if is_match_over:
            eid = f"{m_id}_MATCH_END"
            if not cursor.execute("SELECT 1 FROM events WHERE id=?", (eid,)).fetchone():
                match_facts["event_type"] = "MATCH_END"
                msg = f"🏆 *MATCH COMPLETED: FINAL RESULT* 🏆\n—————————————————\n🎯 *{status_text}*\n\n🔹 {match_name}\n🔹 Final Score: *{score_display}* ({overs_raw})\n\n🖼 [Tap for Winning Moments]({get_img_link(match_name)})\n—————————————————\n✅ *Coverage concluded.*"
                cursor.execute("INSERT INTO events VALUES (?)", (eid,))
                cursor.execute("INSERT OR REPLACE INTO tracking_config VALUES (?, ?, 0)", (m_id, match_name))
                conn.commit()
                
                # Immediately send and stop processing
                send_telegram(msg, pro_edit=True, match_facts=match_facts)
                return 

        # ==========================================
        # 🚨 OTHER EVENTS
        # ==========================================
        if is_innings_break:
            eid = f"{m_id}_INNINGS_BREAK_{runs}"
            if not cursor.execute("SELECT 1 FROM events WHERE id=?", (eid,)).fetchone():
                match_facts["event_type"] = "INNINGS_BREAK"
                msg = f"🛑 *INNINGS COMPLETED* 🛑\n—————————————————\n🏏 *{match_name}* finishes their innings.\n\n📊 *FINAL SCORE:* *{score_display}*\n🎯 *UPDATE:* _{status_text}_\n\n🖼 [Tap for Match Gallery]({get_img_link(match_name)})\n—————————————————\n🕒 _Second innings starts shortly._"
                cursor.execute("INSERT INTO events VALUES (?)", (eid,))
                messages_to_send.append((msg, match_facts.copy()))

        if any(x in status_lower for x in ["rain", "drizzle", "interrupted", "delayed", "covers"]):
            eid = f"{m_id}_RAIN_{stable_event_suffix(status_text)}"
            if not cursor.execute("SELECT 1 FROM events WHERE id=?", (eid,)).fetchone():
                match_facts["event_type"] = "WEATHER_DELAY"
                msg = f"🌦 *WEATHER ALERT: {match_name}* 🌦\n—————————————————\n⚠️ {status_text}\n\n🕒 Match currently interrupted. Stay tuned for restart updates!"
                cursor.execute("INSERT INTO events VALUES (?)", (eid,))
                messages_to_send.append((msg, match_facts.copy()))

        if "timeout" in event_lower and "strategic" in event_lower:
            eid = f"{m_id}_TIMEOUT_{int(cur_overs)}"
            if not cursor.execute("SELECT 1 FROM events WHERE id=?", (eid,)).fetchone():
                match_facts["event_type"] = "STRATEGIC TIMEOUT"
                msg = f"⏸️ *STRATEGIC TIMEOUT* ⏸️\n—————————————————\n🏏 *MATCH:* {match_name}\n📊 *SCORE:* *{score_display}* ({overs_raw})\n\nTime to rethink strategies! Who is winning this phase?\n—————————————————\n"
                cursor.execute("INSERT INTO events VALUES (?)", (eid,))
                messages_to_send.append((msg, match_facts.copy()))

        if any(x in event_lower for x in ["impact player", "impact sub", "substituted by"]):
            eid = f"{m_id}_IMPACT_{stable_event_suffix(event_text)}"
            if not cursor.execute("SELECT 1 FROM events WHERE id=?", (eid,)).fetchone():
                match_facts["event_type"] = "IMPACT PLAYER SUBSTITUTION"
                msg = f"🔄 *IMPACT PLAYER ALERT* 🔄\n—————————————————\n🏏 *MATCH:* {match_name}\n\n📢 _{event_text}_\n\nA major tactical move! Let's see how this pays off.\n—————————————————\n"
                cursor.execute("INSERT INTO events VALUES (?)", (eid,))
                messages_to_send.append((msg, match_facts.copy()))

        if cur_overs > last_ov and str(cur_overs).endswith(".0"):
            runs_this_over = runs - last_score
            if runs_this_over >= 18:
                eid = f"{m_id}_BIG_OVER_{int(cur_overs)}_{runs_this_over}"
                if not cursor.execute("SELECT 1 FROM events WHERE id=?", (eid,)).fetchone():
                    match_facts["event_type"] = f"BIG OVER: {runs_this_over} runs"
                    msg = f"🔥 *MASSIVE OVER ALERT!* 🔥\n—————————————————\n🏏 *{runs_this_over} RUNS* off the last over!\n\n🏆 *{match_name}*\n📊 *SCORE:* *{score_display}* ({overs_raw})\n\nMomentum shifted completely! 🚀\n—————————————————\n"
                    cursor.execute("INSERT INTO events VALUES (?)", (eid,))
                    messages_to_send.append((msg, match_facts.copy()))
            last_score = runs

        if wickets > last_wk:
            new_wk_ov = cur_overs
            if wickets == 3 and cur_overs <= 6.0 and last_wk < 3:
                eid = f"{m_id}_COLLAPSE_3WK"
                if not cursor.execute("SELECT 1 FROM events WHERE id=?", (eid,)).fetchone():
                    match_facts["event_type"] = "BATTING_COLLAPSE"
                    msg = f"🚨 *EARLY COLLAPSE* 🚨\n—————————————————\n💥 Huge trouble early on!\n\n🏏 *MATCH:* {match_name}\n📊 *SCORE:* *{score_display}* ({overs_raw})\n💬 *LATEST WICKET:* _{event_text}_\n\n🖼 [Tap for Match Action]({get_img_link(match_name)})\n—————————————————\n📉 *The batting side is under massive pressure!*"
                    cursor.execute("INSERT INTO events VALUES (?)", (eid,))
                    messages_to_send.append((msg, match_facts.copy()))
            
            elif last_wk_ov > 0 and abs(cur_balls - overs_to_balls(last_wk_ov)) <= 6 and wickets >= last_double_strike_wk + 2:
                eid = f"{m_id}_DOUBLE_STRIKE_{wickets}"
                if not cursor.execute("SELECT 1 FROM events WHERE id=?", (eid,)).fetchone():
                    match_facts["event_type"] = "DOUBLE_WICKET"
                    msg = f"🔥 *DOUBLE STRIKE* 🔥\n—————————————————\n🎯 Two quick wickets have changed the momentum!\n\n🏏 *MATCH:* {match_name}\n📊 *NEW SCORE:* *{score_display}* ({overs_raw})\n💬 *LATEST:* _{event_text}_\n\n🖼 [Tap for Celebration Photos]({get_img_link(match_name)})\n—————————————————\n⚠️ *Huge turning point in the game!*"
                    cursor.execute("INSERT INTO events VALUES (?)", (eid,))
                    last_double_strike_wk = wickets 
                    messages_to_send.append((msg, match_facts.copy()))
            
            last_wk_ov = new_wk_ov

        is_t20 = "T20" in match_name.upper() or "INDIAN PREMIER LEAGUE" in match_name.upper() or " IPL " in match_name.upper() or match_name.upper().endswith(" IPL")
        is_odi = "ODI" in match_name.upper()
        
        if is_t20: milestones = [6, 10, 15, 20]
        elif is_odi: milestones = [10, 20, 30, 40, 50]
        else: milestones = [10, 20, 30, 40, 50, 60, 70, 80, 90]

        passed_m = None
        last_ov_balls = overs_to_balls(last_ov)
        
        for m in milestones:
            m_balls = m * 6
            if last_ov_balls < m_balls and cur_balls >= m_balls:
                passed_m = m
                break

        if passed_m:
            eid = f"{m_id}_OV_{passed_m}_{runs}_{current_innings}"
            if not cursor.execute("SELECT 1 FROM events WHERE id=?", (eid,)).fetchone():
                crr = f"{(runs / cur_overs):.2f}" if cur_overs else "N/A"
                phase_header = f"{passed_m}-OVER"
                event_tag = f"{phase_header} SUMMARY"
                if is_t20 and passed_m == 6: 
                    phase_header = "POWERPLAY END"
                    event_tag = "POWERPLAY"
                elif is_t20 and passed_m in [15, 20]: 
                    phase_header = "DEATH OVERS"

                match_facts["event_type"] = event_tag
                msg = f"🏏 *{phase_header} UPDATE* 🏏\n—————————————————\n🏆 *{match_name}*\n\n📊 *SCORE:* *{score_display}*\n🕒 *OVERS:* {cur_overs}\n📈 *RUN RATE:* {crr}\n\n⚡ *LATEST:* _{event_text}_\n\n🖼 [Tap for Match Photos]({get_img_link(match_name)})\n—————————————————\n🔔 *Stay tuned for more live action!*"
                cursor.execute("INSERT INTO events VALUES (?)", (eid,))
                messages_to_send.append((msg, match_facts.copy()))

        event_type = None
        speed_alert = ""
        balls_faced = 999
        ball_match = re.search(r"(\d+)\s*(balls|b)", event_lower)
        if ball_match:
            balls_faced = int(ball_match.group(1))

        if "orange cap" in event_lower:
            event_type = "ORANGE CAP"
            speed_alert = "🧢 LEADERBOARD SHAKEUP 🧢\n"
        elif "purple cap" in event_lower:
            event_type = "PURPLE CAP"
            speed_alert = "🧢 LEADERBOARD SHAKEUP 🧢\n"
        elif any(x in event_lower for x in ["fifty", "half-century", "half century", "50 runs", "reaches 50"]):
            event_type = "50"
            if balls_faced <= 25: speed_alert = "⚡ EXPLOSIVE INNINGS ⚡\n"
        elif any(x in event_lower for x in ["century", "hundred", "100 runs", "reaches 100"]):
            event_type = "100"
            if balls_faced <= 50: speed_alert = "⚡ SENSATIONAL CENTURY ⚡\n"

        if event_type:
            eid = f"{m_id}_MILESTONE_{stable_event_suffix(event_text)}"
            if not cursor.execute("SELECT 1 FROM events WHERE id=?", (eid,)).fetchone():
                header = f"🔥 *{event_type} REACHED!* 🔥" if "CAP" not in event_type else f"{speed_alert}"
                if speed_alert and "CAP" not in event_type: header = f"{speed_alert}{header}"
                
                match_facts["event_type"] = f"PLAYER {event_type} MILESTONE"
                msg = f"{header}\n—————————————————\n⭐ *Player Milestone*\n\n🏏 *MATCH:* {match_name}\n📊 *CURRENT SCORE:* *{score_display}* ({overs_raw})\n💬 *COMMENTARY:* _{event_text}_\n\n🖼 [Tap for Player Photos]({get_img_link(match_name + ' ' + event_text)})\n—————————————————\n👏 *What a moment! Share the news!*"
                cursor.execute("INSERT INTO events VALUES (?)", (eid,))
                messages_to_send.append((msg, match_facts.copy()))

        # SEND ALL MESSAGES
        for m, f in messages_to_send:
            send_telegram(m, pro_edit=True, match_facts=f)

        # UPDATE DB STATE
        try:
            cursor.execute(
                "INSERT OR REPLACE INTO state (m_id, last_over, last_wickets, toss_done, last_wicket_over, innings, last_double_strike_wk, last_score) VALUES (?,?,?,?,?,?,?,?)",
                (m_id, cur_overs, wickets, toss_done, last_wk_ov, current_innings, last_double_strike_wk, last_score),
            )
            conn.commit()
        except sqlite3.Error:
            pass

    except Exception as e:
        logger.error(f"fetch_match_update failed for {match_url}:\n{traceback.format_exc()}")

def run_bot():
    if not BOT_TOKEN or not CHAT_ID:
        logger.error("Missing BOT_TOKEN and/or CHAT_ID. Bot cannot start.")
        return

    logger.info("🚀 WhatsApp Content Assistant & Narrative AI Engine Starting...")
    send_telegram(
        "✅ *Live-Only Tracker Active!* 🏏\n- Zero spam guaranteed.\n- Use /stop to kill unwanted matches."
    )

    while True:
        try:
            handle_commands()
            handle_daily_briefing()

            matches = scrape_match_links()
            for name, link in matches:
                m_id = link.split("/")[-2]

                row = cursor.execute(
                    "SELECT is_active FROM tracking_config WHERE m_id=?", (m_id,)
                ).fetchone()
                
                default_active = 0 if is_womens_match(name) else 1
                is_tracking = row[0] if row else default_active

                if is_tracking == 0:
                    continue

                fetch_toss_update(link, name)
                fetch_match_update(link, name)

        except Exception as e:
            logger.error(f"Main Loop Error:\n{traceback.format_exc()}")

        time.sleep(15)

if __name__ == "__main__":
    run_bot()
