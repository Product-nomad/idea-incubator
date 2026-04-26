#!/usr/bin/env python3
"""Idea Incubator Bot — text-first idea capture with Mom-Test-style critique.

Flow: /new → title → niche → problem → prior attempts → blockers → magic wand
→ Claude critiques the answers on evidence quality (not idea-quality scoring)
→ write ideas/<title>.md → auto-commit & push.

Question set rooted in customer-discovery practice and *The Mom Test*.
"""

import asyncio
import logging
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from anthropic import Anthropic
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("idea-incubator")

REPO_DIR = Path(__file__).resolve().parent
# Where to write idea .md files. Should be a checkout of a (private) git repo
# so each idea can be auto-pushed. Falls back to a local ideas/ dir if unset.
IDEAS_DIR = Path(os.environ.get("IDEAS_DIR") or (REPO_DIR / "ideas")).resolve()
IDEAS_DIR.mkdir(parents=True, exist_ok=True)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-7")
AUTHORIZED_USERS = {
    int(uid.strip())
    for uid in os.environ.get("AUTHORIZED_USERS", "").split(",")
    if uid.strip().isdigit()
}

if not TELEGRAM_TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN env var is required")
if not ANTHROPIC_API_KEY:
    raise SystemExit("ANTHROPIC_API_KEY env var is required")
if not AUTHORIZED_USERS:
    raise SystemExit(
        "AUTHORIZED_USERS env var is required (comma-separated Telegram numeric IDs)"
    )

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY, timeout=60.0, max_retries=2)

TITLE, NICHE, PROBLEM, PRIOR_ATTEMPTS, BLOCKERS, MAGIC_WAND = range(6)

ASSESSMENT_PROMPT = """You are a critic in the spirit of *The Mom Test* (Rob Fitzpatrick) \
and Steve Blank's customer development. The user has captured an early-stage idea below. \
Critique their answers on EVIDENCE QUALITY — not on whether the idea sounds promising.

Penalise:
- Hypothetical statements ("people would love this", "I think users want...")
- Generic personas ("small businesses", "developers", "anyone who codes")
- Self-flattery, pitchy language, or speculation about market size
- Answers that describe the founder's hopes rather than observed user behaviour

Reward:
- Specific people named or specific niches identifiable enough to find on LinkedIn
- Concrete past behaviour: what users have already tried, paid for, or built
- Real conversations, real spending, real workarounds with named tools
- Honest "I don't know yet" admissions over hand-wavy guesses

Output strictly in this Markdown structure (no code fences, no commentary outside the structure):

### Verdict
One of: **Strong evidence** / **Mixed** / **Mostly hypothetical** — followed by one sentence justifying.

### Evidence quality
- **Niche specificity:** Strong / Weak — one-sentence why
- **Problem evidence:** Strong / Weak — one-sentence why
- **Prior-attempt signal:** Strong / Weak — one-sentence why (this is the strongest predictor of willingness to pay)
- **Solution clarity:** Strong / Weak — one-sentence why

### What's missing
2-3 bullets of specific things the founder doesn't yet know — phrased as "you don't yet know whether X" or "stronger if you knew Y".

### Recommended next step
One concrete action. Default to "Run 5-8 Discovery Calls with [specific niche] before any solution work" unless evidence is already strong, in which case suggest a smoke-test landing page or a paid pre-order test.

### Mom Test red flags
List any phrases in the user's answers that are hypothetical, vague, or pitchy. Quote them verbatim with quotation marks. If none, write "None — answers are grounded."

---
Now critique the following idea:

**Title:** {title}

**Niche (who has this problem):**
{niche}

**Problem:**
{problem}

**Prior attempts (effort or money already spent):**
{prior_attempts}

**Blockers (what's prevented them solving it):**
{blockers}

**Magic wand (ideal solution):**
{magic_wand}
"""


def authorised(user_id: int) -> bool:
    return user_id in AUTHORIZED_USERS


def sanitise_filename(title: str) -> str:
    safe = re.sub(r"[^\w\s-]", "", title).strip()
    safe = re.sub(r"[\s_-]+", "_", safe).lower()
    return safe[:60] or "idea"


def unique_path(name: str) -> Path:
    base = IDEAS_DIR / f"{name}.md"
    if not base.exists():
        return base
    i = 2
    while True:
        candidate = IDEAS_DIR / f"{name}_{i}.md"
        if not candidate.exists():
            return candidate
        i += 1


async def reject_if_unauthorised(update: Update) -> bool:
    if not authorised(update.effective_user.id):
        logger.warning("Rejected unauthorised user %s", update.effective_user.id)
        await update.message.reply_text("Unauthorized.")
        return True
    return False


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await reject_if_unauthorised(update):
        return ConversationHandler.END
    await update.message.reply_text(
        "Idea Incubator Bot\n\n"
        "/new — submit a new idea (6 questions, Mom Test critique)\n"
        "/list — show recent ideas\n"
        "/cancel — abort current submission\n\n"
        "Tip: dictate using your phone keyboard's mic for hands-free entry."
    )
    return ConversationHandler.END


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await reject_if_unauthorised(update):
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text("Give this idea a short working title.")
    return TITLE


async def got_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["title"] = update.message.text.strip()
    await update.message.reply_text(
        f"Got it: {context.user_data['title']}\n\n"
        "Who specifically has this problem? Describe the niche as narrowly "
        "as you can (role, context, life-stage)."
    )
    return NICHE


async def got_niche(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["niche"] = update.message.text.strip()
    await update.message.reply_text(
        "What problem are they facing? What's the main challenge right now?"
    )
    return PROBLEM


async def got_problem(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["problem"] = update.message.text.strip()
    await update.message.reply_text(
        "How have they tried to solve this before — with effort or money?"
    )
    return PRIOR_ATTEMPTS


async def got_prior_attempts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["prior_attempts"] = update.message.text.strip()
    await update.message.reply_text(
        "What's prevented them from solving it so far?"
    )
    return BLOCKERS


async def got_blockers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["blockers"] = update.message.text.strip()
    await update.message.reply_text(
        "If they had a magic wand, how would they ideally solve this?"
    )
    return MAGIC_WAND


async def got_magic_wand(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["magic_wand"] = update.message.text.strip()
    await update.message.reply_text("Generating Mom Test critique via Claude — one moment…")

    try:
        critique_md = await generate_critique(context.user_data)
    except Exception as exc:
        logger.exception("Critique failed")
        await update.message.reply_text(
            f"Critique failed: {exc}\nIdea NOT saved — try /new again."
        )
        return ConversationHandler.END

    submitted_by = update.effective_user.first_name or "Unknown"
    md_path = save_markdown(context.user_data, critique_md, submitted_by)
    commit_status = git_commit_and_push(md_path)
    parts = [f"Saved as `{md_path.name}`", commit_status]
    url = web_url_for(md_path)
    if url:
        parts.append(url)
    await update.message.reply_text(
        "\n".join(parts),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_if_unauthorised(update):
        return
    recent = sorted(
        IDEAS_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True
    )[:5]
    if not recent:
        await update.message.reply_text("No ideas yet. Send /new to start.")
        return
    lines = ["Recent ideas:"]
    for p in recent:
        lines.append(f"• {p.stem.replace('_', ' ')}")
    await update.message.reply_text("\n".join(lines))


async def generate_critique(data: dict) -> str:
    prompt = ASSESSMENT_PROMPT.format(**data)

    def call() -> str:
        msg = anthropic_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text

    raw = await asyncio.to_thread(call)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:markdown|md)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()


def save_markdown(data: dict, critique_md: str, submitted_by: str) -> Path:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    md = f"""# {data['title']}

**Submitted:** {timestamp}
**Submitted by:** {submitted_by}

## Niche
{data['niche']}

## Problem
{data['problem']}

## Prior attempts (effort or money)
{data['prior_attempts']}

## Blockers
{data['blockers']}

## Magic wand (ideal solution)
{data['magic_wand']}

## Mom Test Critique
{critique_md}

---
*Generated by Idea Incubator Bot. Critique focuses on evidence quality, not idea promise — next step is usually 5-8 Discovery Calls in the spirit of* The Mom Test *(Fitzpatrick).*
"""

    name = sanitise_filename(data["title"])
    path = unique_path(name)
    path.write_text(md)
    logger.info("Saved idea to %s", path.name)
    return path


def web_url_for(md_path: Path) -> str | None:
    """Best-effort GitHub blob URL for a file in IDEAS_DIR. Returns None if remote isn't GitHub."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=IDEAS_DIR, check=True, capture_output=True, text=True, timeout=5,
        )
    except subprocess.SubprocessError:
        return None
    remote = result.stdout.strip()
    m = re.match(r"git@github\.com:(.+?)(?:\.git)?$", remote) \
        or re.match(r"https://github\.com/(.+?)(?:\.git)?$", remote)
    if not m:
        return None
    return f"https://github.com/{m.group(1)}/blob/main/{md_path.name}"


def git_commit_and_push(md_path: Path) -> str:
    try:
        rel = md_path.relative_to(IDEAS_DIR)
        subprocess.run(
            ["git", "add", str(rel)],
            cwd=IDEAS_DIR, check=True, capture_output=True, timeout=30,
        )
        subprocess.run(
            ["git", "commit", "-m", f"Add idea: {md_path.stem.replace('_', ' ')}"],
            cwd=IDEAS_DIR, check=True, capture_output=True, timeout=30,
        )
        subprocess.run(
            ["git", "push"],
            cwd=IDEAS_DIR, check=True, capture_output=True, timeout=60,
        )
        return "Pushed to repo."
    except subprocess.CalledProcessError as e:
        err = (e.stderr.decode() if e.stderr else str(e)).strip()
        logger.error("Git failure: %s", err)
        last = err.splitlines()[-1] if err else "unknown error"
        return f"Idea saved locally but git push failed: {last}"


def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("new", cmd_new)],
        states={
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_title)],
            NICHE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_niche)],
            PROBLEM: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_problem)],
            PRIOR_ATTEMPTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_prior_attempts)],
            BLOCKERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_blockers)],
            MAGIC_WAND: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_magic_wand)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(conv)

    logger.info("Idea Incubator Bot starting (model=%s)", CLAUDE_MODEL)
    app.run_polling()


if __name__ == "__main__":
    main()
