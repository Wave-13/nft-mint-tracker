"""NFT Mint Tracker — Telegram-first bot (full version, v1).

A trader opens the bot on Telegram, adds the mints that interest
THEM, and gets beeped 1 day before, 1 hour before, and at launch.

Why Telegram-first, and why each user's mints are PRIVATE:
  The demo web page could not save what a visitor typed, and letting
  anyone add to one shared public list would let fake mints on.
  Inside Telegram each user's list is theirs alone, so self-add needs
  no gatekeeper — a fake mint a user adds only ever beeps them.
  The shared, curated list stays separate (see VERIFIED_MINTS below)
  and only ever holds mints big wave / Vesper verified first.

Commands (in Telegram):
  /start    — welcome + what the bot does
  /add      — add a mint, step by step (name, date/time, price, chain, link)
  /list     — your mints, with countdowns
  /delete   — remove one of your mints
  /upcoming — the verified mints (Grant Yun, Quacked Ops) — /track to follow one
  /track <grant-yun|quacked-ops> — add a verified mint to your list
  /help     — this list again

Reminders: 1 day before, 1 hour before, at launch. Each fires once.
Times the user types are read as WAT (Africa/Lagos, UTC+1) unless
they say otherwise, and stored as UTC.

Hosting (agreed 2026-11-18):
  Test on Railway only (trial: 30 days / $5 — NOT permanent).
  Permanent home: Oracle Always Free VM (same plan as the Wave bot).
  A bot that RECEIVES messages needs a process that is always on —
  GitHub Actions cannot do this, it only runs on a schedule.

Secrets: TELEGRAM_BOT_TOKEN comes from the environment only.
  Never written to a file, never printed, never hard-coded.
  The token is created in BotFather by big wave and pasted once
  into Railway's env vars (same pattern as Wave Radar's token).

Stdlib only — no pip installs, so Railway / Oracle need nothing
beyond `python3 bot.py`.
"""

import hashlib
import hmac
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl

WAT = timezone(timedelta(hours=1))  # Africa/Lagos, UTC+1, no DST

STATE_PATH = os.environ.get("MINTBOT_STATE", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "state", "mints.json"))
POLL_SECONDS = int(os.environ.get("MINTBOT_POLL_SECONDS", "2"))
CHECK_SECONDS = int(os.environ.get("MINTBOT_CHECK_SECONDS", "30"))
DRY_RUN = os.environ.get("MINTBOT_DRY_RUN", "") == "1"

# Verified mints only — the same two as the demo page (2026-11-18).
# Nothing else is listed here. Add a new one only after verifying it.
VERIFIED_MINTS = [
    {
        "key": "grant-yun",
        "name": "Grant Yun — Sections",
        "mint_time_utc": "2026-11-18T18:00:00+00:00",  # 19:00 WAT
        "price": "0.05 ETH",
        "chain": "Ethereum",
        "link": "https://opensea.io/",
        "note": "1,300 onchain 1/1 sections. Public mint 0.05 ETH.",
        "has_time": True,
    },
    {
        "key": "quacked-ops",
        "name": "Quacked Ops",
        "mint_time_utc": "2026-12-01T18:00:00+00:00",  # Dec 1, time TBC — 19:00 WAT placeholder, see README
        "price": "Allowlist 0.02 ETH / Public 0.03 ETH",
        "chain": "Base",
        "link": "",
        "note": "115 pieces. Exact launch time + mint link still pending.",
        "has_time": False,  # no announced time yet: watchlist-only, no beeps
    },
]

ADD_STEPS = ["name", "time", "price", "chain", "link"]
ADD_PROMPTS = {
    "name": "What is the mint called? (e.g. Grant Yun — Sections)",
    "time": "When does it mint? Send date + time in WAT, e.g. `2026-12-01 19:00` — or `Dec 1 19:00`, `today 19:00`, `tomorrow 19:00`.",
    "price": "What is the mint price? (e.g. 0.05 ETH — or `free` / `unknown`)",
    "chain": "Which chain? (e.g. Ethereum, Base, Solana)",
    "link": "Mint link? Paste the URL — or send `none` if there is none yet.",
}

MINI_APP_URL = "https://wave-13.github.io/mint-tracker-mini-app/"

START_TEXT = (
    "Welcome to Mint Tracker \U0001f44b\n\n"
    "Your mint calendar lives in the app, not here.\n\n"
    "Tap \u201cOpen Mint Tracker\u201d below to see it, track a mint, "
    "and get beeped before it drops \u2014 1 day before, 1 hour before, "
    "and at launch.\n\n"
    "Everything happens in the app."
)

HELP_TEXT = (
    "NFT Mint Tracker — never miss a mint again.\n\n"
    "Add the mints YOU care about and I beep you 1 day before, "
    "1 hour before, and at launch, with price, chain and link.\n\n"
    "Tap \"Open Mint Tracker\" below to open the calendar — "
    "Track in there sends the mint straight to me, and I save it "
    "and beep you. (Opening the calendar from a plain link will not "
    "send it to me — use the button below.)\n\n"
    "/add — add a mint, step by step\n"
    "/list — your mints, with countdowns\n"
    "/delete — remove one of your mints\n"
    "/upcoming — verified mints (Grant Yun, Quacked Ops)\n"
    "/track grant-yun — follow a verified mint\n"
    "/stats — how many people use Mint Tracker\n"
    "/help — this list again\n\n"
    "Your list is yours alone — nobody else sees what you add."
)


# ---------------------------------------------------------------- state
# One lock guards every load -> modify -> save sequence. The bridge
# thread and the polling loop both touch the same file; without it the
# last writer wins and the other thread's mint (or beep marks) is lost.
STATE_LOCK = threading.Lock()

# Liveness for /health: last time each half of the service proved alive.
HEALTH = {"started": time.time(), "last_getupdates_ok": 0.0,
          "last_reminder_check": 0.0, "bridge_ok": False, "last_error": ""}

# A corrupt state file used to be treated as "no state" and the next
# save overwrote it with empty data — silent total loss. Now a corrupt
# file is quarantined and the last-good backup is used instead.
def load_state():
    for path in (STATE_PATH, STATE_PATH + ".bak"):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and "users" in data:
                data.setdefault("visitors", {})
                data.setdefault("app_opens", 0)
                return data
        except FileNotFoundError:
            continue
        except ValueError:
            if path == STATE_PATH:
                try:
                    bad = "%s.corrupt-%d" % (STATE_PATH, int(time.time()))
                    os.replace(STATE_PATH, bad)
                    print("WARNING: corrupt state quarantined to %s" % bad,
                          file=sys.stderr)
                except Exception as exc:
                    print("WARNING: could not quarantine corrupt state: %s" % exc,
                          file=sys.stderr)
            continue
    return {"users": {}, "visitors": {}, "app_opens": 0}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    # Keep a last-good backup: a corrupt main file falls back to this.
    try:
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH, "rb") as src, \
                    open(STATE_PATH + ".bak.tmp", "wb") as dst:
                dst.write(src.read())
            os.replace(STATE_PATH + ".bak.tmp", STATE_PATH + ".bak")
    except Exception as exc:
        print("WARNING: state backup failed: %s" % exc, file=sys.stderr)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, STATE_PATH)


def user_mints(state, chat_id):
    return state["users"].setdefault(str(chat_id), {"mints": []})["mints"]


def next_id(mints):
    return max([m.get("id", 0) for m in mints], default=0) + 1


# ---------------------------------------------------------------- time parsing
def parse_mint_time(text, now=None):
    """Parse what a user typed as a WAT date/time. Returns aware UTC datetime or None.

    Accepted: '2026-12-01 19:00', 'Dec 1 19:00', 'Dec 1 2026 19:00',
    'today 19:00', 'tomorrow 19:00'. A bare date means 19:00 WAT.
    """
    now = now or datetime.now(WAT)
    s = (text or "").strip()
    m = re.match(r"^(today|tomorrow)\s+(\d{1,2}):(\d{2})$", s, re.I)
    if m:
        day = now.date() + (timedelta(days=1) if m.group(1).lower() == "tomorrow" else timedelta(0))
        return datetime(day.year, day.month, day.day,
                        int(m.group(2)), int(m.group(3)), tzinfo=WAT).astimezone(timezone.utc)
    for fmt in ("%Y-%m-%d %H:%M", "%d %b %Y %H:%M", "%b %d %Y %H:%M",
                "%d %b %H:%M", "%b %d %H:%M",
                "%Y-%m-%d", "%d %b %Y", "%b %d %Y", "%d %b", "%b %d"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.year == 1900:  # no year given — take this year, roll to next if past
                dt = dt.replace(year=now.year)
                if dt.replace(tzinfo=WAT) < now - timedelta(hours=1):
                    dt = dt.replace(year=now.year + 1)
            if "%H" not in fmt:
                dt = dt.replace(hour=19, minute=0)
            return dt.replace(tzinfo=WAT).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def fmt_wat(iso):
    dt = datetime.fromisoformat(iso).astimezone(WAT)
    return dt.strftime("%a %d %b %Y, %H:%M WAT")


def countdown(iso, now=None):
    now = now or datetime.now(timezone.utc)
    delta = datetime.fromisoformat(iso) - now
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "minting now / passed"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return "%dd %dh %dm left" % (days, hours, mins)
    if hours:
        return "%dh %dm left" % (hours, mins)
    return "%dm left" % mins


# ---------------------------------------------------------------- Telegram Mini App auth
# The bridge used to trust whatever chat_id the client posted — anyone
# could add mints to anyone's list and make the bot spam strangers.
# Now every bridge call must carry the mini app's raw initData string,
# and the chat_id comes from the verified payload, never from the client.
def verify_init_data(init_data, token):
    """Validate a Telegram Mini App initData string (HMAC-SHA-256).
    Returns the Telegram user id if valid and fresh, else None."""
    if not init_data or not token:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None
    received = pairs.pop("hash", None)
    if not received:
        return None
    check = "\n".join("%s=%s" % (k, v) for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        return None
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        return None
    if auth_date <= 0 or time.time() - auth_date > 86400:
        return None  # missing or stale: replay
    try:
        user = json.loads(pairs.get("user", "{}"))
    except Exception:
        return None
    uid = user.get("id")
    if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
        return None
    return uid


# Last Telegram send error, for the reminder loop to act on:
# 403 = user blocked the bot (stop retrying them); 429 = slow down.
LAST_SEND_ERROR = {"code": None, "description": "", "retry_after": 0}


# ---------------------------------------------------------------- Telegram API
def api(method, payload=None, token=None):
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    url = "https://api.telegram.org/bot%s/%s" % (token, method)
    data = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send_message(chat_id, text, token=None, reply_markup=None):
    if DRY_RUN:
        print("[dry-run] -> %s:\n%s\n" % (chat_id, text))
        return True
    LAST_SEND_ERROR.update({"code": None, "description": "", "retry_after": 0})
    try:
        payload = {"chat_id": chat_id, "text": text,
                   "disable_web_page_preview": True}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        body = api("sendMessage", payload, token)
    except Exception as exc:
        print("ERROR sending to %s: %s" % (chat_id, exc), file=sys.stderr)
        return False
    if not body.get("ok"):
        code = body.get("error_code")
        desc = body.get("description", "")
        params = body.get("parameters") or {}
        retry_after = int(params.get("retry_after") or 0)
        LAST_SEND_ERROR.update({"code": code, "description": desc,
                                "retry_after": retry_after})
        print("ERROR Telegram API %s: %s" % (code, desc), file=sys.stderr)
        if code == 429 and retry_after:
            # Telegram tells us exactly how long to wait. Retrying
            # immediately into a 429 is how a bot earns a longer ban.
            time.sleep(min(retry_after, 60))
        return False
    return True


def upcoming_reminders(mint_time, now=None):
    """Labels of the beeps still in the future for this mint.

    A mint 1 minute away gets only the launch beep; a mint 12 hours
    away gets the 1-hour and launch beeps; a mint 2 days away gets
    all three. The Tracking reply must only promise these, never
    a 1-day / 1-hour beep whose window has already passed.
    """
    now = now or datetime.now(timezone.utc)
    if isinstance(mint_time, str):
        mint_time = datetime.fromisoformat(mint_time)
    labels = []
    for key, offset, label in REMINDER_OFFSETS:
        if key == "launch":
            if mint_time > now:
                labels.append("at launch")
        elif mint_time - offset > now:
            labels.append(label)
    return labels


def beep_promise(mint_time, now=None, has_time=True):
    if not has_time:
        return ("Watching it for now — no beeps until the mint time is "
                "announced.")
    labels = upcoming_reminders(mint_time, now)
    if not labels:
        return "Minting now."
    if len(labels) == 1:
        return "I will beep you %s." % labels[0]
    return "I will beep you %s, and %s." % (", ".join(labels[:-1]), labels[-1])


# ---------------------------------------------------------------- reminders
REMINDER_OFFSETS = [("day", timedelta(days=1), "1 day before"),
                    ("hour", timedelta(hours=1), "1 hour before"),
                    ("launch", timedelta(0), "MINTING NOW")]


def due_reminders(mint, now=None):
    """Which reminder is due for this mint right now (not yet sent).

    Returns at most one — the latest due (launch beats hour beats day)
    — so a bot that was offline does not triple-beep when it returns,
    and a mint added late still gets the next beep, not a stale one.
    A reminder only fires inside its own window: the 5 minutes after
    its due time — except the launch beep, which gets a 30-minute late
    window so a restart or outage across launch does not silently eat
    the beep that matters most (the caller labels it late). A 1-hour
    beep whose due time passed an hour ago (e.g. a mint added 1 minute
    before launch) is skipped, never fired late — the Tracking reply
    promises only future beeps, and the beeps themselves must match
    that promise. A mint with no announced time (has_time False) never
    beeps: its midnight placeholder is not a launch.
    """
    now = now or datetime.now(timezone.utc)
    mint_time = datetime.fromisoformat(mint["mint_time_utc"])
    if not mint.get("has_time", True):
        return []
    if now >= mint_time + timedelta(minutes=30):
        return []
    sent = mint.setdefault("sent", [])
    for key, offset, _label in reversed(REMINDER_OFFSETS):
        if key in sent:
            continue
        if key == "launch":
            if mint_time <= now:
                return [key]
        elif mint_time - offset <= now < mint_time - offset + timedelta(minutes=5):
            return [key]
    return []


def reminder_text(mint, label):
    lines = ["⏰ %s — %s" % (label, mint["name"]),
             "%s · %s" % (fmt_wat(mint["mint_time_utc"]), mint.get("price", "?")),
             "Chain: %s" % mint.get("chain", "?")]
    if mint.get("link"):
        lines.append("Mint: %s" % mint["link"])
    if mint.get("note"):
        lines.append(mint["note"])
    return "\n".join(lines)


def check_reminders(state, now=None):
    """Send every due reminder once. Returns count sent."""
    now = now or datetime.now(timezone.utc)
    sent_count = 0
    changed = False
    blocked = set(str(c) for c in (state.get("blocked") or []))
    for chat_id, udata in state["users"].items():
        if str(chat_id) in blocked:
            continue  # user blocked the bot: never retry them
        for mint in udata.get("mints", []):
            try:
                mint_time = datetime.fromisoformat(mint["mint_time_utc"])
            except Exception:
                continue  # malformed stored mint: skip it, never crash
            # Missed entirely (bot was down past the late window): say so
            # once in the log instead of silently skipping it.
            if mint.get("has_time", True) \
                    and "launch" not in mint.get("sent", []) \
                    and now >= mint_time + timedelta(minutes=30) \
                    and not mint.get("missed_logged"):
                mint["missed_logged"] = True
                changed = True
                print("MISSED BEEP: %s for chat %s (launch %s, bot was down)"
                      % (mint.get("name"), chat_id, mint["mint_time_utc"]),
                      file=sys.stderr)
            for key in due_reminders(mint, now):
                label = dict((k, l) for k, _o, l in REMINDER_OFFSETS)[key]
                if key == "launch" and now > mint_time + timedelta(minutes=5):
                    label = "MINTING NOW (late beep — the bot was down)"
                if send_message(chat_id, reminder_text(mint, label)):
                    mint.setdefault("sent", []).append(key)
                    sent_count += 1
                    changed = True
                elif LAST_SEND_ERROR.get("code") == 403:
                    # User blocked the bot. Stop retrying them entirely:
                    # mark this beep handled so it never retries, and
                    # remember the block so future beeps skip them too.
                    mint.setdefault("sent", []).append(key)
                    blocked.add(str(chat_id))
                    state["blocked"] = sorted(blocked)
                    changed = True
                    print("User %s blocked the bot — future beeps skipped"
                          % chat_id, file=sys.stderr)
    if changed:
        save_state(state)
    return sent_count


# ---------------------------------------------------------------- /add conversation
# In-memory only: an interrupted /add restarts cleanly. Saved mints
# live in state/mints.json, never in this dict.
conversations = {}


# Only the owner sees /stats. This is the user's own Telegram id.
OWNER_CHAT_ID = 5601504588


def stats_text(state):
    users = state.get("users") or {}
    tracking = [u for u in users.values() if u.get("mints")]
    total_mints = sum(len(u.get("mints", [])) for u in tracking)
    beeps = sum(len(m.get("sent", [])) for u in tracking for m in u.get("mints", []))
    lines = ["Mint Tracker — stats\n"]
    lines.append("People tracking: %d" % len(tracking))
    lines.append("Mints tracked: %d" % total_mints)
    lines.append("Beeps sent: %d" % beeps)
    for v in VERIFIED_MINTS:
        n = sum(1 for u in tracking
                if any(m.get("source_key") == v["key"] for m in u.get("mints", [])))
        lines.append("Tracking %s: %d" % (v["name"], n))
    visitors = state.get("visitors") or {}
    lines.append("App opens: %d" % int(state.get("app_opens") or 0))
    lines.append("Unique app visitors: %d" % len(visitors))
    return "\n".join(lines)


def handle_message(state, chat_id, text):
    """Returns reply text (or None if already handled/sent)."""
    text = (text or "").strip()
    conv = conversations.get(str(chat_id))
    # A stale draft (user walked away mid-/add) expires after 15 minutes.
    if conv is not None and time.time() - conv.get("ts", 0) > 900:
        conversations.pop(str(chat_id), None)
        conv = None
    # Any command abandons the open draft.
    if conv is not None and text.startswith("/"):
        conversations.pop(str(chat_id), None)
        conv = None

    # Delete flow FIRST: "__delete__" is not an /add step, and the generic
    # block below does ADD_STEPS.index(step) — which used to crash the
    # whole bot on every /delete. Handle it here, before that block.
    if conv is not None and conv.get("step") == "__delete__" \
            and not text.startswith("/"):
        mints = user_mints(state, chat_id)
        try:
            want = int(text)
        except ValueError:
            return "Send just the number, e.g. 2 — or /cancel."
        before = len(mints)
        mints[:] = [m for m in mints if m["id"] != want]
        conversations.pop(str(chat_id), None)
        if len(mints) == before:
            return "No mint #%d on your list." % want
        save_state(state)
        return "Deleted mint #%d." % want

    if conv is not None and not text.startswith("/"):
        step = conv["step"]
        draft = conv["draft"]
        conv["ts"] = time.time()
        if step == "time":
            parsed = parse_mint_time(text)
            if parsed is None:
                return ("I could not read that date. Try `2026-12-01 19:00` "
                        "or `Dec 1 19:00` — date + time in WAT.")
            if parsed < datetime.now(timezone.utc):
                return "That time is already past. Send a future date + time."
            draft["mint_time_utc"] = parsed.isoformat()
        elif step == "link":
            draft["link"] = "" if text.lower() == "none" else text
        else:
            draft[step] = text
        idx = ADD_STEPS.index(step)
        if idx + 1 < len(ADD_STEPS):
            conv["step"] = ADD_STEPS[idx + 1]
            return ADD_PROMPTS[conv["step"]]
        # done — save it
        mints = user_mints(state, chat_id)
        draft["id"] = next_id(mints)
        draft["sent"] = []
        draft["has_time"] = True  # /add always has a real date + time
        draft.setdefault("price", "?")
        draft.setdefault("chain", "?")
        mints.append(draft)
        save_state(state)
        del conversations[str(chat_id)]
        return ("Saved ✅\n\n%s\n%s · %s\nChain: %s%s\n\n%s"
                % (draft["name"], fmt_wat(draft["mint_time_utc"]),
                   draft["price"], draft["chain"],
                   ("\nMint: " + draft["link"]) if draft.get("link") else "",
                   beep_promise(draft["mint_time_utc"])))

    if text == "/stats":
        if int(chat_id) != OWNER_CHAT_ID:
            return "Stats are private — only the owner can see them."
        return stats_text(state)
    if text == "/start":
        return START_TEXT
    if text == "/help":
        return HELP_TEXT
    if text == "/add":
        conversations[str(chat_id)] = {"step": "name", "draft": {},
                                       "ts": time.time()}
        return ADD_PROMPTS["name"] + "\n\n(Send /cancel to stop.)"
    if text == "/cancel":
        conversations.pop(str(chat_id), None)
        return "Cancelled — nothing saved. /add to start again."
    if text == "/list":
        mints = user_mints(state, chat_id)
        if not mints:
            return "You have no mints yet. /add one, or /upcoming for the verified list."
        lines = ["Your mints:\n"]
        for m in sorted(mints, key=lambda x: x["mint_time_utc"]):
            lines.append("#%d %s\n   %s — %s\n   %s · %s%s" % (
                m["id"], m["name"], fmt_wat(m["mint_time_utc"]),
                countdown(m["mint_time_utc"]), m.get("price", "?"),
                m.get("chain", "?"),
                (" — " + m["link"]) if m.get("link") else ""))
        return "\n".join(lines)
    if text == "/delete":
        mints = user_mints(state, chat_id)
        if not mints:
            return "Nothing to delete — your list is empty."
        conversations[str(chat_id)] = {"step": "__delete__", "draft": {},
                                       "ts": time.time()}
        lines = ["Send the number of the mint to delete:\n"]
        for m in mints:
            lines.append("#%d %s (%s)" % (m["id"], m["name"], fmt_wat(m["mint_time_utc"])))
        return "\n".join(lines)
    if text == "/upcoming":
        lines = ["Verified mints — checked by us, not user-added:\n"]
        for v in VERIFIED_MINTS:
            when = ("Time to be announced" if not v.get("has_time", True)
                    else "%s — %s" % (fmt_wat(v["mint_time_utc"]),
                                      countdown(v["mint_time_utc"])))
            lines.append("• %s\n  %s\n  %s · %s%s\n  Track it: /track %s" % (
                v["name"], when, v["price"], v["chain"],
                ("\n  " + v["link"]) if v["link"] else "", v["key"]))
        return "\n".join(lines)
    if text.startswith("/track"):
        parts = text.split()
        key = parts[1].lower() if len(parts) > 1 else ""
        match = [v for v in VERIFIED_MINTS if v["key"] == key]
        if not match:
            return "Usage: /track grant-yun  or  /track quacked-ops  (see /upcoming)"
        v = match[0]
        mints = user_mints(state, chat_id)
        if any(m.get("source_key") == key for m in mints):
            return "%s is already on your list." % v["name"]
        has_time = v.get("has_time", True)
        mints.append({"id": next_id(mints), "name": v["name"],
                     "mint_time_utc": v["mint_time_utc"], "price": v["price"],
                     "chain": v["chain"], "link": v["link"],
                     "note": v.get("note", ""), "source_key": key, "sent": [],
                     "has_time": has_time})
        save_state(state)
        if has_time:
            return ("Tracking %s ✅\n%s · %s\n%s"
                    % (v["name"], fmt_wat(v["mint_time_utc"]), v["price"],
                       beep_promise(v["mint_time_utc"])))
        return ("Watching %s 👀\n%s · %s\n%s"
                % (v["name"], "Time to be announced", v["price"],
                   beep_promise(v["mint_time_utc"], has_time=False)))
    if text.startswith("/"):
        return "Unknown command. /help lists what I can do."
    return ("Send /add to add a mint, /list to see yours, "
            "/upcoming for the verified mints, or /help.")


# ---------------------------------------------------------------- mini app bridge
# The mini app (GitHub Pages) has no back end of its own, so its Track /
# Add-a-mint buttons send the mint to the bot with
# Telegram.WebApp.sendData(). Telegram delivers that as a message with
# web_app_data, and the bot saves it exactly like /add or /track — so a
# tap in the mini app really turns the beeps on.
def handle_web_app_data(state, chat_id, data_str):
    """Save a mint sent from the mini app. Returns a dict:
    {"ok": bool, "already": bool, "reply": str}.
    ok is True for Tracking, Watching (no time yet) and already-tracked —
    the mint IS on the list in all three, so the app must show it saved.
    Only real errors (no name/date, unreadable date, past mint) are ok False.
    """
    def err(reply):
        return {"ok": False, "already": False, "reply": reply}
    try:
        data = json.loads(data_str or "{}")
    except Exception:
        return err("I could not read that from the mini app. Try again from the bot with /add.")
    mint = data.get("mint") or {}
    name = (mint.get("name") or "").strip()[:120]
    when = (mint.get("mint_time_utc") or "").strip()
    if not name or not when:
        return err("That mint had no name or date. Try adding it again from the mini app.")
    try:
        mint_time = datetime.fromisoformat(when.replace("Z", "+00:00"))
        if mint_time.tzinfo is None:
            mint_time = mint_time.replace(tzinfo=timezone.utc)
    except ValueError:
        return err("That mint had a date I could not read. Try adding it again from the mini app.")
    if mint_time < datetime.now(timezone.utc):
        return err("%s has already passed — add a future mint to get beeps." % name)
    # has_time False = date known, time not announced: watchlist-only, the
    # midnight placeholder must never schedule beeps.
    has_time = mint.get("has_time")
    if has_time is None:
        has_time = True  # older clients always sent a real time
    has_time = bool(has_time)
    mints = user_mints(state, chat_id)
    key = (mint.get("source_key") or "").strip().lower()[:80]
    if key and any(m.get("source_key") == key for m in mints):
        return {"ok": True, "already": True,
                "reply": "%s is already on your list." % name}
    if any(m.get("name") == name and m.get("mint_time_utc") == mint_time.isoformat()
           for m in mints):
        return {"ok": True, "already": True,
                "reply": "%s is already on your list." % name}
    # Time announced since? A watchlist-only mint with the same name gets
    # its real time now, and its beeps turn on from scratch.
    pending = next((m for m in mints
                    if m.get("name") == name and not m.get("has_time", True)), None)
    if pending is not None and has_time:
        pending["mint_time_utc"] = mint_time.isoformat()
        pending["has_time"] = True
        pending["sent"] = []
        pending["missed_logged"] = False
        pending["price"] = (mint.get("price") or pending.get("price", "?"))[:60]
        pending["link"] = (mint.get("link") or pending.get("link", ""))[:500]
        save_state(state)
        return {"ok": True, "already": False,
                "reply": ("Tracking %s ✅\n%s · %s\n%s"
                          % (name, fmt_wat(mint_time.isoformat()),
                             pending["price"],
                             beep_promise(mint_time.isoformat())))}
    mints.append({"id": next_id(mints), "name": name,
                 "mint_time_utc": mint_time.isoformat(),
                 "price": (mint.get("price") or "?")[:60],
                 "chain": (mint.get("chain") or "?")[:40],
                 "link": (mint.get("link") or "")[:500],
                 "note": (mint.get("note") or "")[:200],
                 "source_key": key, "sent": [], "has_time": has_time})
    save_state(state)
    if has_time:
        return {"ok": True, "already": False,
                "reply": ("Tracking %s ✅\n%s · %s\n%s"
                          % (name, fmt_wat(mint_time.isoformat()),
                             mints[-1]["price"],
                             beep_promise(mint_time.isoformat())))}
    return {"ok": True, "already": False,
            "reply": ("Watching %s 👀\n%s · %s\n%s"
                      % (name, "Time to be announced",
                         mints[-1]["price"],
                         beep_promise(mint_time.isoformat(), has_time=False)))}


# ---------------------------------------------------------------- polling loop
# ---------------------------------------------------------------- direct HTTP bridge
# The mini app (GitHub Pages) sends Track/Add straight to the bot over
# HTTP, instead of only Telegram's sendData — sendData silently drops
# the message on link/menu launches, which is why mints added in the
# app never landed on the bot's /list. The app POSTs
# {"init_data": <Telegram's signed initData string>, "mint": {...}}
# to /track; the bot verifies initData (HMAC, bot token), takes the
# user id from the verified payload — never from the client — saves
# the mint and replies "Tracking" in the bot chat, so /list shows it.
# CORS is restricted to the Pages origin. Endpoints:
#   POST /track   — save a mint (auth required)
#   POST /untrack — remove a mint (auth required)
#   POST /status  — the user's tracked mints, for the app to reconcile
#                   its local "Saved to the bot" flags against (auth required)
#   POST /ping    — one app-open count per visitor per day (stats only)
#   GET  /        — liveness; GET /health — poller + bridge status
BOT_HTTP_URL = "https://nft-mint-tracker-production.up.railway.app"
BRIDGE_ORIGIN = "https://wave-13.github.io"
MAX_BODY_BYTES = 65536


def _bridge_auth(data):
    """Verify initData, return the Telegram user id or None."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    return verify_init_data(data.get("init_data") or "", token)


def bridge_track(state, chat_id, mint):
    """Pure helper: save a mint from the mini app. Returns a JSON dict."""
    return handle_web_app_data(state, chat_id, json.dumps({"mint": mint}))


def bridge_untrack(state, chat_id, data):
    """Pure helper: remove a mint the app untracked. Returns a JSON dict."""
    key = (data.get("source_key") or "").strip().lower()
    name = (data.get("name") or "").strip()
    when = (data.get("mint_time_utc") or "").strip()
    mints = user_mints(state, chat_id)
    before = len(mints)
    if key:
        mints[:] = [m for m in mints if m.get("source_key") != key]
    elif name and when:
        mints[:] = [m for m in mints
                    if not (m.get("name") == name
                            and m.get("mint_time_utc") == when)]
    elif name:
        mints[:] = [m for m in mints if m.get("name") != name]
    else:
        return {"ok": False, "reply": "Nothing to untrack."}
    if len(mints) == before:
        return {"ok": True, "reply": "That mint was not on your list."}
    save_state(state)
    return {"ok": True, "reply": "Stopped tracking %s." % (name or key)}


def bridge_status(state, chat_id):
    """Pure helper: what the bot actually has for this user."""
    mints = user_mints(state, chat_id)
    return {"ok": True, "mints": [
        {"id": m.get("id"), "name": m.get("name"),
         "mint_time_utc": m.get("mint_time_utc"),
         "source_key": m.get("source_key") or "",
         "has_time": m.get("has_time", True)}
        for m in mints]}


class _BridgeHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send_json(self, code, obj, origin=None):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _origin(self):
        origin = (self.headers.get("Origin") or "").strip()
        return origin if origin == BRIDGE_ORIGIN else None

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return None, "bad content-length"
        if length <= 0 or length > MAX_BODY_BYTES:
            return None, "body too large"
        try:
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8") or "{}"), None
        except Exception:
            return None, "bad json"

    def do_OPTIONS(self):
        self._send_json(200, {"ok": True}, origin=self._origin())

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            self._send_json(200, {
                "ok": True, "service": "mint-tracker-bot",
                "uptime_s": int(time.time() - HEALTH["started"]),
                "bridge_ok": HEALTH["bridge_ok"],
                "last_getupdates_ok_s_ago": (
                    int(time.time() - HEALTH["last_getupdates_ok"])
                    if HEALTH["last_getupdates_ok"] else None),
                "last_reminder_check_s_ago": (
                    int(time.time() - HEALTH["last_reminder_check"])
                    if HEALTH["last_reminder_check"] else None),
                "last_error": HEALTH["last_error"] or None,
            }, origin=self._origin())
            return
        self._send_json(200, {"ok": True, "service": "mint-tracker-bot"},
                        origin=self._origin())

    def do_POST(self):
        path = self.path.rstrip("/")
        origin = self._origin()
        if path == "/ping":
            data, err = self._read_json()
            if err:
                self._send_json(400, {"ok": False, "error": err}, origin=origin)
                return
            vid = str(data.get("visitor_id") or "").strip()[:64]
            if not vid:
                self._send_json(400, {"ok": False, "error": "visitor_id required"},
                                origin=origin)
                return
            today = time.strftime("%Y-%m-%d", time.gmtime())
            with STATE_LOCK:
                state = load_state()
                state.setdefault("visitors", {})
                state.setdefault("app_opens", 0)
                # One open per visitor per day: a retry storm or a spammer
                # must not inflate the stats unboundedly.
                if state["visitors"].get(vid) != today:
                    state["visitors"][vid] = today
                    state["app_opens"] = int(state.get("app_opens") or 0) + 1
                    save_state(state)
            self._send_json(200, {"ok": True}, origin=origin)
            return
        if path not in ("/track", "/untrack", "/status"):
            self._send_json(404, {"ok": False, "error": "not found"}, origin=origin)
            return
        data, err = self._read_json()
        if err:
            self._send_json(400, {"ok": False, "error": err}, origin=origin)
            return
        chat_id = _bridge_auth(data)
        if chat_id is None:
            self._send_json(401, {"ok": False, "error": "auth"}, origin=origin)
            return
        if path == "/track":
            mint = data.get("mint") or {}
            if not mint.get("name"):
                self._send_json(400, {"ok": False, "error": "mint.name required"},
                                origin=origin)
                return
            with STATE_LOCK:
                state = load_state()
                result = bridge_track(state, chat_id, mint)
            if result.get("reply"):
                try:
                    send_message(chat_id, result["reply"])
                except Exception as exc:
                    print("ERROR sending Tracking reply: %s" % exc, file=sys.stderr)
            self._send_json(200 if result["ok"] else 400, result, origin=origin)
            return
        if path == "/untrack":
            with STATE_LOCK:
                state = load_state()
                result = bridge_untrack(state, chat_id, data)
            self._send_json(200 if result["ok"] else 400, result, origin=origin)
            return
        # /status
        with STATE_LOCK:
            state = load_state()
            result = bridge_status(state, chat_id)
        self._send_json(200, result, origin=origin)


def start_bridge_server():
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), _BridgeHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    HEALTH["bridge_ok"] = True
    print("Mint Tracker bridge listening on port %d" % port)
    return server


def _load_offset():
    # The update offset used to live only in memory: after any restart
    # the bot re-fetched unacknowledged updates and re-sent replies.
    try:
        with open(STATE_PATH + ".offset", "r", encoding="utf-8") as fh:
            return int(fh.read().strip() or 0)
    except Exception:
        return 0


def _save_offset(offset):
    try:
        os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
        tmp = STATE_PATH + ".offset.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(str(offset))
        os.replace(tmp, STATE_PATH + ".offset")
    except Exception as exc:
        print("WARNING: offset save failed: %s" % exc, file=sys.stderr)


def run_loop():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("ERROR: set TELEGRAM_BOT_TOKEN first (BotFather token).",
              file=sys.stderr)
        return 2
    state = load_state()
    offset = _load_offset()
    last_check = 0.0
    fail_streak = 0
    print("Mint Tracker bot running. State: %s" % STATE_PATH)
    try:
        start_bridge_server()
    except Exception as exc:
        HEALTH["last_error"] = "bridge: %s" % exc
        print("bridge server failed to start: %s" % exc, file=sys.stderr)
    while True:
        try:
            body = api("getUpdates", {"offset": offset, "timeout": 20}, token)
        except Exception as exc:
            # Persistent failure (bad token, Telegram outage): back off
            # instead of printing an error every 2 seconds forever.
            fail_streak += 1
            wait = min(2 * (2 ** min(fail_streak, 5)), 60)
            HEALTH["last_error"] = "getUpdates: %s" % exc
            print("getUpdates failed: %s — retrying in %ss" % (exc, wait),
                  file=sys.stderr)
            time.sleep(wait)
            continue
        fail_streak = 0
        HEALTH["last_getupdates_ok"] = time.time()
        HEALTH["last_error"] = ""
        for update in body.get("result") or []:
            try:
                offset = max(offset, update.get("update_id", 0) + 1)
                _save_offset(offset)
                # Reload from disk on every update: the bridge server saves
                # mints to the same file, and a stale in-memory copy would
                # make /list miss mints the bridge just saved (and could
                # overwrite them on the next save).
                with STATE_LOCK:
                    state = load_state()
                    msg = update.get("message") or {}
                    chat = msg.get("chat") or {}
                    if "id" not in chat:
                        continue
                    wad = msg.get("web_app_data") or {}
                    if wad.get("data"):
                        res = handle_web_app_data(state, chat["id"], wad["data"])
                        reply = res.get("reply")
                        if reply:
                            send_message(chat["id"], reply, token)
                        continue
                    text = msg.get("text") or ""
                    if not text:
                        continue
                    reply = handle_message(state, chat["id"], text)
                if reply:
                    markup = None
                    if text.strip() in ("/start", "/help"):
                        # Keyboard button is the only launch that lets the mini
                        # app send data back to the bot (Telegram's sendData
                        # rule) — a plain link / menu launch cannot send it.
                        markup = {"keyboard": [[{"text": "Open Mint Tracker",
                                                  "web_app": {"url": MINI_APP_URL}}]],
                                  "resize_keyboard": True}
                    send_message(chat["id"], reply, token, reply_markup=markup)
            except Exception as exc:
                # One bad update must never kill the service.
                HEALTH["last_error"] = "update: %s" % exc
                print("update failed, skipping: %s" % exc, file=sys.stderr)
                continue
        if time.time() - last_check >= CHECK_SECONDS:
            last_check = time.time()
            HEALTH["last_reminder_check"] = time.time()
            try:
                with STATE_LOCK:
                    state = load_state()
                    n = check_reminders(state)
                if n:
                    print("sent %d reminder(s)" % n)
            except Exception as exc:
                print("reminder check failed: %s" % exc, file=sys.stderr)
        time.sleep(0.1)


def selftest():
    """No-network check: parsing, countdowns, /add flow, reminder due logic."""
    global STATE_PATH
    import tempfile
    STATE_PATH = tempfile.mktemp(suffix=".json")  # keep test data off the real state file
    from datetime import datetime as dt
    now = dt(2026, 11, 18, 9, 0, tzinfo=timezone.utc)  # Wed 18 Nov, 10:00 WAT
    assert parse_mint_time("2026-12-01 19:00", now).isoformat() == \
        "2026-12-01T18:00:00+00:00", parse_mint_time("2026-12-01 19:00", now)
    assert parse_mint_time("Dec 1 19:00", now).isoformat() == \
        "2026-12-01T18:00:00+00:00"
    assert parse_mint_time("today 19:00", now).isoformat() == \
        "2026-11-18T18:00:00+00:00"
    assert parse_mint_time("tomorrow 19:00", now).isoformat() == \
        "2026-11-19T18:00:00+00:00"
    assert parse_mint_time("not a date", now) is None
    assert countdown("2026-11-18T18:00:00+00:00", now) == "9h 0m left"
    # Grant Yun tonight: each beep fires only inside its own 5-minute
    # window after its due time — a beep whose window already passed
    # (e.g. the day beep at 10:00, 16h after it was due) is skipped.
    gy = {"id": 1, "name": "Grant Yun — Sections",
          "mint_time_utc": "2026-11-18T18:00:00+00:00",
          "price": "0.05 ETH", "chain": "Ethereum", "link": "", "sent": []}
    assert due_reminders(gy, now) == [], due_reminders(gy, now)
    assert due_reminders(gy, dt(2026, 11, 17, 18, 2, tzinfo=timezone.utc)) == ["day"]
    assert due_reminders(gy, dt(2026, 11, 18, 17, 2, tzinfo=timezone.utc)) == ["hour"]
    assert due_reminders(gy, dt(2026, 11, 18, 18, 2, tzinfo=timezone.utc)) == ["launch"]
    # Launch beep gets a 30-minute late window (bot was down across
    # launch): still due at +10 min, gone at +35 min.
    assert due_reminders(gy, dt(2026, 11, 18, 18, 10, tzinfo=timezone.utc)) == ["launch"]
    assert due_reminders(gy, dt(2026, 11, 18, 18, 35, tzinfo=timezone.utc)) == []
    # Mint added 1 minute before launch: the 1-hour beep must NOT fire
    # late (its window passed an hour ago) — only the launch beep.
    late = {"id": 2, "name": "Dred",
            "mint_time_utc": "2026-11-19T23:54:00+00:00",
            "price": "Free", "chain": "Ethereum", "link": "", "sent": []}
    assert due_reminders(late, dt(2026, 11, 19, 23, 53, tzinfo=timezone.utc)) == []
    assert due_reminders(late, dt(2026, 11, 19, 23, 54, tzinfo=timezone.utc)) == ["launch"]
    # Tracking reply must only promise beeps still in the future
    assert upcoming_reminders("2026-11-18T18:00:00+00:00",
                               dt(2026, 11, 18, 17, 59, tzinfo=timezone.utc)) == ["at launch"]
    assert upcoming_reminders("2026-11-18T18:00:00+00:00",
                               dt(2026, 11, 18, 10, 0, tzinfo=timezone.utc)) == ["1 hour before", "at launch"]
    assert upcoming_reminders("2026-11-20T18:00:00+00:00",
                               dt(2026, 11, 18, 10, 0, tzinfo=timezone.utc)) == ["1 day before", "1 hour before", "at launch"]
    assert beep_promise("2026-11-18T18:00:00+00:00",
                        dt(2026, 11, 18, 17, 59, tzinfo=timezone.utc)) == "I will beep you at launch."
    # /add flow end-to-end in memory (dry-run sends print, never hit network)
    state = {"users": {}}
    assert "step" not in handle_message(state, 1, "/add")  # returns prompt
    handle_message(state, 1, "Test Mint")
    r = handle_message(state, 1, "2026-12-05 19:00")
    assert "price" in r.lower(), r
    handle_message(state, 1, "0.02 ETH")
    handle_message(state, 1, "Base")
    r = handle_message(state, 1, "https://example.com/mint")
    assert "Saved" in r, r
    assert len(state["users"]["1"]["mints"]) == 1
    assert state["users"]["1"]["mints"][0]["mint_time_utc"] == "2026-12-05T18:00:00+00:00"
    r = handle_message(state, 1, "/track grant-yun")
    assert "Tracking" in r, r
    assert len(state["users"]["1"]["mints"]) == 2
    # /delete must not crash the bot (it used to: __delete__ is not an
    # /add step, and ADD_STEPS.index() raised ValueError, uncaught).
    r = handle_message(state, 1, "/delete")
    assert "Send the number" in r, r
    r = handle_message(state, 1, "1")
    assert "Deleted mint #1" in r, r
    assert len(state["users"]["1"]["mints"]) == 1
    # mini app bridge: Track / Add sends web_app_data, bot must save + beep it
    r = handle_web_app_data(state, 2, json.dumps({
        "action": "track", "mint": {"name": "Quacked Ops",
        "mint_time_utc": "2026-12-01T18:00:00+00:00",
        "price": "0.02 ETH", "chain": "Base", "link": "",
        "source_key": "quacked-ops"}}))
    assert r["ok"] and "Tracking" in r["reply"], r
    assert len(state["users"]["2"]["mints"]) == 1
    # A duplicate track is ok:true (already) — the app must show it saved,
    # not failed: the mint IS on the list and WILL beep.
    r = handle_web_app_data(state, 2, json.dumps({
        "action": "track", "mint": {"name": "Quacked Ops",
        "mint_time_utc": "2026-12-01T18:00:00+00:00",
        "price": "0.02 ETH", "chain": "Base", "link": "",
        "source_key": "quacked-ops"}}))
    assert r["ok"] and r["already"] and "already" in r["reply"], r
    assert len(state["users"]["2"]["mints"]) == 1
    r = handle_web_app_data(state, 2, "not json")
    assert not r["ok"] and "could not read" in r["reply"], r
    # Date-only mint (no announced time): watchlist-only, never beeps.
    r = handle_web_app_data(state, 3, json.dumps({
        "mint": {"name": "NoTime", "mint_time_utc": "2026-12-05T23:00:00+00:00",
                 "price": "Free", "chain": "Ethereum", "link": "",
                 "has_time": False}}))
    assert r["ok"] and "Watching" in r["reply"], r
    m = state["users"]["3"]["mints"][0]
    assert due_reminders(m, dt(2026, 12, 5, 23, 1, tzinfo=timezone.utc)) == []
    # Time announced later: the same mint updates and beeps turn on.
    r = handle_web_app_data(state, 3, json.dumps({
        "mint": {"name": "NoTime", "mint_time_utc": "2026-12-05T19:00:00+00:00",
                 "price": "Free", "chain": "Ethereum", "link": "",
                 "has_time": True}}))
    assert r["ok"] and "Tracking" in r["reply"], r
    assert len(state["users"]["3"]["mints"]) == 1
    assert state["users"]["3"]["mints"][0]["has_time"] is True
    # Bridge helpers: /status lists what the bot has, /untrack removes it.
    st = bridge_status(state, 3)
    assert st["ok"] and len(st["mints"]) == 1 and st["mints"][0]["name"] == "NoTime", st
    u = bridge_untrack(state, 3, {"name": "NoTime"})
    assert u["ok"], u
    assert bridge_status(state, 3)["mints"] == []
    # initData verification: a correctly signed payload validates, and
    # any tampering (or a wrong token) fails.
    token = "123456:TESTTOKEN"
    import hashlib as _hl, hmac as _hm
    from urllib.parse import quote as _q
    fields = {"auth_date": str(int(time.time())),
              "user": json.dumps({"id": 5601504588, "first_name": "Big wave"},
                                 separators=(",", ":"))}
    check = "\n".join("%s=%s" % (k, v) for k, v in sorted(fields.items()))
    secret = _hm.new(b"WebAppData", token.encode(), _hl.sha256).digest()
    digest = _hm.new(secret, check.encode(), _hl.sha256).hexdigest()
    init_data = "&".join("%s=%s" % (k, _q(v, safe="")) for k, v in fields.items())
    init_data += "&hash=" + digest
    assert verify_init_data(init_data, token) == 5601504588
    assert verify_init_data(init_data, "123456:WRONGTOKEN") is None
    assert verify_init_data(init_data.replace("5601504588", "123456789"), token) is None
    assert verify_init_data("", token) is None
    # Corrupt state file: quarantined, last-good backup used, never silently empty.
    import os as _os
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        fh.write('{"users": {"9": {"mints": [{"id": 1, "name": "Kept"}]}}}')
    save_state(load_state())
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        fh.write('{"users": {"9": {"mints": [{"id": 1, "name": "Kept"}')
    recovered = load_state()
    assert recovered["users"]["9"]["mints"][0]["name"] == "Kept", recovered
    assert _os.path.exists(STATE_PATH + ".bak")
    print("selftest: all checks passed")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(run_loop())
