# Idea Incubator

Telegram bot that captures startup ideas via a 6-question Discovery Call flow, then uses Claude to produce a Mom-Test-style critique focused on **evidence quality** (not idea promise). Each idea becomes a markdown file auto-committed and pushed to a separate **private** ideas repo.

This repo holds the bot source. Ideas themselves live in a separate private repo pointed at via the `IDEAS_DIR` env var (e.g. `Product-nomad/idea-incubator-ideas`).

The question set and assessment lens are drawn from various methodologies and *The Mom Test* (Rob Fitzpatrick).

## Flow

1. `/new` in Telegram
2. **Title** → **Niche** (who specifically) → **Problem** → **Prior attempts** (effort or money) → **Blockers** → **Magic wand** (ideal solution)
3. Bot calls Claude → generates a Mom Test critique (Verdict, Evidence quality, What's missing, Recommended next step, Red flags)
4. Markdown written to `ideas/<title>.md` and auto-pushed

Tip: use your phone keyboard's mic to dictate. The whole flow takes ~2 minutes.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill in TELEGRAM_BOT_TOKEN, ANTHROPIC_API_KEY, AUTHORIZED_USERS
python bot.py
```

### Env vars

| Var | Required | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | From @BotFather |
| `ANTHROPIC_API_KEY` | yes | console.anthropic.com |
| `AUTHORIZED_USERS` | yes | Comma-separated numeric Telegram IDs (get yours from @userinfobot) |
| `CLAUDE_MODEL` | no | Defaults to `claude-opus-4-7`. For cost-sensitive runs use `claude-haiku-4-5-20251001` (~$0.005/idea). |
| `IDEAS_DIR` | no | Where idea `.md` files are written + pushed from. Should be a checkout of a (private) git repo. Defaults to `./ideas/` alongside `bot.py`. |

## Run as a systemd service

```bash
sudo cp idea-bot.service.example /etc/systemd/system/idea-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now idea-bot
sudo journalctl -u idea-bot -f
```

## Commands

- `/new` — start a new idea
- `/list` — last 5 ideas
- `/cancel` — abort current submission
- `/start` — help

## Why this question set?

The reference methodology argues that ideas can't be self-rated from the armchair — Problem Acuity, Market Size, etc. are *discovered* through 5-8 customer Discovery Calls. So:

- Q4 (**prior attempts**) is the strongest predictor of willingness to pay — has the user already spent effort or money trying to solve this?
- Q5 (**blockers**) reveals whether the problem is hard or just neglected.
- Q6 (**magic wand**) is the only hypothetical question, and its job is to surface solution-shape ideas, not validate demand.
- The "stakes / why should they care" question from earlier designs was dropped — *The Mom Test* explicitly warns against asking hypothetical "would you care" questions because people lie politely.

The Claude critique grades the answers on **evidence quality** (specific niches, real money, named tools, observed behaviour) rather than scoring the idea on a 1-10 rubric.

## Output format

Each idea produces a markdown file with these sections:

- Title, submitted timestamp, submitter
- Niche, Problem, Prior attempts, Blockers, Magic wand (raw user answers)
- Mom Test Critique: Verdict / Evidence quality / What's missing / Recommended next step / Red flags

The recommended next step is almost always "run 5-8 Discovery Calls with the named niche before any solution work."
