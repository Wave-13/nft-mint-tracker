# NFT Mint Tracker — Telegram-first bot

A product to sell to NFT traders and collectors: they add the mints
that interest them inside Telegram, and the bot beeps them 1 day
before, 1 hour before, and at launch — with price, chain and link.

Agreed with big wave 2026-11-18 (see MEMORY.md, NFT Mint Tracker):
- TELEGRAM-first. Users add their own mints themselves in Telegram.
- Free mint calendar to look at; paid Telegram reminders.
- Test on Railway only. Permanent home: Oracle Always Free VM.
  Railway trial (30 days / $5) is NOT the permanent home.

## What is built

- `bot.py` — stdlib-only Telegram bot (no pip installs).
  Long-polling `getUpdates`, so it needs a process that stays on.
  Commands: `/start /add /list /delete /upcoming /track /help /cancel`.
- State: `state/mints.json` — one list per Telegram chat id.
  Each user's mints are private to them; no shared public list,
  so a fake mint a user adds only ever beeps them.
- Verified mints (the only shared list, curated, verified first):
  Grant Yun — Sections, Nov 18 2026 19:00 WAT, 0.05 ETH, Ethereum;
  Quacked Ops, Dec 1 2026, Base, allowlist 0.02 / public 0.03 ETH.
  Quacked Ops launch time + mint link are still pending — the bot
  stores 19:00 WAT as a placeholder and says so in `/upcoming`.
- Reminders: 1 day / 1 hour / at launch, each sent once.
  A restart does not triple-beep: only the latest due reminder fires.

## Test (no Telegram needed)

```sh
python3 bot.py --selftest
```

Checks date parsing (incl. `Dec 1 19:00`), countdowns, the full
`/add` flow, `/track grant-yun`, and reminder-due logic.
Verified 2026-11-18: all checks passed.

## Deploy — test on Railway

1. big wave creates the bot in BotFather (he did this before for
   Wave14_bot, Oct 29) and pastes the token ONCE into Railway's
   environment variables as `TELEGRAM_BOT_TOKEN`. The token is never
   stored in a file here.
2. Push this folder to a GitHub repo (e.g. `Wave-13/nft-mint-tracker`).
3. Railway → New Project → Deploy from GitHub → pick the repo.
   Start command is in `railway.json`: `python3 bot.py`.
   No card is needed for the Railway trial — and no card is ever
   to be added (his rule, 2026-11-17).
4. Test in Telegram: `/start`, `/add` a test mint 10 minutes out,
   check the launch beep lands, `/delete` it.
5. Grant Yun tonight (19:00 WAT) is the live test case:
   `/track grant-yun` → 1-hour beep at 18:00 WAT, launch beep 19:00 WAT.

## Deploy — permanent home, Oracle Always Free VM

Same command, `python3 bot.py`, kept alive with systemd (or tmux
for the first test). State file `state/mints.json` moves with it —
copy it over so users' lists survive the move. This is the same
Oracle VM plan as the Wave Discord bot; the VM itself is not set
up yet, so Railway is the test home until it is.

## Still open (his call, not built yet)

- Price for the paid reminders (Free vs Pro split is in the demo
  page, artifact `nft-mint-tracker`; no price was set).
- How a buyer pays / is let in (no payment flow built yet).
- Quacked Ops exact launch time + mint link — update
  `VERIFIED_MINTS` in `bot.py` when verified, and the demo page too.
