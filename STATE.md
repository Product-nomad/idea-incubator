# STATE — idea-incubator

## Goal
Telegram bot for capturing ideas (text, optionally dictated via phone keyboard mic), running them through fixed clarifying questions, generating a Claude-powered 7-dimension assessment, and auto-committing the markdown to a private GitHub repo.

## Status
v0.3 running on Haiku. Two-repo architecture: source public, ideas private.

## What's done
- Project layout: `bot.py`, `requirements.txt`, `.env.example`, `.gitignore`, `README.md`, `idea-bot.service.example`, `ideas/`.
- Bot uses `python-telegram-bot` v20 `ConversationHandler`, env-var config, allowlisted users.
- Claude API integration with 60s timeout + 2 retries. Model defaults to `claude-opus-4-7`; currently overridden to `claude-haiku-4-5-20251001` via `.env` for cost (~$0.005/idea vs ~$0.07 on Opus).
- Auto-commit + push on each completed idea, with subprocess timeouts (30s add/commit, 60s push).
- **Two-repo split** (2026-04-26):
  - `Product-nomad/idea-incubator` — bot source, **public**.
  - `Product-nomad/idea-incubator-ideas` — captured ideas, **private**, `zummed` (Paul) added as collaborator.
  - Bot uses `IDEAS_DIR` env var to point at a checkout of the private repo (`/home/vpc/projects/idea-incubator-ideas/`); all git ops happen inside that dir.
- Bot username `@Incub8_bot`.
- Question set: title, niche, problem, prior attempts (effort/money), blockers, magic wand. Methodology rooted in *The Mom Test* (Fitzpatrick) and customer-discovery practice.
- Assessment is a Mom Test critique (evidence quality, red flags, recommended next step), not a scoring rubric.
- Running as `idea-bot.service` (system systemd unit, enabled at boot).

## What's next
1. End-to-end test on the new architecture: send `/new` to `@Incub8_bot`, verify the resulting `.md` lands in the private `idea-incubator-ideas` repo (NOT the public source repo).
2. Add Paul's numeric Telegram ID to `AUTHORIZED_USERS` in `.env` once he sends his first message and gets rejected (or asks userinfobot first). Restart the service after.
3. Confirm the secret-rotation status (Anthropic key / Telegram token both leaked into the chat transcript on 2026-04-26 during setup).

## Open questions / future
- Add Paul (engineer friend) as a second authorised user once he wants in.
- Voice input as v0.2 (Whisper API or local) — currently text-only; phone-keyboard dictation covers the UX in the meantime.
- Adaptive follow-up questions (LLM-driven instead of fixed) — deferred.
- Optional: review-before-push mode behind an env flag, instead of auto-commit.

## Design decisions worth remembering
- **Text-only, not voice** for v0. Telegram Bot API doesn't transcribe; phone-side dictation is the simplest path.
- **Auto-commit** per completed idea, not manual `/commit`. Edit the .md afterwards if needed.
- **Filename** is just sanitised title (no date prefix) — git history carries the dates.
- **System systemd unit** (not user) to match `claude-agent.service`.
- **No `incubator_config.json`** with token-on-disk — env vars only.
- **Mom Test lens, not scoring rubric** for the assessment. The reference methodology argues against self-rating dimensions like Market Size or Founder-Market Fit from the armchair; evidence quality is what the bot grades instead.
