#!/usr/bin/env python3
"""
Grind Line — My Team roster email

Sends every signed-in user with a saved roster and email_opt_in = true
their own personalized weekly summary: projected points this week, games
this week, and the 4-game/2-game split - the same figures shown on
grindline.ca/fantasy.html's My Team page, for their own roster only.

Separate from daily_email.py's two broadcast sends (Rest Edge, and the
older goalie-starts "fantasy" send): both of those build ONE email and
BCC a static Brevo list. This one is inherently per-user - every
recipient must see only their own roster - sourced from Supabase rather
than a public JSON file alone, and sent as one real Brevo transactional
call per recipient rather than one call with everyone bcc'd. Shares page
chrome, color tokens and the day-label helper with daily_email.py via
import rather than duplicating them (grep for `from daily_email import`
below) - that module has no import-time side effects (no network calls,
env vars read with a safe fallback), so importing it here is safe.

Requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (Project Settings ->
API -> the service_role key specifically - it bypasses Row Level
Security by design, which is exactly why it must only ever live here as
a GitHub Actions secret, never client-side, never in grindline's own
fantasy.html). BREVO_API_KEY is the same secret daily_email.py uses.

auth.users is NOT exposed through Supabase's PostgREST data API (the
auth schema is deliberately kept off that surface) - user emails come
from the Admin Auth endpoint instead (/auth/v1/admin/users), the
documented server-side way to list accounts, authenticated with the
same service_role bearer token.

Run:  python my_team_email.py
"""
import os
import time

import requests

from daily_email import (
    _email_html_shell, get_day_label,
    FROM_EMAIL, FROM_NAME,
    CARD, BORDER, ACCENT, GREEN, GOLD, TEXT, MUTED, BRIGHT, FONT,
)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")

PLAYER_HUB_URL = "https://grindline.ca/data/player_hub.json"
SEND_DELAY_SECONDS = 0.25  # a courtesy pace against Brevo's transactional rate limit, not a hard requirement


def _supabase_headers():
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
    }


# ── SUPABASE FETCHES ──────────────────────────────────────────────────────────
def fetch_all_roster_rows():
    """Every roster_players row, across every user, in ONE call - the
    service_role key bypasses RLS, so there's no need to query per user.
    Returns {user_id: {"skaters": [...ids], "goalies": [...ids]}}, ids as
    strings to match player_hub.json's own key shape."""
    url = f"{SUPABASE_URL}/rest/v1/roster_players"
    params = {"select": "user_id,nhl_player_id,player_type"}
    try:
        r = requests.get(url, headers=_supabase_headers(), params=params, timeout=15)
        r.raise_for_status()
        rows = r.json()
    except Exception as e:
        print(f"roster_players fetch error: {e}")
        return {}
    out = {}
    for row in rows:
        bucket = out.setdefault(row["user_id"], {"skaters": [], "goalies": []})
        pid = str(row["nhl_player_id"])
        if row.get("player_type") == "goalie":
            bucket["goalies"].append(pid)
        else:
            bucket["skaters"].append(pid)
    return out


def fetch_opted_in_user_ids():
    """{user_id, ...} for every profile with email_opt_in = true."""
    url = f"{SUPABASE_URL}/rest/v1/profiles"
    params = {"select": "id", "email_opt_in": "eq.true"}
    try:
        r = requests.get(url, headers=_supabase_headers(), params=params, timeout=15)
        r.raise_for_status()
        rows = r.json()
    except Exception as e:
        print(f"profiles fetch error: {e}")
        return set()
    return {row["id"] for row in rows}


def fetch_user_emails():
    """{user_id: email} for every account, via the Admin Auth endpoint
    (paginated - Supabase's default page size is small enough that a real
    user base needs more than one page)."""
    out = {}
    page = 1
    per_page = 200
    while True:
        url = f"{SUPABASE_URL}/auth/v1/admin/users"
        params = {"page": page, "per_page": per_page}
        try:
            r = requests.get(url, headers=_supabase_headers(), params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"auth admin users fetch error (page {page}): {e}")
            break
        users = data.get("users", [])
        for u in users:
            if u.get("email"):
                out[u["id"]] = u["email"]
        if len(users) < per_page:
            break
        page += 1
    return out


def fetch_player_hub():
    """This repo has no filesystem access to grindline's data/ directory -
    same cross-repo HTTP pattern as daily_email.py's fetch_fantasy_picks()
    and fetch_emerging_edge_backtest(). Fetched once per run, not once per
    user - it's the shared join target for every recipient's roster."""
    try:
        r = requests.get(PLAYER_HUB_URL, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"player_hub.json fetch error: {e}")
        return None


# ── JOIN + WEEKLY SUMMARY ─────────────────────────────────────────────────────
def resolve_roster(ids, hub_section):
    """ids: list of playerId strings. hub_section: player_hub.json's
    "players" or "goalies" dict. An id with no match (traded off every
    roster since being added, a stale id) is left out rather than
    breaking the whole send - blanks, not drops, the same rule every
    other script in this project follows."""
    resolved = []
    for pid in ids:
        p = hub_section.get(pid)
        if p:
            resolved.append({"id": pid, **p})
    return resolved


def compute_weekly_summary(skaters):
    """Direct port of fantasy.html's computeSkaterSummary() (grindline
    repo) - skaters only, since goalies carry no "projection" field in
    player_hub.json at all (see build_player_hub.py's docstring: goalie
    fantasy scoring isn't points-based, so no projection is invented for
    them)."""
    total_projected = 0.0
    total_games = 0
    four_game = 0
    two_game = 0
    for p in skaters:
        proj = p.get("projection") or {}
        pts = proj.get("projected_points")
        if pts is not None:
            total_projected += pts
        gw = proj.get("games_this_week")
        if gw is not None:
            total_games += gw
            if gw == 4:
                four_game += 1
            elif gw == 2:
                two_game += 1
    return {
        "total_projected": round(total_projected, 2),
        "total_games": total_games,
        "four_game": four_game,
        "two_game": two_game,
    }


# ── BUILD HTML ────────────────────────────────────────────────────────────────
def _skater_rows_html(skaters):
    rows = ""
    for i, p in enumerate(skaters):
        border = f"border-bottom:1px solid {BORDER};" if i < len(skaters) - 1 else ""
        proj = p.get("projection") or {}
        pts = proj.get("projected_points")
        pts_str = f"{pts:.2f}" if pts is not None else "—"
        gw = proj.get("games_this_week")
        gw_str = str(gw) if gw is not None else "—"
        games_word = "game" if gw == 1 else "games"
        rows += f'''
        <tr><td style="padding:10px 20px;{border}">
          <table width="100%" cellpadding="0" cellspacing="0" role="presentation"><tr>
            <td style="font-family:{FONT};font-size:13px;font-weight:700;color:{BRIGHT};">{p.get("name","")} <span style="color:{MUTED};font-weight:600;font-size:11px;">{p.get("team","")} {p.get("position","")}</span></td>
            <td align="right" style="white-space:nowrap;padding-left:10px;font-family:{FONT};font-size:13px;font-weight:700;color:{ACCENT};">{pts_str} pts</td>
          </tr></table>
          <div style="font-family:{FONT};font-size:11px;color:{MUTED};margin-top:2px;">{gw_str} {games_word} this week</div>
        </td></tr>'''
    return rows


def _goalie_rows_html(goalies):
    rows = ""
    for i, g in enumerate(goalies):
        border = f"border-bottom:1px solid {BORDER};" if i < len(goalies) - 1 else ""
        gw = g.get("games_this_week")
        gw_str = str(gw) if gw is not None else "—"
        games_word = "game" if gw == 1 else "games"
        rows += f'''
        <tr><td style="padding:10px 20px;{border}">
          <table width="100%" cellpadding="0" cellspacing="0" role="presentation"><tr>
            <td style="font-family:{FONT};font-size:13px;font-weight:700;color:{BRIGHT};">{g.get("name","")} <span style="color:{MUTED};font-weight:600;font-size:11px;">{g.get("team","")}</span></td>
            <td align="right" style="white-space:nowrap;padding-left:10px;font-family:{FONT};font-size:12px;color:{MUTED};">{gw_str} team {games_word} this week</td>
          </tr></table>
        </td></tr>'''
    return rows


def build_my_team_email_html(day_label, skaters, goalies, summary):
    sections = ""
    if skaters:
        sections += f'''
        <tr><td style="padding:14px 20px 4px;font-family:{FONT};font-size:10px;font-weight:700;letter-spacing:1.5px;color:{MUTED};text-transform:uppercase;">Skaters</td></tr>
        {_skater_rows_html(skaters)}'''
    if goalies:
        sections += f'''
        <tr><td style="padding:14px 20px 4px;font-family:{FONT};font-size:10px;font-weight:700;letter-spacing:1.5px;color:{MUTED};text-transform:uppercase;">Goalies</td></tr>
        {_goalie_rows_html(goalies)}'''

    roster_block = f'''
        <tr><td style="padding:0 20px 16px;">
          <p style="font-family:{FONT};font-size:11px;font-weight:700;letter-spacing:2px;color:{BRIGHT};text-transform:uppercase;margin:0 0 10px;">Your Roster · {day_label}</p>
          <table width="100%" cellpadding="0" cellspacing="0" role="presentation" bgcolor="{CARD}" style="background:{CARD};border:1px solid {BORDER};border-radius:12px;">
            {sections}
          </table>
        </td></tr>''' if sections else ""

    summary_block = f'''
        <tr><td style="padding:0 20px 16px;">
          <p style="font-family:{FONT};font-size:11px;font-weight:700;letter-spacing:2px;color:{BRIGHT};text-transform:uppercase;margin:0 0 10px;">Weekly Summary</p>
          <table width="100%" cellpadding="0" cellspacing="0" role="presentation" bgcolor="{CARD}" style="background:{CARD};border:1px solid {BORDER};border-radius:12px;">
            <tr><td style="padding:10px 20px;border-bottom:1px solid {BORDER};">
              <table width="100%" cellpadding="0" cellspacing="0" role="presentation"><tr>
                <td style="font-family:{FONT};font-size:13px;color:{TEXT};">Projected points this week</td>
                <td align="right" style="font-family:{FONT};font-size:13px;font-weight:700;color:{GREEN};">{summary["total_projected"]:.2f}</td>
              </tr></table>
            </td></tr>
            <tr><td style="padding:10px 20px;border-bottom:1px solid {BORDER};">
              <table width="100%" cellpadding="0" cellspacing="0" role="presentation"><tr>
                <td style="font-family:{FONT};font-size:13px;color:{TEXT};">Games this week (skaters)</td>
                <td align="right" style="font-family:{FONT};font-size:13px;font-weight:700;color:{BRIGHT};">{summary["total_games"]}</td>
              </tr></table>
            </td></tr>
            <tr><td style="padding:10px 20px;">
              <table width="100%" cellpadding="0" cellspacing="0" role="presentation"><tr>
                <td style="font-family:{FONT};font-size:13px;color:{TEXT};">4-game / 2-game weeks</td>
                <td align="right" style="font-family:{FONT};font-size:13px;font-weight:700;color:{GOLD};">{summary["four_game"]} / {summary["two_game"]}</td>
              </tr></table>
            </td></tr>
          </table>
        </td></tr>'''

    body_html = roster_block + summary_block
    return _email_html_shell(
        "Your Roster This Week", day_label, body_html,
        home_url="https://grindline.ca/fantasy.html",
        footer_tagline="Manage your roster and the Trade Calculator",
    )


def build_my_team_email_text(day_label, skaters, goalies, summary):
    lines = [f"Grind Line — Your Roster This Week — {day_label}", "=" * 50, ""]
    if skaters:
        lines.append("SKATERS:")
        for p in skaters:
            proj = p.get("projection") or {}
            pts = proj.get("projected_points")
            gw = proj.get("games_this_week")
            pts_str = f"{pts:.2f}" if pts is not None else "—"
            gw_str = gw if gw is not None else "—"
            lines.append(f"  {p.get('name','')} ({p.get('team','')} {p.get('position','')}) — {pts_str} pts, {gw_str} games this week")
        lines.append("")
    if goalies:
        lines.append("GOALIES:")
        for g in goalies:
            gw = g.get("games_this_week")
            gw_str = gw if gw is not None else "—"
            lines.append(f"  {g.get('name','')} ({g.get('team','')}) — {gw_str} team games this week")
        lines.append("")
    lines.append("WEEKLY SUMMARY:")
    lines.append(f"  Projected points this week: {summary['total_projected']:.2f}")
    lines.append(f"  Games this week (skaters): {summary['total_games']}")
    lines.append(f"  4-game / 2-game weeks: {summary['four_game']} / {summary['two_game']}")
    lines += ["", "grindline.ca/fantasy.html", "Reply to this email to unsubscribe."]
    return "\n".join(lines)


# ── SEND VIA BREVO (transactional, per-recipient) ─────────────────────────────
def send_transactional_email(to_email, subject, html_content, text_content):
    """Unlike daily_email.py's send_email() (one call, everyone bcc'd on
    a static list), this is one real Brevo call per recipient with a
    single real "to" address - the actual requirement for a personalized
    send, not an optimization."""
    url = "https://api.brevo.com/v3/smtp/email"
    headers = {"api-key": BREVO_API_KEY, "Content-Type": "application/json"}
    payload = {
        "sender": {"name": FROM_NAME, "email": FROM_EMAIL},
        "to": [{"email": to_email}],
        "subject": subject,
        "htmlContent": html_content,
        "textContent": text_content,
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"  send error for {to_email}: {e}")
        if hasattr(e, "response") and e.response is not None:
            print(f"    response: {e.response.text}")
        return False


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        print("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set - aborting")
        return
    if not BREVO_API_KEY:
        print("BREVO_API_KEY not set - aborting")
        return

    day_label = get_day_label()
    print(f"\n{'='*50}\nGrind Line — My Team Email — {day_label}\n{'='*50}\n")

    print("Fetching roster_players...")
    rosters_by_user = fetch_all_roster_rows()
    print(f"  {len(rosters_by_user)} user(s) with at least one roster row")

    print("Fetching opted-in profiles...")
    opted_in = fetch_opted_in_user_ids()
    print(f"  {len(opted_in)} user(s) with email_opt_in = true")

    print("Fetching user emails...")
    emails_by_user = fetch_user_emails()
    print(f"  {len(emails_by_user)} user(s) with a resolvable email")

    print("Fetching player_hub.json...")
    hub = fetch_player_hub()
    if not hub:
        print("player_hub.json unavailable - aborting, nothing to join rosters against")
        return
    players_section = hub.get("players", {})
    goalies_section = hub.get("goalies", {})

    recipients = [uid for uid in rosters_by_user if uid in opted_in and uid in emails_by_user]
    print(f"\n{len(recipients)} recipient(s) qualify (roster + opted in + resolvable email)")

    sent = skipped_no_match = failed = 0
    for uid in recipients:
        to_email = emails_by_user[uid]
        try:
            roster = rosters_by_user[uid]
            skaters = resolve_roster(roster["skaters"], players_section)
            goalies = resolve_roster(roster["goalies"], goalies_section)
            if not skaters and not goalies:
                print(f"  {to_email}: no roster players resolved against player_hub.json - skipping")
                skipped_no_match += 1
                continue

            summary = compute_weekly_summary(skaters)
            subject = f"Your Grind Line roster — {day_label}"
            html_content = build_my_team_email_html(day_label, skaters, goalies, summary)
            text_content = build_my_team_email_text(day_label, skaters, goalies, summary)

            if send_transactional_email(to_email, subject, html_content, text_content):
                print(f"  {to_email}: sent ({len(skaters)} skaters, {len(goalies)} goalies)")
                sent += 1
            else:
                failed += 1
        except Exception as e:
            print(f"  {to_email}: unexpected error, skipping this user: {e}")
            failed += 1
        time.sleep(SEND_DELAY_SECONDS)

    print(f"\n{'='*50}")
    print(f"Sent {sent}, skipped (no matching players) {skipped_no_match}, failed {failed}")


if __name__ == "__main__":
    main()
