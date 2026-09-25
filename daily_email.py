#!/usr/bin/env python3
"""
Grind Line — Daily NHL Edge Report
Runs every morning at 7:00 AM MDT
Fetches tonight's NHL schedule, checks B2B situations, pulls lines, sends email via Brevo
"""

import os
import requests
import json
from datetime import datetime, timedelta
import pytz

from rest_edge import is_rest_edge, is_cancelled

# ── CONFIG ────────────────────────────────────────────────────────────────────
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "YOUR_BREVO_API_KEY_HERE")
ODDS_API_KEY  = os.environ.get("ODDS_API_KEY", "YOUR_ODDS_API_KEY_HERE")
FROM_EMAIL    = "hello@grindline.ca"
FROM_NAME     = "Grind Line"

# ── EMAIL DESIGN TOKENS ─────────────────────────────────────────────────────
# Pulled directly from grindline.ca's :root palette (index.html) so the email
# matches the site rather than approximating it. No CSS variables, gradients,
# flex/grid or webfonts here - Outlook's Word rendering engine supports none
# of that, so every value is inlined and layout stays table-based. Rounded
# corners and rgba tints are used where Outlook safely ignores/degrades them
# (square corners, or the solid text/border color still reads fine without
# the tint) - nothing depends on them rendering.
BG      = "#060b14"   # page background            (site --dark)
CARD    = "#0d1a2a"   # card/section background     (site --card)
BORDER  = "#162334"   # card/row borders            (site --border)
ACCENT  = "#00c46a"   # brand green / CTAs          (site --accent)
GREEN   = "#3ddc97"   # win / Rest Edge fired        (site --green)
RED     = "#ff4757"   # loss                        (site --red)
GOLD    = "#f5a623"   # confidence / secondary badge (site --gold)
CYAN    = "#00e88a"   # Emerging Edge accent        (site betting.html --cyan)
TEXT    = "#c5e8d5"   # body copy                   (site --text)
MUTED   = "#5a8a72"   # secondary/meta text         (site --muted)
BRIGHT  = "#ffffff"   # headings                    (site --bright)
FONT    = "'Segoe UI',system-ui,Arial,sans-serif"


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
    For each completed game yesterday, check if Rest Edge fired: away team
    on a back-to-back, home team rested exactly two days (see rest_edge.py).
    We re-derive rest days by checking the last two nights' schedules.
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

        # Rest days as of yesterday: 1 = played yesterday (B2B), 2 = played
        # two nights ago (one day off), 3 = neither (2+ days off).
        away_rest = 1 if away in played_two_days_ago else (2 if away in played_three_days_ago else 3)
        home_rest = 1 if home in played_two_days_ago else (2 if home in played_three_days_ago else 3)
        away_b2b = away_rest == 1

        signal_label = "Rest Edge" if is_rest_edge(away_rest, home_rest) else "No Signal"

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
        if is_rest_edge(away_rest, home_rest):
            signal = "HIGH"
            signal_label = "⚡ REST EDGE"
            signal_detail = f"{away} on B2B · {home} rested 2 days · back {home}"
        elif is_cancelled(away_rest, home_rest):
            signal = "CANCEL"
            signal_label = "↔ NO EDGE — BOTH B2B"
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
    order = {"HIGH": 0, "CANCEL": 1, None: 2}
    flagged.sort(key=lambda x: order.get(x["signal"], 3))
    return flagged

# ── BUILD RESULTS SECTION HTML ────────────────────────────────────────────────
def build_results_html(yesterday_results, yesterday_date):
    """A tight, one-line-per-game list - mirrors the site's recap-row/
    rr-result treatment (bordered card, thin row dividers, small right-
    aligned outcome badge) rather than a large card per game. This is what
    a no-Rest-Edge night leans on to still read as a full email."""
    if not yesterday_results:
        return ""

    date_label = datetime.strptime(yesterday_date, "%Y-%m-%d").strftime("%a %b %-d") if yesterday_date else "Yesterday"

    rows = ""
    for i, r in enumerate(yesterday_results):
        won = r["fade_won"]
        if won is True:
            badge_bg, badge_color, badge_border, badge_text = "rgba(61,220,151,0.12)", GREEN, "rgba(61,220,151,0.4)", "WIN"
        elif won is False:
            badge_bg, badge_color, badge_border, badge_text = "rgba(255,71,87,0.12)", RED, "rgba(255,71,87,0.35)", "LOSS"
        else:
            badge_bg, badge_color, badge_border, badge_text = "rgba(90,138,114,0.08)", MUTED, "rgba(90,138,114,0.25)", "NO SIGNAL"

        note = f'<div style="font-family:{FONT};font-size:11px;color:{MUTED};margin-top:3px;">Rest Edge · backed {r["home"]}</div>' if r["signal_label"] != "No Signal" else ""
        border_bottom = f"border-bottom:1px solid {BORDER};" if i < len(yesterday_results) - 1 else ""

        rows += f'''
        <tr><td style="padding:11px 20px;{border_bottom}">
          <table width="100%" cellpadding="0" cellspacing="0" role="presentation"><tr>
            <td style="font-family:{FONT};font-size:13px;font-weight:700;color:{BRIGHT};">{r["away"]} {r["away_score"]} @ {r["home"]} {r["home_score"]}</td>
            <td align="right" style="white-space:nowrap;padding-left:10px;">
              <span style="display:inline-block;background:{badge_bg};color:{badge_color};border:1px solid {badge_border};font-family:{FONT};font-size:10px;font-weight:700;letter-spacing:0.5px;text-transform:uppercase;padding:3px 9px;border-radius:4px;">{badge_text}</span>
            </td>
          </tr></table>
          {note}
        </td></tr>'''

    return f'''
    <tr><td style="padding:0 20px 16px;">
      <p style="font-family:{FONT};font-size:11px;font-weight:700;letter-spacing:2px;color:{BRIGHT};text-transform:uppercase;margin:0 0 10px;">Last Night — {date_label}</p>
      <table width="100%" cellpadding="0" cellspacing="0" role="presentation" bgcolor="{CARD}" style="background:{CARD};border:1px solid {BORDER};border-radius:12px;">
        {rows}
      </table>
    </td></tr>'''


def fetch_fantasy_picks():
    """Fetch today's fantasy picks from the live site, but only if they're
    today's - this repo has no filesystem access to grindline's data/
    directory, so this must go over HTTP, not a local file read."""
    try:
        r = requests.get("https://grindline.ca/data/fantasy.json", timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"Fantasy fetch error: {e}")
        return None
    today = datetime.now(MST).strftime("%Y-%m-%d")
    if data.get("date") != today:
        print(f"Fantasy data is stale ({data.get('date')}) - omitting section")
        return None
    return data


def build_fantasy_section(fantasy):
    """Build a condensed goalie-starts section for email. Props are
    deliberately not read here even though fantasy.json still carries a
    player_props field - the site split into betting.html/fantasy.html,
    props moved to betting.html's Props page, and this fantasy-only email
    should not show them anymore."""
    if not fantasy:
        return ""

    gs = fantasy.get("goalie_starts", {})
    goalies = [g for g in gs.get("goalies", []) if g.get("recommendation") == "start"][:2]

    if not goalies:
        return ""

    goalies_html = ""
    for i, g in enumerate(goalies):
        border_bottom = f"border-bottom:1px solid {BORDER};" if i < len(goalies) - 1 else ""
        goalies_html += f'''
        <tr><td style="padding:11px 20px;{border_bottom}">
          <table width="100%" cellpadding="0" cellspacing="0" role="presentation"><tr>
            <td style="font-family:{FONT};font-size:13px;font-weight:700;color:{BRIGHT};">
              <span style="display:inline-block;background:rgba(61,220,151,0.12);color:{GREEN};border:1px solid rgba(61,220,151,0.35);font-family:{FONT};font-size:10px;font-weight:700;letter-spacing:0.5px;text-transform:uppercase;padding:3px 8px;border-radius:4px;margin-right:8px;">{g.get("rec_label","Start")}</span>
              {g.get("name","")}
            </td>
            <td align="right" style="white-space:nowrap;padding-left:10px;font-family:{FONT};font-size:12px;color:{MUTED};">{g.get("sv_pct") or "—"} SV%</td>
          </tr></table>
          <div style="font-family:{FONT};font-size:11px;color:{MUTED};margin-top:3px;">{g.get("team","")} vs {g.get("opponent","")} · {g.get("gaa") or "—"} GAA{(" · " + g["rest_note"]) if g.get("rest_note") else ""}</div>
        </td></tr>'''

    sections = f'''
        <tr><td style="padding:14px 20px 4px;font-family:{FONT};font-size:10px;font-weight:700;letter-spacing:1.5px;color:{MUTED};text-transform:uppercase;">Goalie Starts</td></tr>
        {goalies_html}'''

    return f'''
        <!-- FANTASY PICKS -->
        <tr><td style="padding:0 20px 16px;">
          <p style="font-family:{FONT};font-size:11px;font-weight:700;letter-spacing:2px;color:{BRIGHT};text-transform:uppercase;margin:0 0 10px;">Tonight's Picks · {fantasy.get("date_label","")}</p>
          <table width="100%" cellpadding="0" cellspacing="0" role="presentation" bgcolor="{CARD}" style="background:{CARD};border:1px solid {BORDER};border-radius:12px;">
            {sections}
          </table>
          <p style="font-family:{FONT};font-size:12px;color:{MUTED};margin:10px 0 0;">Full Season Board — goalie table, usage leaders, weekly workload → <a href="https://grindline.ca/fantasy.html" style="color:{ACCENT};text-decoration:none;font-weight:700;">grindline.ca/fantasy.html</a></p>
        </td></tr>'''

def fmt_pct(v):
    if v is None:
        return "—"
    return f"{'+' if v >= 0 else ''}{v}%"


def fetch_emerging_edge_backtest():
    """Backtest validation numbers for the Emerging Edge section, fetched
    fresh each send from grindline.ca - no filesystem access between
    these two repos, same HTTP-only convention as fetch_fantasy_picks.
    Not date-gated like fantasy.json: this is a backtest result (four
    completed seasons), not daily data, so there's no "today's version"
    to check staleness against - it's the same file every day until the
    backtest itself is re-run."""
    try:
        r = requests.get("https://grindline.ca/data/goalie_overlay_broad_backtest.json", timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"Emerging Edge backtest fetch error: {e}")
        return None


def build_emerging_edge_html(backtest):
    """Static, evergreen block - NOT tied to tonight's slate. Unlike Rest
    Edge, whether a game qualifies depends on which goalie the away team
    actually starts, and that isn't knowable before puck drop (the same
    reason capture_signals.py logs it null and resolves it after the
    game on grindline's side) - so this never claims "fires tonight",
    only presents the tracked pattern itself, framed exactly as
    betting.html's Emerging Edge section: a tracked pattern acted on,
    explicitly not validated the way Rest Edge is, with the interval and
    the retirement test stated plainly, and the promise that results get
    published here either way."""
    if not backtest:
        return ""
    num1 = (backtest.get("goalie_overlay_broad_number_one") or {}).get("pooled") or {}
    backup = (backtest.get("goalie_overlay_broad_backup") or {}).get("pooled") or {}
    if not num1.get("n"):
        return ""
    ci = num1.get("roi_ci95") or [None, None]

    return f'''
    <tr><td style="padding:0 20px 16px;">
      <table width="100%" cellpadding="0" cellspacing="0" role="presentation" bgcolor="{CARD}" style="background:{CARD};border:1px solid rgba(0,232,138,0.35);border-radius:12px;">
        <tr>
          <td width="5" bgcolor="{CYAN}" style="background:{CYAN};font-size:1px;line-height:1px;">&nbsp;</td>
          <td style="padding:18px 20px;">
            <span style="display:inline-block;background:rgba(0,232,138,0.12);color:{CYAN};border:1px solid rgba(0,232,138,0.3);font-family:{FONT};font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;padding:4px 10px;border-radius:20px;">Emerging Edge</span>
            <div style="font-family:{FONT};font-size:15px;font-weight:800;color:{BRIGHT};margin:10px 0 6px;">Goalie Overlay — a tracked pattern, not yet a validated signal</div>
            <div style="font-family:{FONT};font-size:12px;color:{TEXT};line-height:1.6;">Away team on a back-to-back, home team rested (any amount — not the exact two days Rest Edge requires), and the away team starts its own #1 goalie by cumulative starts this season. Back the home side.</div>
            <div style="font-family:{FONT};font-size:12px;color:{TEXT};line-height:1.6;margin-top:10px;">{num1["n"]} games across four seasons, {num1["win_rate"]}% win rate, {fmt_pct(num1["roi"])} ROI at real closing prices, 95% CI {fmt_pct(ci[0])} to {fmt_pct(ci[1])} — positive in all four individual seasons, but that interval still includes zero. Not validated the way Rest Edge is (509 games).</div>
            <div style="font-family:{FONT};font-size:11px;color:{MUTED};line-height:1.6;margin-top:10px;">Comparison — away starts a backup instead: {backup.get("n","—")} games, {backup.get("win_rate","—")}% win rate, {fmt_pct(backup.get("roi"))} ROI — essentially flat.</div>
            <div style="font-family:{FONT};font-size:11px;color:{MUTED};line-height:1.6;margin-top:10px;padding-top:10px;border-top:1px solid {BORDER};">Tracked live from opening night. If the live sample's first 100 games run net negative, Emerging Edge gets retired publicly — the same predetermined rule Signal 1 was retired under. Results published here either way. Full detail → <a href="https://grindline.ca/betting.html" style="color:{CYAN};text-decoration:none;font-weight:700;">grindline.ca/betting.html</a></div>
          </td>
        </tr>
      </table>
    </td></tr>'''


def build_emerging_edge_text(backtest):
    """Plain-text mirror of build_emerging_edge_html - same content, same
    "not tied to tonight" framing."""
    if not backtest:
        return []
    num1 = (backtest.get("goalie_overlay_broad_number_one") or {}).get("pooled") or {}
    backup = (backtest.get("goalie_overlay_broad_backup") or {}).get("pooled") or {}
    if not num1.get("n"):
        return []
    ci = num1.get("roi_ci95") or [None, None]

    return [
        "EMERGING EDGE — GOALIE OVERLAY (tracked pattern, not yet a validated signal):",
        "  Away B2B + home rested (any amount, not just Rest Edge's exact 2 days) +",
        "  away starts its own #1 goalie by cumulative starts -> back home.",
        f"  {num1['n']} games across four seasons, {num1['win_rate']}% win rate, {fmt_pct(num1['roi'])} ROI at real",
        f"  closing prices, 95% CI {fmt_pct(ci[0])} to {fmt_pct(ci[1])} — positive in all four seasons, but",
        "  that interval still includes zero. Not validated the way Rest Edge is (509 games).",
        f"  Comparison (away starts a backup instead): {backup.get('n','—')} games, {backup.get('win_rate','—')}% win rate, {fmt_pct(backup.get('roi'))} ROI — essentially flat.",
        "  Tracked live from opening night. If the live sample's first 100 games run net",
        "  negative, Emerging Edge gets retired publicly — same rule Signal 1 was retired under.",
        "  Results published either way. Full detail: grindline.ca/betting.html",
        "",
    ]


# ── BUILD EMAIL HTML ──────────────────────────────────────────────────────────
def build_email_html(games_with_signals, odds_data, day_label, yesterday_results=None, yesterday_date="", emerging_backtest=None):
    """Leads with tonight (Rest Edge headline if it fired, then the full
    slate), then Emerging Edge (evergreen, not tonight-specific - see
    build_emerging_edge_html), then last night's compressed results, then
    picks. A Rest Edge night gets an unmistakable green-accented headline
    card; a quiet night still reads as a full email off the compact
    slate + results list rather than empty space where the headline used
    to be."""
    signal_games = [g for g in games_with_signals if g["signal"] == "HIGH"]

    def rest_edge_card(g):
        odds = match_odds(g, odds_data)
        def chip(book, val):
            if val == "N/A":
                return ""
            return f'<span style="display:inline-block;background:rgba(61,220,151,0.1);color:{GREEN};border:1px solid rgba(61,220,151,0.3);font-family:{FONT};font-size:11px;font-weight:700;padding:4px 9px;border-radius:4px;margin-right:5px;">{book} {val}</span>'
        odds_chips = (
            chip("DK", format_american(odds.get("draftkings_home")))
            + chip("FD", format_american(odds.get("fanduel_home")))
            + chip("MGM", format_american(odds.get("betmgm_home")))
            + chip("PIN", format_american(odds.get("pinnacle_home")))
        )
        odds_row = f'<div style="margin-top:12px;">{odds_chips}</div>' if odds_chips else ""
        return f'''
        <table width="100%" cellpadding="0" cellspacing="0" role="presentation" bgcolor="{CARD}" style="background:{CARD};border:1px solid rgba(61,220,151,0.4);border-radius:12px;margin-bottom:10px;">
          <tr>
            <td width="5" bgcolor="{GREEN}" style="background:{GREEN};font-size:1px;line-height:1px;">&nbsp;</td>
            <td style="padding:22px 24px;">
              <span style="display:inline-block;background:rgba(61,220,151,0.12);color:{GREEN};border:1px solid rgba(61,220,151,0.3);font-family:{FONT};font-size:11px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;padding:5px 12px;border-radius:20px;">⚡ Rest Edge Fires Tonight</span>
              <div style="font-family:{FONT};font-size:24px;font-weight:800;color:{BRIGHT};margin:16px 0 6px;">{g["away"]} @ {g["home"]}</div>
              <div style="font-family:{FONT};font-size:13px;color:{TEXT};line-height:1.5;">{g["away"]} on the road, second night of a back-to-back · {g["home"]} rested exactly two days · {g["time_str"]}</div>
              <div style="font-family:{FONT};font-size:17px;font-weight:800;color:{GREEN};margin-top:14px;">Back {g["home"]}</div>
              {odds_row}
              <div style="font-family:{FONT};font-size:11px;color:{MUTED};margin-top:14px;padding-top:12px;border-top:1px solid {BORDER};">62.1% win rate · +5.6% ROI · 509 games across four seasons</div>
            </td>
          </tr>
        </table>'''

    if signal_games:
        headline_html = "".join(rest_edge_card(g) for g in signal_games)
    else:
        headline_html = f'''
        <table width="100%" cellpadding="0" cellspacing="0" role="presentation" bgcolor="{CARD}" style="background:{CARD};border:1px solid {BORDER};border-radius:12px;margin-bottom:10px;">
          <tr><td style="padding:16px 20px;">
            <span style="font-family:{FONT};font-size:13px;font-weight:700;color:{TEXT};">No Rest Edge tonight</span>
            <div style="font-family:{FONT};font-size:12px;color:{MUTED};margin-top:4px;line-height:1.5;">No game on the slate has an away team on a back-to-back with the home team rested exactly two days.</div>
          </td></tr>
        </table>'''

    def slate_row(g, is_last):
        tags = ""
        if g["signal"] == "HIGH":
            tags += f'<span style="display:inline-block;background:rgba(61,220,151,0.12);color:{GREEN};border:1px solid rgba(61,220,151,0.35);font-family:{FONT};font-size:9px;font-weight:700;letter-spacing:0.5px;text-transform:uppercase;padding:2px 7px;border-radius:3px;margin-left:6px;">Rest Edge</span>'
        if g["away_b2b"]:
            tags += f'<span style="display:inline-block;background:rgba(90,138,114,0.1);color:{MUTED};border:1px solid rgba(90,138,114,0.25);font-family:{FONT};font-size:9px;font-weight:700;letter-spacing:0.5px;padding:2px 7px;border-radius:3px;margin-left:6px;">{g["away"]} B2B</span>'
        if g["home_b2b"]:
            tags += f'<span style="display:inline-block;background:rgba(90,138,114,0.1);color:{MUTED};border:1px solid rgba(90,138,114,0.25);font-family:{FONT};font-size:9px;font-weight:700;letter-spacing:0.5px;padding:2px 7px;border-radius:3px;margin-left:6px;">{g["home"]} B2B</span>'
        border_bottom = "" if is_last else f"border-bottom:1px solid {BORDER};"
        return f'''
        <tr><td style="padding:11px 20px;{border_bottom}">
          <table width="100%" cellpadding="0" cellspacing="0" role="presentation"><tr>
            <td style="font-family:{FONT};font-size:13px;font-weight:700;color:{BRIGHT};">{g["away"]} @ {g["home"]}{tags}</td>
            <td align="right" style="white-space:nowrap;padding-left:10px;font-family:{FONT};font-size:12px;color:{MUTED};">{g["time_str"]}</td>
          </tr></table>
        </td></tr>'''

    slate_rows = "".join(slate_row(g, i == len(games_with_signals) - 1) for i, g in enumerate(games_with_signals))
    slate_section = f'''
        <tr><td style="padding:0 20px 16px;">
          <p style="font-family:{FONT};font-size:11px;font-weight:700;letter-spacing:2px;color:{BRIGHT};text-transform:uppercase;margin:0 0 10px;">Tonight\'s Full Slate</p>
          <table width="100%" cellpadding="0" cellspacing="0" role="presentation" bgcolor="{CARD}" style="background:{CARD};border:1px solid {BORDER};border-radius:12px;">
            {slate_rows}
          </table>
        </td></tr>''' if games_with_signals else ""

    results_section = build_results_html(yesterday_results or [], yesterday_date)
    emerging_section = build_emerging_edge_html(emerging_backtest)

    body_html = f'''
        <!-- TONIGHT -->
        <tr><td style="padding:0 20px 10px;">
          <p style="font-family:{FONT};font-size:11px;font-weight:700;letter-spacing:2px;color:{BRIGHT};text-transform:uppercase;margin:0 0 10px;">Tonight</p>
          {headline_html}
        </td></tr>

        <!-- EMERGING EDGE (evergreen - not tonight-specific) -->
        {emerging_section}

        <!-- FULL SLATE -->
        {slate_section}

        <!-- LAST NIGHT\'S RESULTS -->
        {results_section}'''

    return _email_html_shell(
        "NHL Edge Report", day_label, body_html,
        home_url="https://grindline.ca/betting.html",
        footer_tagline="Full dashboard, live scores, Signal Tracker &amp; props",
    )


def _email_html_shell(title, day_label, body_html, home_url, footer_tagline):
    """Shared page chrome (head, header, footer) for both the Rest Edge
    and fantasy-picks emails - same brand wordmark, different title, body
    content, home link and footer tagline per send. home_url and
    footer_tagline are required (not defaulted) since grindline.ca split
    into betting.html/fantasy.html and each send must point at its own
    side, never at a bare grindline.ca that no longer represents either."""
    return f'''<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    @media only screen and (max-width: 600px) {{
      .email-container {{ width: 100% !important; }}
      .hide-mobile {{ display: none !important; }}
      td {{ padding-left: 14px !important; padding-right: 14px !important; }}
    }}
  </style>
</head>
<body style="margin:0;padding:0;background:{BG};font-family:{FONT};">
  <table width="100%" cellpadding="0" cellspacing="0" role="presentation" bgcolor="{BG}" style="background:{BG};">
    <tr><td align="center" style="padding:20px 8px;">
      <table class="email-container" width="600" cellpadding="0" cellspacing="0" role="presentation" style="max-width:600px;width:100%;">

        <!-- HEADER -->
        <tr><td style="padding:4px 20px 20px;">
          <span style="font-family:{FONT};font-size:11px;font-weight:800;letter-spacing:2px;color:{ACCENT};text-transform:uppercase;">Grind Line</span>
          <div style="font-family:{FONT};font-size:19px;font-weight:800;color:{BRIGHT};margin-top:6px;">{title}</div>
          <div style="font-family:{FONT};font-size:12px;color:{MUTED};margin-top:3px;">{day_label} · <a href="{home_url}" style="color:{MUTED};text-decoration:none;">{home_url.replace("https://", "")}</a></div>
        </td></tr>
        {body_html}

        <!-- FOOTER -->
        <tr><td style="padding:16px 20px 4px;border-top:1px solid {BORDER};text-align:center;">
          <p style="font-family:{FONT};font-size:11px;color:{MUTED};margin:0 0 6px;">
            {footer_tagline} → <a href="{home_url}" style="color:{ACCENT};text-decoration:none;">{home_url.replace("https://", "")}</a>
          </p>
          <p style="font-family:{FONT};font-size:10px;color:{MUTED};margin:0;">
            Grind Line · Statistical analysis for research purposes only · Not betting advice
          </p>
          <p style="font-family:{FONT};font-size:10px;color:{MUTED};margin:4px 0 0;">
            Reply to this email to unsubscribe.
          </p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>'''


def build_fantasy_email_html(fantasy, day_label):
    """Standalone fantasy-picks email: goalie starts only (props live on
    betting.html now), same page chrome as the Rest Edge email but linking
    to fantasy.html throughout. fantasy must already be validated non-empty
    by the caller - this does not itself decide whether to send."""
    body_html = build_fantasy_section(fantasy)
    return _email_html_shell(
        "Fantasy Picks", day_label, body_html,
        home_url="https://grindline.ca/fantasy.html",
        footer_tagline="Full Season Board, Players &amp; schedule analysis",
    )


# ── BUILD PLAIN TEXT VERSION ──────────────────────────────────────────────────
def build_email_text(games_with_signals, day_label, yesterday_results=None, yesterday_date="", emerging_backtest=None):
    """Mirrors the HTML: tonight leads (Rest Edge headline or a plain
    no-edge line), then Emerging Edge (evergreen, not tonight-specific),
    then the full slate, then a tight one-line-per-game results list,
    then picks."""
    lines = [
        f"Grind Line — NHL Edge Report — {day_label}",
        "=" * 50,
        ""
    ]

    signal_games = [g for g in games_with_signals if g["signal"] == "HIGH"]
    if signal_games:
        lines.append("⚡ REST EDGE FIRES TONIGHT:")
        for g in signal_games:
            lines.append(f"  {g['away']} @ {g['home']} — {g['time_str']}")
            lines.append(f"  {g['signal_detail']}")
            lines.append("  62.1% win rate · +5.6% ROI · 509 games across four seasons")
            lines.append("")
    else:
        lines.append("No Rest Edge tonight — no away-B2B/home-rested-2 matchup on the slate.")
        lines.append("")

    lines += build_emerging_edge_text(emerging_backtest)

    lines.append("FULL SLATE:")
    for g in games_with_signals:
        tags = []
        if g["signal"] == "HIGH":
            tags.append("REST EDGE")
        if g["away_b2b"]:
            tags.append(f"{g['away']} B2B")
        if g["home_b2b"]:
            tags.append(f"{g['home']} B2B")
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        lines.append(f"  {g['away']} @ {g['home']} — {g['time_str']}{tag_str}")

    if yesterday_results:
        date_label = datetime.strptime(yesterday_date, "%Y-%m-%d").strftime("%a %b %-d") if yesterday_date else "Yesterday"
        lines.append("")
        lines.append(f"LAST NIGHT — {date_label}:")
        for r in yesterday_results:
            won = r["fade_won"]
            result = "WIN" if won is True else "LOSS" if won is False else "no signal"
            note = " (Rest Edge)" if r["signal_label"] != "No Signal" else ""
            lines.append(f"  {r['away']} {r['away_score']} @ {r['home']} {r['home_score']} — {result}{note}")

    lines += ["", "grindline.ca/betting.html", "Not betting advice — for research purposes only", "Reply to this email to unsubscribe."]
    return "\n".join(lines)


def build_fantasy_email_text(fantasy, day_label):
    """Standalone fantasy-picks email, plain-text version: goalie starts
    only. Props are deliberately not read here (see build_fantasy_section's
    docstring) - they moved to betting.html's Props page. fantasy must
    already be validated non-empty by the caller."""
    lines = [
        f"Grind Line — Fantasy Picks — {day_label}",
        "=" * 50,
        ""
    ]

    gs = fantasy.get("goalie_starts", {})
    goalies = [g for g in gs.get("goalies", []) if g.get("recommendation") == "start"][:2]

    if goalies:
        lines.append("GOALIE STARTS:")
        for g in goalies:
            lines.append(f"  {g.get('name','')} ({g.get('team','')} vs {g.get('opponent','')}) — {g.get('sv_pct') or '—'} SV%, {g.get('gaa') or '—'} GAA")
        lines.append("")

    lines.append("Full Season Board — goalie table, usage leaders, weekly workload:")
    lines += ["  grindline.ca/fantasy.html", "", "Not betting advice — for research purposes only", "Reply to this email to unsubscribe."]
    return "\n".join(lines)

# ── FETCH BREVO CONTACTS ──────────────────────────────────────────────────────
def get_brevo_contacts():
    """GET /v3/contacts?listId=N does NOT filter by list - listId is
    silently ignored on that endpoint and it returns every contact in the
    account. Confirmed the hard way: the last send went to all 9 contacts
    on the account instead of the 1 actually on list 5. The endpoint that
    actually filters by list is GET /v3/contacts/lists/{listId}/contacts
    (see https://developers.brevo.com/reference/getcontactsfromlist)."""
    list_id = os.environ.get("BREVO_LIST_ID")
    if not list_id:
        print("BREVO_LIST_ID not set - refusing to fall back to the whole account")
        return []
    headers = {"api-key": BREVO_API_KEY, "Content-Type": "application/json"}
    emails = []
    offset = 0
    limit = 100
    try:
        while True:
            url = f"https://api.brevo.com/v3/contacts/lists/{list_id}/contacts"
            r = requests.get(url, headers=headers, params={"limit": limit, "offset": offset}, timeout=10)
            r.raise_for_status()
            contacts = r.json().get("contacts", [])
            emails.extend(c["email"] for c in contacts if c.get("email"))
            if len(contacts) < limit:
                break
            offset += limit
        print(f"Found {len(emails)} contact(s) on list {list_id}: {emails}")
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
def send_rest_edge_email():
    """The 14:00 UTC send: tonight's slate, the Rest Edge headline if it
    fired, and last night's compressed results. No fantasy content."""
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
    print(f"Rest Edge active in {n_signals} game(s) tonight")

    # 4. Fetch yesterday's scores and signal results
    print("Fetching yesterday's results...")
    yesterday_scores, yesterday_date = fetch_yesterday_scores()
    yesterday_results = get_yesterday_signals(yesterday_scores)

    # 5. Fetch odds
    print("Fetching odds...")
    odds_data = fetch_odds()
    print(f"Got odds for {len(odds_data)} events")

    # 5b. Fetch Emerging Edge backtest numbers (evergreen, not tonight-
    # specific - see build_emerging_edge_html). Never blocks the send:
    # a fetch failure just omits the section.
    print("Fetching Emerging Edge backtest...")
    emerging_backtest = fetch_emerging_edge_backtest()

    # 6. Build email
    print("Building email...")
    subject = f"⚡ NHL Edge Report — {day_label}" if n_signals > 0 else f"NHL Edge Report — {day_label}"
    html_content = build_email_html(games_with_signals, odds_data, day_label, yesterday_results, yesterday_date, emerging_backtest)
    text_content = build_email_text(games_with_signals, day_label, yesterday_results, yesterday_date, emerging_backtest)

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
        print("\n✓ Rest Edge email sent successfully")
    else:
        print("\n✗ Email failed — check logs above")


def send_fantasy_email():
    """The 17:30 UTC send: goalie starts and a link to the Season Board.
    No props - those moved to betting.html's Props page when the site
    split, and this send should not show them anymore even though
    fantasy.json still carries a player_props field. Runs 30 minutes after
    the fantasy pipeline writes fantasy.json, so the date guard in
    fetch_fantasy_picks should pass under normal operation. If it doesn't
    - stale data, fetch failure, or a valid-but-empty payload (e.g. an
    off-season placeholder with no goalie starts) - this skips the send
    entirely rather than mail out an empty shell."""
    day_label, today_str = get_today_str()

    print("Fetching fantasy picks...")
    fantasy = fetch_fantasy_picks()
    if fantasy is None:
        print("No usable fantasy data (missing, fetch failed, or not today's) — skipping send")
        return

    gs = fantasy.get("goalie_starts", {})
    goalies = [g for g in gs.get("goalies", []) if g.get("recommendation") == "start"]
    if not goalies:
        print("Fantasy data has no goalie starts today — skipping send")
        return

    print(f"Building fantasy email ({len(goalies)} goalie start(s))...")
    subject = f"🏒 Grind Line Fantasy Picks — {day_label}"
    html_content = build_fantasy_email_html(fantasy, day_label)
    text_content = build_fantasy_email_text(fantasy, day_label)

    print("Fetching Brevo contacts...")
    recipients = get_brevo_contacts()
    if not recipients:
        print("No contacts found — check Brevo list")
        return

    print(f"Sending to: {recipients}")
    success = send_email(recipients, subject, html_content, text_content)

    if success:
        print("\n✓ Fantasy email sent successfully")
    else:
        print("\n✗ Email failed — check logs above")


def main():
    mode = os.environ.get("EMAIL_MODE", "rest_edge").strip().lower()
    print(f"\n{'='*50}")
    print(f"Grind Line — Daily Email [{mode}] — {datetime.now(MST).strftime('%Y-%m-%d %H:%M MST')}")
    print(f"{'='*50}\n")

    if mode == "fantasy":
        send_fantasy_email()
    elif mode == "rest_edge":
        send_rest_edge_email()
    else:
        print(f"Unknown EMAIL_MODE {mode!r} — expected 'rest_edge' or 'fantasy'")

if __name__ == "__main__":
    main()
