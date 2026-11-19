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

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

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
    },
    {
        "key": "quacked-ops",
        "name": "Quacked Ops",
        "mint_time_utc": "2026-12-01T18:00:00+00:00",  # Dec 1, time TBC — 19:00 WAT placeholder, see README
        "price": "Allowlist 0.02 ETH / Public 0.03 ETH",
        "chain": "Base",
        "link": "",
        "note": "115 pieces. Exact launch time + mint link still pending.",
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
    "/help — this list again\n\n"
    "Your list is yours alone — nobody else sees what you add."
)


# ---------------------------------------------------------------- state
def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and "users" in data:
            return data
    except (FileNotFoundError, ValueError):
        pass
    return {"users": {}}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
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
        print("ERROR Telegram API: %s" % body, file=sys.stderr)
        return False
    return True


# ---------------------------------------------------------------- reminders
REMINDER_OFFSETS = [("day", timedelta(days=1), "1 day to go"),
                    ("hour", timedelta(hours=1), "1 hour to go"),
                    ("launch", timedelta(0), "MINTING NOW")]


def due_reminders(mint, now=None):
    """Which reminder is due for this mint right now (not yet sent).

    Returns at most one — the latest due (launch beats hour beats day)
    — so a bot that was offline does not triple-beep when it returns,
    and a mint added late still gets the next beep, not a stale one.
    A mint whose launch window (5 min) has passed gets nothing.
    """
    now = now or datetime.now(timezone.utc)
    mint_time = datetime.fromisoformat(mint["mint_time_utc"])
    if now >= mint_time + timedelta(minutes=5):
        return []
    sent = mint.setdefault("sent", [])
    for key, offset, _label in reversed(REMINDER_OFFSETS):
        if key in sent:
            continue
        if key == "launch":
            if mint_time <= now:
                return [key]
        elif mint_time - offset <= now:
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
    for chat_id, udata in state["users"].items():
        for mint in udata.get("mints", []):
            for key in due_reminders(mint, now):
                label = dict((k, l) for k, _o, l in REMINDER_OFFSETS)[key]
                if send_message(chat_id, reminder_text(mint, label)):
                    mint.setdefault("sent", []).append(key)
                    sent_count += 1
                    changed = True
    if changed:
        save_state(state)
    return sent_count


# ---------------------------------------------------------------- /add conversation
# In-memory only: an interrupted /add restarts cleanly. Saved mints
# live in state/mints.json, never in this dict.
conversations = {}


def handle_message(state, chat_id, text):
    """Returns reply text (or None if already handled/sent)."""
    text = (text or "").strip()
    conv = conversations.get(str(chat_id))

    if conv is not None and not text.startswith("/"):
        step = conv["step"]
        draft = conv["draft"]
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
        draft.setdefault("price", "?")
        draft.setdefault("chain", "?")
        mints.append(draft)
        save_state(state)
        del conversations[str(chat_id)]
        return ("Saved ✅\n\n%s\n%s · %s\nChain: %s%s\n\n"
                "I will beep you 1 day before, 1 hour before, and at launch."
                % (draft["name"], fmt_wat(draft["mint_time_utc"]),
                   draft["price"], draft["chain"],
                   ("\nMint: " + draft["link"]) if draft.get("link") else ""))

    if text in ("/start", "/help"):
        return HELP_TEXT
    if text == "/add":
        conversations[str(chat_id)] = {"step": "name", "draft": {}}
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
        conversations[str(chat_id)] = {"step": "__delete__", "draft": {}}
        lines = ["Send the number of the mint to delete:\n"]
        for m in mints:
            lines.append("#%d %s (%s)" % (m["id"], m["name"], fmt_wat(m["mint_time_utc"])))
        return "\n".join(lines)
    if conv is not None and conv["step"] == "__delete__" and not text.startswith("/"):
        mints = user_mints(state, chat_id)
        try:
            want = int(text)
        except ValueError:
            return "Send just the number, e.g. 2 — or /cancel."
        before = len(mints)
        mints[:] = [m for m in mints if m["id"] != want]
        del conversations[str(chat_id)]
        if len(mints) == before:
            return "No mint #%d on your list." % want
        save_state(state)
        return "Deleted mint #%d." % want
    if text == "/upcoming":
        lines = ["Verified mints — checked by us, not user-added:\n"]
        for v in VERIFIED_MINTS:
            lines.append("• %s\n  %s — %s\n  %s · %s%s\n  Track it: /track %s" % (
                v["name"], fmt_wat(v["mint_time_utc"]),
                countdown(v["mint_time_utc"]), v["price"], v["chain"],
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
        mints.append({"id": next_id(mints), "name": v["name"],
                     "mint_time_utc": v["mint_time_utc"], "price": v["price"],
                     "chain": v["chain"], "link": v["link"],
                     "note": v.get("note", ""), "source_key": key, "sent": []})
        save_state(state)
        return ("Tracking %s ✅\n%s · %s\nI will beep you 1 day before, "
                "1 hour before, and at launch."
                % (v["name"], fmt_wat(v["mint_time_utc"]), v["price"]))
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
    try:
        data = json.loads(data_str or "{}")
    except Exception:
        return "I could not read that from the mini app. Try again from the bot with /add."
    mint = data.get("mint") or {}
    name = (mint.get("name") or "").strip()
    when = (mint.get("mint_time_utc") or "").strip()
    if not name or not when:
        return "That mint had no name or date. Try adding it again from the mini app."
    try:
        mint_time = datetime.fromisoformat(when.replace("Z", "+00:00"))
        if mint_time.tzinfo is None:
            mint_time = mint_time.replace(tzinfo=timezone.utc)
    except ValueError:
        return "That mint had a date I could not read. Try adding it again from the mini app."
    if mint_time < datetime.now(timezone.utc):
        return "%s has already passed — add a future mint to get beeps." % name
    mints = user_mints(state, chat_id)
    key = (mint.get("source_key") or "").strip().lower()
    if key and any(m.get("source_key") == key for m in mints):
        return "%s is already on your list." % name
    if any(m.get("name") == name and m.get("mint_time_utc") == mint_time.isoformat() for m in mints):
        return "%s is already on your list." % name
    mints.append({"id": next_id(mints), "name": name,
                 "mint_time_utc": mint_time.isoformat(),
                 "price": mint.get("price") or "?",
                 "chain": mint.get("chain") or "?",
                 "link": mint.get("link") or "",
                 "note": mint.get("note") or "",
                 "source_key": key, "sent": []})
    save_state(state)
    return ("Tracking %s ✅\n%s · %s\nI will beep you 1 day before, "
            "1 hour before, and at launch."
            % (name, fmt_wat(mint_time.isoformat()), mints[-1]["price"]))


# ---------------------------------------------------------------- polling loop
def run_loop():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("ERROR: set TELEGRAM_BOT_TOKEN first (BotFather token).",
              file=sys.stderr)
        return 2
    state = load_state()
    offset = 0
    last_check = 0.0
    print("Mint Tracker bot running. State: %s" % STATE_PATH)
    while True:
        try:
            body = api("getUpdates", {"offset": offset, "timeout": 20}, token)
        except Exception as exc:
            print("getUpdates failed: %s — retrying" % exc, file=sys.stderr)
            time.sleep(POLL_SECONDS)
            continue
        for update in body.get("result", []):
            offset = max(offset, update.get("update_id", 0) + 1)
            msg = update.get("message") or {}
            chat = msg.get("chat") or {}
            if "id" not in chat:
                continue
            wad = msg.get("web_app_data") or {}
            if wad.get("data"):
                reply = handle_web_app_data(state, chat["id"], wad["data"])
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
        if time.time() - last_check >= CHECK_SECONDS:
            last_check = time.time()
            try:
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
    # Grant Yun tonight: 1-day reminder already due (mint - 24h was yesterday),
    # 1-hour due at 17:00 UTC, launch at 18:00 UTC.
    gy = {"id": 1, "name": "Grant Yun — Sections",
          "mint_time_utc": "2026-11-18T18:00:00+00:00",
          "price": "0.05 ETH", "chain": "Ethereum", "link": "", "sent": []}
    assert due_reminders(gy, now) == ["day"], due_reminders(gy, now)
    assert due_reminders(gy, dt(2026, 11, 18, 17, 30, tzinfo=timezone.utc)) == ["hour"]
    assert due_reminders(gy, dt(2026, 11, 18, 18, 2, tzinfo=timezone.utc)) == ["launch"]
    assert due_reminders(gy, dt(2026, 11, 18, 18, 10, tzinfo=timezone.utc)) == []
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
    # mini app bridge: Track / Add sends web_app_data, bot must save + beep it
    r = handle_web_app_data(state, 2, json.dumps({
        "action": "track", "mint": {"name": "Quacked Ops",
        "mint_time_utc": "2026-12-01T18:00:00+00:00",
        "price": "0.02 ETH", "chain": "Base", "link": "",
        "source_key": "quacked-ops"}}))
    assert "Tracking" in r, r
    assert len(state["users"]["2"]["mints"]) == 1
    r = handle_web_app_data(state, 2, json.dumps({
        "action": "track", "mint": {"name": "Quacked Ops",
        "mint_time_utc": "2026-12-01T18:00:00+00:00",
        "price": "0.02 ETH", "chain": "Base", "link": "",
        "source_key": "quacked-ops"}}))
    assert "already" in r, r
    r = handle_web_app_data(state, 2, "not json")
    assert "could not read" in r, r
    print("selftest: all checks passed")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(run_loop())
