#!/usr/bin/env python3
"""
Ice Cold Analytics — Daily NHL Edge Report
Runs every morning at 7:00 AM MST
Fetches tonight's NHL schedule, checks B2B situations, pulls lines, sends email via Brevo
"""

import os
import requests
import json
from datetime import datetime, timedelta
import pytz

# ── CONFIG ────────────────────────────────────────────────────────────────────
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "YOUR_BREVO_API_KEY_HERE")
ODDS_API_KEY  = os.environ.get("ODDS_API_KEY", "YOUR_ODDS_API_KEY_HERE")
FROM_EMAIL    = "info@icecoldanalytics.ca"
FROM_NAME     = "Ice Cold Analytics"
SEND_TO_LIST_ID = None

# ── TIMEZONE ──────────────────────────────────────────────────────────────────
MST = pytz.timezone("America/Edmonton")
UTC = pytz.utc

def get_today_str():
    now = datetime.now(MST)
    return now.strftime("%a %b %-d %Y"), now.strftime("%Y-%m-%d")

def get_day_label():
    now = datetime.now(MST)
    return now.strftime("%a %b %-d")

# ── FETCH TONIGHT'S NHL ODDS ──────────────────────────────────────────────────
def fetch_odds():
    url = "https://api.the-odds-api.com/v4/sports/icehockey_nhl/odds/"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us",
        "markets": "h2h",
        "oddsFormat": "american",
        "bookmakers": "draftkings,fanduel,betmgm,pinnacle"
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        print(f"Odds API credits used: {r.headers.get('x-requests-used', 'unknown')}")
        print(f"Odds API credits remaining: {r.headers.get('x-requests-remaining', 'unknown')}")
        return r.json()
    except Exception as e:
        print(f"Odds API error: {e}")
        return []

# ── FETCH NHL SCHEDULE ────────────────────────────────────────────────────────
def fetch_schedule():
    today = datetime.now(MST).strftime("%Y-%m-%d")
    url = f"https://api-web.nhle.com/v1/schedule/{today}"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        games = []
        for game_week in data.get("gameWeek", []):
            if game_week.get("date") == today:
                for g in game_week.get("games", []):
                    games.append({
                        "away": g["awayTeam"]["abbrev"],
                        "home": g["homeTeam"]["abbrev"],
                        "away_full": g["awayTeam"].get("placeName", {}).get("default", g["awayTeam"]["abbrev"]),
                        "home_full": g["homeTeam"].get("placeName", {}).get("default", g["homeTeam"]["abbrev"]),
                        "start_time_utc": g.get("startTimeUTC", ""),
                        "game_id": g.get("id", "")
                    })
        return games
    except Exception as e:
        print(f"NHL API error: {e}")
        return []

# ── FETCH YESTERDAY'S SCORES ──────────────────────────────────────────────────
def fetch_yesterday_scores():
    """Fetch completed scores from yesterday via NHL API"""
    mst_now = datetime.now(MST)
    yesterday = (mst_now - timedelta(days=1)).strftime("%Y-%m-%d")
    url = f"https://api-web.nhle.com/v1/score/{yesterday}"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        scores = []
        for g in data.get("games", []):
            state = g.get("gameState", "")
            if state in ("OFF", "FINAL"):
                scores.append({
                    "away": g["awayTeam"]["abbrev"],
                    "home": g["homeTeam"]["abbrev"],
                    "away_score": g["awayTeam"].get("score", 0),
                    "home_score": g["homeTeam"].get("score", 0),
                    "game_id": g.get("id", "")
                })
        print(f"Found {len(scores)} completed games yesterday")
        return scores, yesterday
    except Exception as e:
        print(f"Yesterday scores error: {e}")
        return [], ""

# ── GET YESTERDAY'S SIGNAL RESULTS ───────────────────────────────────────────
def get_yesterday_signals(scores):
    """
    For each completed game yesterday, check if Signal 1 was active.
    Signal 1: away team on B2B, home team rested 3+ days.
    We re-derive B2B by checking two days ago schedule.
    Returns list of result dicts for flagged games only.
    """
    if not scores:
        return []

    mst_now = datetime.now(MST)
    two_days_ago = (mst_now - timedelta(days=2)).strftime("%Y-%m-%d")
    three_days_ago = (mst_now - timedelta(days=3)).strftime("%Y-%m-%d")

    # Teams that played two days ago (= on B2B yesterday)
    played_two_days_ago = set()
    try:
        r = requests.get(f"https://api-web.nhle.com/v1/schedule/{two_days_ago}", timeout=10)
        data = r.json()
        for gw in data.get("gameWeek", []):
            if gw.get("date") == two_days_ago:
                for g in gw.get("games", []):
                    played_two_days_ago.add(g["awayTeam"]["abbrev"])
                    played_two_days_ago.add(g["homeTeam"]["abbrev"])
    except:
        pass

    # Teams that played three days ago (for home rest calculation)
    played_three_days_ago = set()
    try:
        r = requests.get(f"https://api-web.nhle.com/v1/schedule/{three_days_ago}", timeout=10)
        data = r.json()
        for gw in data.get("gameWeek", []):
            if gw.get("date") == three_days_ago:
                for g in gw.get("games", []):
                    played_three_days_ago.add(g["awayTeam"]["abbrev"])
                    played_three_days_ago.add(g["homeTeam"]["abbrev"])
    except:
        pass

    results = []
    for g in scores:
        away = g["away"]
        home = g["home"]

        away_b2b = away in played_two_days_ago
        home_b2b = home in played_two_days_ago

        # Home rest days as of yesterday
        if home in played_two_days_ago:
            home_rest = 1
        elif home in played_three_days_ago:
            home_rest = 2
        else:
            home_rest = 3

        # Signal 1 conditions
        if away_b2b and not home_b2b and home_rest >= 3:
            signal_label = "Signal 1"
        elif away_b2b and not home_b2b and home_rest == 2:
            signal_label = "Signal 1 Partial"
        else:
            signal_label = "No Signal"

        # Did the fade win? We fade the away team = home team wins
        home_won = g["home_score"] > g["away_score"]
        score_str = f"{g['away']} {g['away_score']} — {g['home']} {g['home_score']}"

        results.append({
            "away": away,
            "home": home,
            "away_score": g["away_score"],
            "home_score": g["home_score"],
            "score_str": score_str,
            "signal_label": signal_label,
            "fade_won": home_won if signal_label != "No Signal" else None,
            "away_b2b": away_b2b,
            "home_rest": home_rest
        })

    print(f"Yesterday's signal results: {len(results)} flagged game(s)")
    return results

# ── CHECK B2B SITUATIONS ──────────────────────────────────────────────────────
def check_b2b(games):
    mst_now = datetime.now(MST)
    yesterday = (mst_now - timedelta(days=1)).strftime("%Y-%m-%d")
    url = f"https://api-web.nhle.com/v1/schedule/{yesterday}"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        played_yesterday = set()
        for game_week in data.get("gameWeek", []):
            if game_week.get("date") == yesterday:
                for g in game_week.get("games", []):
                    played_yesterday.add(g["awayTeam"]["abbrev"])
                    played_yesterday.add(g["homeTeam"]["abbrev"])
        tonight_teams = set()
        for g in games:
            tonight_teams.add(g["away"])
            tonight_teams.add(g["home"])
        b2b_teams = played_yesterday & tonight_teams
        print(f"Teams on B2B tonight: {b2b_teams}")
        return b2b_teams, played_yesterday
    except Exception as e:
        print(f"B2B check error: {e}")
        return set(), set()

def get_rest_days(team, played_yesterday, played_two_days_ago):
    if team in played_yesterday:
        return 1
    elif team in played_two_days_ago:
        return 2
    else:
        return 3

def check_two_days_ago():
    mst_now = datetime.now(MST)
    two_days_ago = (mst_now - timedelta(days=2)).strftime("%Y-%m-%d")
    url = f"https://api-web.nhle.com/v1/schedule/{two_days_ago}"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        played = set()
        for game_week in data.get("gameWeek", []):
            if game_week.get("date") == two_days_ago:
                for g in game_week.get("games", []):
                    played.add(g["awayTeam"]["abbrev"])
                    played.add(g["homeTeam"]["abbrev"])
        return played
    except:
        return set()

# ── MATCH ODDS TO GAMES ───────────────────────────────────────────────────────
def match_odds(game, odds_data):
    away = game["away"].lower()
    home = game["home"].lower()
    name_map = {
        "tor": "toronto", "fla": "florida", "bos": "boston", "buf": "buffalo",
        "mtl": "montreal", "ott": "ottawa", "det": "detroit", "tbl": "tampa",
        "car": "carolina", "nyr": "new york rangers", "nyi": "new york islanders",
        "njd": "new jersey", "phi": "philadelphia", "pit": "pittsburgh",
        "wsh": "washington", "cbj": "columbus", "chi": "chicago",
        "nsh": "nashville", "stl": "st. louis", "min": "minnesota",
        "wpg": "winnipeg", "col": "colorado", "uta": "utah", "cgy": "calgary",
        "edm": "edmonton", "van": "vancouver", "sea": "seattle",
        "lak": "los angeles", "ana": "anaheim", "sjs": "san jose",
        "vgk": "vegas", "dal": "dallas"
    }
    away_search = name_map.get(away, away)
    home_search = name_map.get(home, home)
    for event in odds_data:
        teams = [t.lower() for t in [event.get("home_team",""), event.get("away_team","")]]
        if any(away_search in t for t in teams) and any(home_search in t for t in teams):
            lines = {}
            for bm in event.get("bookmakers", []):
                key = bm["key"]
                for market in bm.get("markets", []):
                    if market["key"] == "h2h":
                        for outcome in market.get("outcomes", []):
                            team_name = outcome["name"].lower()
                            price = outcome["price"]
                            if home_search in team_name:
                                lines[f"{key}_home"] = price
                            else:
                                lines[f"{key}_away"] = price
            return lines
    return {}

def format_american(odds):
    if odds is None:
        return "N/A"
    return f"+{odds}" if odds > 0 else str(odds)

# ── DETECT SIGNALS ────────────────────────────────────────────────────────────
def detect_signals(games, b2b_teams, played_yesterday, played_two_days_ago):
    flagged = []
    for g in games:
        away = g["away"]
        home = g["home"]
        away_b2b = away in b2b_teams
        home_b2b = home in b2b_teams
        away_rest = get_rest_days(away, played_yesterday, played_two_days_ago)
        home_rest = get_rest_days(home, played_yesterday, played_two_days_ago)
        signal = None
        signal_label = ""
        signal_detail = ""
        if away_b2b and not home_b2b and home_rest >= 3:
            signal = "HIGH"
            signal_label = "⚡ SIGNAL 1 ACTIVE"
            signal_detail = f"{away} on B2B · {home} rested {home_rest}+ days · +8.7% ROI historical"
        elif away_b2b and not home_b2b and home_rest == 2:
            signal = "MID"
            signal_label = "⚠ SIGNAL 1 PARTIAL"
            signal_detail = f"{away} on B2B · {home} rested 2 days"
        elif away_b2b and home_b2b:
            signal = "CANCEL"
            signal_label = "↔ SIGNALS CANCEL"
            signal_detail = "Both teams on B2B — no situational edge"
        try:
            utc_time = datetime.strptime(g["start_time_utc"], "%Y-%m-%dT%H:%M:%SZ")
            utc_time = UTC.localize(utc_time)
            et_time = utc_time.astimezone(pytz.timezone("America/New_York"))
            time_str = et_time.strftime("%-I:%M %p ET")
        except:
            time_str = "TBD"
        flagged.append({
            **g,
            "signal": signal,
            "signal_label": signal_label,
            "signal_detail": signal_detail,
            "away_b2b": away_b2b,
            "home_b2b": home_b2b,
            "away_rest": away_rest,
            "home_rest": home_rest,
            "time_str": time_str
        })
    order = {"HIGH": 0, "MID": 1, "CANCEL": 2, None: 3}
    flagged.sort(key=lambda x: order.get(x["signal"], 3))
    return flagged

# ── BUILD RESULTS SECTION HTML ────────────────────────────────────────────────
def build_results_html(yesterday_results, yesterday_date):
    if not yesterday_results:
        return ""

    date_label = datetime.strptime(yesterday_date, "%Y-%m-%d").strftime("%a %b %-d") if yesterday_date else "Yesterday"

    rows = ""
    for r in yesterday_results:
        icon = "✅" if r["fade_won"] is True else "❌" if r["fade_won"] is False else "·"
        result_color = "#00ff88" if r["fade_won"] is True else "#ff4444" if r["fade_won"] is False else "#8fafc4"
        result_word = "WIN" if r["fade_won"] is True else "LOSS" if r["fade_won"] is False else "NO SIGNAL"
        border_color = "#00ff88" if r["fade_won"] is True else "#ff4444" if r["fade_won"] is False else "#2a3d4a"

        if r["signal_label"] != "No Signal":
            fade_note = f"{r['signal_label']} · Fade {r['away']}{'  (B2B)' if r['away_b2b'] else ''} · Home rested {r['home_rest']}d"
        else:
            fade_note = "No signal"

        rows += f'''
        <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:8px;border:1px solid {border_color};border-radius:6px;background:#111d27;">
          <tr>
            <td style="padding:12px 14px 4px;">
              <span style="font-family:Arial,sans-serif;font-size:16px;font-weight:bold;color:#ffffff;">{r["away"]} @ {r["home"]}</span>
              <span style="font-family:Arial,sans-serif;font-size:13px;color:#adc8d8;margin-left:8px;">{r["score_str"]}</span>
            </td>
            <td style="padding:12px 14px 4px;text-align:right;white-space:nowrap;">
              <span style="font-family:Arial,sans-serif;font-size:13px;font-weight:bold;color:{result_color};">{icon} {result_word}</span>
            </td>
          </tr>
          <tr>
            <td colspan="2" style="padding:2px 14px 12px;font-family:Arial,sans-serif;font-size:12px;color:#8fafc4;">
              {fade_note}
            </td>
          </tr>
        </table>'''

    return f'''
    <tr><td style="background:#0d1a24;border-left:1px solid #1e2d38;border-right:1px solid #1e2d38;padding:16px 20px 8px;">
      <p style="font-family:Arial,sans-serif;font-size:11px;font-weight:bold;letter-spacing:2px;color:#00c2ff;text-transform:uppercase;margin:0 0 12px;">📊 Last Night's Results — {date_label}</p>
      {rows}
    </td></tr>'''


def fetch_fantasy_picks():
    """Fetch today's fantasy picks from the live site"""
    try:
        r = requests.get("https://icecoldanalytics.ca/data/fantasy.json", timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"Fantasy fetch error: {e}")
        return None


def build_fantasy_section(fantasy):
    """Build a condensed fantasy picks section for email"""
    if not fantasy:
        return ""

    vp = fantasy.get("value_plays", {})
    plays = vp.get("plays", [])[:5]
    pp = fantasy.get("player_props", {})
    props = pp.get("props", [])[:4]
    gs = fantasy.get("goalie_starts", {})
    goalies = [g for g in gs.get("goalies", []) if g.get("recommendation") == "start"][:2]

    if not plays:
        return ""

    plays_html = ""
    for p in plays:
        tier_color = "#ffb020" if p["tier"] == "S" else "#00c2ff" if p["tier"] == "A" else "#00ff88"
        tags_html = " ".join(f'<span style="background:#1e2d38;color:#adc8d8;font-family:Arial,sans-serif;font-size:10px;padding:3px 7px;border-radius:3px;margin-right:3px;">{t}</span>' for t in p.get("tags", []))
        plays_html += f'''
        <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:8px;border:1px solid #2a3d4a;border-radius:6px;background:#111d27;">
          <tr>
            <td style="padding:12px 14px 4px;">
              <span style="background:{tier_color};color:#000;font-family:Arial,sans-serif;font-size:10px;font-weight:bold;padding:3px 7px;border-radius:3px;margin-right:8px;">{p["tier"]}-TIER</span>
              <span style="font-family:Arial,sans-serif;font-size:15px;font-weight:bold;color:#ffffff;">{p["player"]}</span>
            </td>
            <td style="padding:12px 14px 4px;text-align:right;white-space:nowrap;">
              <span style="font-family:Arial,sans-serif;font-size:12px;color:#adc8d8;">DK {p["dk_salary"]}</span>
            </td>
          </tr>
          <tr>
            <td colspan="2" style="padding:2px 14px 6px;">
              <span style="font-family:Arial,sans-serif;font-size:12px;color:#adc8d8;">{p["team"]} · {p["position"]} · {p["matchup"]} · {p["game_time"]}</span>
            </td>
          </tr>
          <tr>
            <td colspan="2" style="padding:2px 14px 8px;">
              <span style="font-family:Arial,sans-serif;font-size:12px;color:#c8dce8;">{p["reason"][:140]}{"..." if len(p["reason"]) > 140 else ""}</span>
            </td>
          </tr>
          <tr>
            <td colspan="2" style="padding:0 14px 10px;">{tags_html}</td>
          </tr>
        </table>'''

    props_html = ""
    for p in props:
        pick_color = "#00ff88" if p["pick"] == "over" else "#ff4444" if p["pick"] == "under" else "#00c2ff"
        pick_bg = "rgba(0,255,136,0.15)" if p["pick"] == "over" else "rgba(255,68,68,0.15)" if p["pick"] == "under" else "rgba(0,194,255,0.15)"
        props_html += f'''
        <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:8px;border:1px solid #2a3d4a;border-radius:6px;background:#111d27;">
          <tr>
            <td style="padding:12px 14px 4px;">
              <span style="font-family:Arial,sans-serif;font-size:15px;font-weight:bold;color:#ffffff;">{p["player"]}</span>
              <span style="font-family:Arial,sans-serif;font-size:12px;color:#adc8d8;margin-left:6px;">— {p["prop_type"]}</span>
            </td>
            <td style="padding:12px 14px 4px;text-align:right;white-space:nowrap;">
              <span style="background:{pick_bg};color:{pick_color};font-family:Arial,sans-serif;font-size:12px;font-weight:bold;padding:4px 8px;border-radius:4px;">{p["pick"].upper()} {p["line"]}</span>
              <span style="font-family:Arial,sans-serif;font-size:12px;color:#adc8d8;margin-left:6px;">{p["odds"]}</span>
            </td>
          </tr>
          <tr>
            <td colspan="2" style="padding:2px 14px 10px;font-family:Arial,sans-serif;font-size:12px;color:#c8dce8;">{p["reason"][:140]}{"..." if len(p["reason"]) > 140 else ""}</td>
          </tr>
        </table>'''

    goalies_html = ""
    for g in goalies:
        goalies_html += f'''
        <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:8px;border:1px solid #2a3d4a;border-radius:6px;background:#111d27;">
          <tr>
            <td style="padding:12px 14px 4px;">
              <span style="background:#00ff88;color:#000;font-family:Arial,sans-serif;font-size:10px;font-weight:bold;padding:3px 7px;border-radius:3px;margin-right:8px;">▲ START</span>
              <span style="font-family:Arial,sans-serif;font-size:15px;font-weight:bold;color:#ffffff;">{g["name"]}</span>
            </td>
            <td style="padding:12px 14px 4px;text-align:right;white-space:nowrap;">
              <span style="font-family:Arial,sans-serif;font-size:12px;color:#adc8d8;">{g["sv_pct"]} SV%</span>
            </td>
          </tr>
          <tr>
            <td colspan="2" style="padding:2px 14px 10px;font-family:Arial,sans-serif;font-size:12px;color:#adc8d8;">{g["team"]} vs {g["opponent"]} · {g["gaa"]} GAA{(" · " + g["signal_note"]) if g.get("signal_note") else ""}</td>
          </tr>
        </table>'''

    return f'''
        <!-- FANTASY PICKS -->
        <tr><td style="background:#0d1a24;border-left:1px solid #1e2d38;border-right:1px solid #1e2d38;padding:16px 20px 8px;">
          <p style="font-family:Arial,sans-serif;font-size:11px;font-weight:bold;letter-spacing:2px;color:#00c2ff;text-transform:uppercase;margin:0 0 12px;">🏒 Top Fantasy Plays · {fantasy.get("date_label","")}</p>
          {plays_html}
          <p style="font-family:Arial,sans-serif;font-size:11px;font-weight:bold;letter-spacing:2px;color:#8fafc4;text-transform:uppercase;margin:16px 0 10px;">📊 Top Props</p>
          {props_html}
          {"<p style='font-family:Arial,sans-serif;font-size:11px;font-weight:bold;letter-spacing:2px;color:#8fafc4;text-transform:uppercase;margin:16px 0 10px;'>🥅 Goalie Starts</p>" + goalies_html if goalies_html else ""}
          <p style="font-family:Arial,sans-serif;font-size:12px;color:#8fafc4;margin:10px 0 0;">Full picks + goalie table → <a href="https://icecoldanalytics.ca" style="color:#00c2ff;text-decoration:none;font-weight:bold;">icecoldanalytics.ca</a></p>
        </td></tr>'''

# ── BUILD EMAIL HTML ──────────────────────────────────────────────────────────
def build_email_html(games_with_signals, odds_data, day_label, yesterday_results=None, yesterday_date="", fantasy=None):
    signal_games = [g for g in games_with_signals if g["signal"] in ("HIGH", "MID")]
    regular_games = [g for g in games_with_signals if g["signal"] not in ("HIGH", "MID")]

    def game_row(g, highlight=False):
        odds = match_odds(g, odds_data)
        away_b2b_badge = '<span style="background:#ff4444;color:#fff;font-family:Arial,sans-serif;font-size:10px;font-weight:bold;padding:3px 7px;border-radius:3px;margin-left:6px;display:inline-block;">B2B</span>' if g["away_b2b"] else ""
        home_b2b_badge = '<span style="background:#ff4444;color:#fff;font-family:Arial,sans-serif;font-size:10px;font-weight:bold;padding:3px 7px;border-radius:3px;margin-left:6px;display:inline-block;">B2B</span>' if g["home_b2b"] else ""
        dk_home = format_american(odds.get("draftkings_home"))
        fd_home = format_american(odds.get("fanduel_home"))
        mgm_home = format_american(odds.get("betmgm_home"))
        pin_home = format_american(odds.get("pinnacle_home"))
        border_color = "#ff4444" if highlight == "HIGH" else "#ffb020" if highlight == "MID" else "#2a3d4a"
        bg_color = "rgba(255,68,68,0.08)" if highlight == "HIGH" else "rgba(255,176,32,0.08)" if highlight == "MID" else "#111d27"
        signal_row = ""
        if g["signal"] in ("HIGH", "MID"):
            badge_bg = "#ff4444" if g["signal"] == "HIGH" else "#ffb020"
            signal_row = f'''
            <tr><td colspan="2" style="padding:4px 14px 10px;font-family:Arial,sans-serif;font-size:13px;font-weight:bold;color:{badge_bg};">
              {g["signal_label"]} — {g["signal_detail"]}
            </td></tr>'''
        odds_row = ""
        if any(v != "N/A" for v in [dk_home, fd_home, mgm_home, pin_home]):
            def chip(book, val):
                if val == "N/A": return ""
                return f'<span style="background:#1e2d38;color:#adc8d8;font-family:Arial,sans-serif;font-size:11px;padding:4px 8px;border-radius:3px;margin-right:4px;display:inline-block;">{book} {val}</span>'
            odds_row = f'''
            <tr><td colspan="2" style="padding:2px 14px 10px;">
              <span style="font-family:Arial,sans-serif;font-size:11px;color:#8fafc4;font-weight:bold;margin-right:8px;">HOME ML</span>
              {chip("DK", dk_home)}{chip("FD", fd_home)}{chip("MGM", mgm_home)}{chip("PIN", pin_home)}
            </td></tr>'''
        return f'''
        <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:8px;border:1px solid {border_color};border-radius:6px;background:{bg_color};">
          <tr>
            <td style="padding:12px 14px 4px;word-break:break-word;">
              <span style="font-family:Arial,sans-serif;font-size:17px;font-weight:bold;color:#ffffff;">{g["away"]}</span>{away_b2b_badge}
              <span style="font-family:Arial,sans-serif;font-size:14px;color:#8fafc4;margin:0 8px;">@</span>
              <span style="font-family:Arial,sans-serif;font-size:17px;font-weight:bold;color:#ffffff;">{g["home"]}</span>{home_b2b_badge}
            </td>
            <td style="padding:12px 14px 4px;text-align:right;font-family:Arial,sans-serif;font-size:12px;color:#adc8d8;white-space:nowrap;">{g["time_str"]}</td>
          </tr>
          {signal_row}
          {odds_row}
        </table>'''
    n_signals = len(signal_games)
    signal_summary = f"{n_signals} signal game{'s' if n_signals != 1 else ''} tonight" if n_signals > 0 else "No high-confidence signals tonight"
    signal_color = "#ff4444" if n_signals > 0 else "#5a7a8a"
    signal_games_html = "".join(game_row(g, highlight=g["signal"]) for g in signal_games)
    regular_games_html = "".join(game_row(g) for g in regular_games)

    regular_section = ""
    if regular_games:
        regular_section = f'''
        <tr><td style="background:#0d1a24;border-left:1px solid #1e2d38;border-right:1px solid #1e2d38;padding:20px 24px 8px;">
         <p style="font-family:Arial,sans-serif;font-size:11px;font-weight:bold;letter-spacing:2px;color:#8fafc4;text-transform:uppercase;margin:0 0 12px;">Tonight\'s Full Slate</p>
          {regular_games_html}
        </td></tr>'''

    results_section = build_results_html(yesterday_results or [], yesterday_date)
    fantasy_section = build_fantasy_section(fantasy)

    html = f'''<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    @media only screen and (max-width: 600px) {{
      .email-container {{ width: 100% !important; }}
      .game-team {{ font-size: 14px !important; }}
      .hide-mobile {{ display: none !important; }}
      td {{ padding-left: 12px !important; padding-right: 12px !important; }}
    }}
  </style>
</head>
<body style="margin:0;padding:0;background:#0a0f14;font-family:sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0a0f14;">
    <tr><td align="center" style="padding:16px 8px;">
      <table class="email-container" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">

        <!-- HEADER -->
        <tr><td style="background:#0d1a24;border:1px solid #1e2d38;border-radius:6px 6px 0 0;padding:20px 24px;">
          <p style="font-family:monospace;font-size:9px;letter-spacing:3px;color:#00c2ff;text-transform:uppercase;margin:0 0 4px;">Ice Cold Analytics</p>
          <p style="font-family:monospace;font-size:20px;font-weight:bold;color:#e8f0f4;margin:0;">NHL Edge Report</p>
          <p style="font-family:monospace;font-size:11px;color:#5a7a8a;margin:4px 0 0;">{day_label} · <a href="https://icecoldanalytics.ca" style="color:#5a7a8a;text-decoration:none;">icecoldanalytics.ca</a></p>
        </td></tr>

        <!-- SIGNAL SUMMARY BAR -->
        <tr><td style="background:#111d27;border-left:1px solid #1e2d38;border-right:1px solid #1e2d38;padding:12px 24px;">
          <span style="font-family:monospace;font-size:12px;color:{signal_color};font-weight:bold;">{signal_summary}</span>
          <span style="font-family:monospace;font-size:10px;color:#5a7a8a;margin-left:12px;">Signal 1: B2B + rest differential (+8.7% ROI historical)</span>
        </td></tr>

        <!-- LAST NIGHT\'S RESULTS -->
        {results_section}

        <!-- SIGNAL GAMES -->
        {'<tr><td style="background:#0d1a24;border-left:1px solid #1e2d38;border-right:1px solid #1e2d38;padding:16px 24px 8px;"><p style="font-family:monospace;font-size:9px;letter-spacing:2px;color:#ff4444;text-transform:uppercase;margin:0 0 10px;">⚡ Flagged Games</p>' + signal_games_html + '</td></tr>' if signal_games else ''}

        <!-- ALL OTHER GAMES -->
        {regular_section}

        <!-- FANTASY PICKS -->
        {fantasy_section}

        <!-- FOOTER -->
        <tr><td style="background:#0d1a24;border:1px solid #1e2d38;border-radius:0 0 6px 6px;padding:16px 24px;text-align:center;">
          <p style="font-family:monospace;font-size:9px;color:#5a7a8a;margin:0 0 6px;">
            Full dashboard, live scores + DFS tools → <a href="https://icecoldanalytics.ca" style="color:#00c2ff;text-decoration:none;">icecoldanalytics.ca</a>
          </p>
          <p style="font-family:monospace;font-size:8px;color:#2a3d4a;margin:0;">
            Ice Cold Analytics · Statistical analysis for research purposes only · Not betting advice ·
            <a href="*|UNSUBSCRIBE|*" style="color:#2a3d4a;">Unsubscribe</a>
          </p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>'''
    return html
# ── BUILD PLAIN TEXT VERSION ──────────────────────────────────────────────────
def build_email_text(games_with_signals, day_label, yesterday_results=None, yesterday_date=""):
    lines = [
        f"ICE COLD ANALYTICS — NHL Edge Report — {day_label}",
        "=" * 50,
        ""
    ]

    if yesterday_results:
        date_label = datetime.strptime(yesterday_date, "%Y-%m-%d").strftime("%a %b %-d") if yesterday_date else "Yesterday"
        lines.append(f"📊 LAST NIGHT'S RESULTS — {date_label}:")
        for r in yesterday_results:
            result = "✅ WIN" if r["fade_won"] else "❌ LOSS"
            lines.append(f"  {r['away']} @ {r['home']} — {r['score_str']} — {result}")
            lines.append(f"  {r['signal_label']} · Fade {r['away']}")
            lines.append("")

    signal_games = [g for g in games_with_signals if g["signal"] in ("HIGH", "MID")]
    if signal_games:
        lines.append("⚡ FLAGGED GAMES TONIGHT:")
        for g in signal_games:
            lines.append(f"  {g['away']} @ {g['home']} — {g['time_str']}")
            lines.append(f"  {g['signal_label']}: {g['signal_detail']}")
            lines.append("")
    else:
        lines.append("No high-confidence signals tonight.")
        lines.append("")

    lines.append("FULL SLATE:")
    for g in games_with_signals:
        b2b = " [B2B]" if g["away_b2b"] or g["home_b2b"] else ""
        lines.append(f"  {g['away']} @ {g['home']} — {g['time_str']}{b2b}")

    lines += ["", "icecoldanalytics.ca", "Not betting advice — for research purposes only"]
    return "\n".join(lines)

# ── FETCH BREVO CONTACTS ──────────────────────────────────────────────────────
def get_brevo_contacts():
    url = "https://api.brevo.com/v3/contacts"
    headers = {"api-key": BREVO_API_KEY, "Content-Type": "application/json"}
    try:
        list_id = os.environ.get("BREVO_LIST_ID")
        params = {"limit": 100}
        if list_id:
            params["listId"] = int(list_id)
        r = requests.get(url, headers=headers, params=params, timeout=10)
        r.raise_for_status()
        contacts = r.json().get("contacts", [])
        emails = [c["email"] for c in contacts if c.get("email")]
        print(f"Found {len(emails)} contacts: {emails}")
        return emails
    except Exception as e:
        print(f"Brevo contacts error: {e}")
        return []

# ── SEND EMAIL VIA BREVO ──────────────────────────────────────────────────────
def send_email(to_emails, subject, html_content, text_content):
    url = "https://api.brevo.com/v3/smtp/email"
    headers = {"api-key": BREVO_API_KEY, "Content-Type": "application/json"}
    bcc_list = [{"email": email} for email in to_emails]
    payload = {
        "sender": {"name": FROM_NAME, "email": FROM_EMAIL},
        "to": [{"email": FROM_EMAIL}],
        "bcc": bcc_list,
        "subject": subject,
        "htmlContent": html_content,
        "textContent": text_content
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=15)
        r.raise_for_status()
        print(f"✓ Email sent to {len(to_emails)} recipient(s)")
        return True
    except Exception as e:
        print(f"✗ Email send error: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"  Response: {e.response.text}")
        return False

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*50}")
    print(f"Ice Cold Analytics — Daily Email — {datetime.now(MST).strftime('%Y-%m-%d %H:%M MST')}")
    print(f"{'='*50}\n")

    day_label, today_str = get_today_str()

    # 1. Fetch tonight's schedule
    print("Fetching NHL schedule...")
    games = fetch_schedule()
    if not games:
        print("No games today or schedule fetch failed — skipping email")
        return
    print(f"Found {len(games)} games tonight")

    # 2. Check B2B
    print("Checking B2B situations...")
    b2b_teams, played_yesterday = check_b2b(games)
    played_two_days_ago = check_two_days_ago()

    # 3. Detect signals
    print("Detecting signals...")
    games_with_signals = detect_signals(games, b2b_teams, played_yesterday, played_two_days_ago)
    n_signals = sum(1 for g in games_with_signals if g["signal"] == "HIGH")
    print(f"Signal 1 active in {n_signals} game(s) tonight")

    # 4. Fetch yesterday's scores and signal results
    print("Fetching yesterday's results...")
    yesterday_scores, yesterday_date = fetch_yesterday_scores()
    yesterday_results = get_yesterday_signals(yesterday_scores)

    # 5. Fetch odds
    print("Fetching odds...")
    odds_data = fetch_odds()
    print(f"Got odds for {len(odds_data)} events")

    # 6. Build email
    print("Building email...")
    subject = f"⚡ NHL Edge Report — {day_label}" if n_signals > 0 else f"NHL Edge Report — {day_label}"
    fantasy = fetch_fantasy_picks()
    html_content = build_email_html(games_with_signals, odds_data, day_label, yesterday_results, yesterday_date, fantasy=fantasy)
    text_content = build_email_text(games_with_signals, day_label, yesterday_results, yesterday_date)

    # 7. Get recipients
    print("Fetching Brevo contacts...")
    recipients = get_brevo_contacts()
    if not recipients:
        print("No contacts found — check Brevo list")
        return

    # 8. Send
    print(f"Sending to: {recipients}")
    success = send_email(recipients, subject, html_content, text_content)

    if success:
        print("\n✓ Daily email sent successfully")
    else:
        print("\n✗ Email failed — check logs above")

if __name__ == "__main__":
    main()
