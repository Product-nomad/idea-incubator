#!/usr/bin/env python3
"""Idea Incubator Bot — text-first idea capture with Mom-Test-style critique.

Flow: /new → title → niche → problem → prior attempts → blockers → magic wand
→ Claude critiques the answers on evidence quality (not idea-quality scoring)
→ write ideas/<title>.md with YAML frontmatter (title, submitted, by, status,
verdict, niche, tags) → regenerate README.md index → auto-commit & push.

Other commands:
- /status — change the status of an existing idea (inbox|discovery|smoke-test|mvp|killed)
- /note — append a timestamped note to an existing idea (e.g. discovery-call findings)
- /list — show recent ideas with status + verdict

Question set rooted in customer-discovery practice and *The Mom Test*.
"""

import asyncio
import html
import json
import logging
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

import yaml
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

STATUSES = ["inbox", "discovery", "smoke-test", "mvp", "killed"]

TITLE, NICHE, PROBLEM, PRIOR_ATTEMPTS, BLOCKERS, MAGIC_WAND = range(6)
STATUS_SELECT, STATUS_NEW = range(6, 8)
NOTE_SELECT, NOTE_TEXT = range(8, 10)

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

Return ONLY valid JSON in this exact shape (no code fences, no commentary):
{{
  "verdict": one of "Strong evidence" / "Mixed" / "Mostly hypothetical",
  "tags": [2-4 short lowercase hyphenated tags useful for grouping ideas later, e.g. "b2b", "b2c", "saas", "marketplace", "mobile-app", "browser-extension", "dev-tools", "ai", "fintech", "healthtech", "education", "consumer", "productivity"],
  "critique_markdown": the full critique as a string of markdown with these sections in this order: "### Verdict" (one of Strong evidence / Mixed / Mostly hypothetical, then one sentence justification), "### Evidence quality" (4 bullets: Niche specificity, Problem evidence, Prior-attempt signal, Solution clarity, each Strong/Weak with one-sentence why), "### What's missing" (2-3 bullets phrased as "you don't yet know whether X" or "stronger if you knew Y"), "### Recommended next step" (one concrete action; default to running 5-8 Discovery Calls with the named niche before any solution work, unless evidence is already strong), "### Mom Test red flags" (list any phrases in the user's answers that are hypothetical, vague, or pitchy, quoted verbatim with quotation marks; if none, write "None — answers are grounded.")
}}

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


# ---------------------------------------------------------------------------
# Frontmatter helpers
# ---------------------------------------------------------------------------

FM_RE = re.compile(r"\A---\n(.*?)\n---\n+(.*)", re.DOTALL)


def parse_md_with_frontmatter(text: str) -> tuple[dict, str]:
    m = FM_RE.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(fm, dict):
        return {}, text
    return fm, m.group(2)


def write_md_with_frontmatter(fm: dict, body: str) -> str:
    fm_yaml = yaml.safe_dump(
        fm, sort_keys=False, allow_unicode=True, default_flow_style=False
    ).strip()
    return f"---\n{fm_yaml}\n---\n\n{body.lstrip()}"


# ---------------------------------------------------------------------------
# Index / listing
# ---------------------------------------------------------------------------

def list_ideas() -> list[dict]:
    items = []
    for p in sorted(IDEAS_DIR.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.name.lower() == "readme.md":
            continue
        try:
            fm, _ = parse_md_with_frontmatter(p.read_text())
        except Exception:
            fm = {}
        items.append({
            "path": p,
            "filename": p.name,
            "title": fm.get("title") or p.stem.replace("_", " ").title(),
            "status": fm.get("status") or "?",
            "verdict": fm.get("verdict") or "—",
            "niche": fm.get("niche") or "—",
            "submitted": fm.get("submitted") or "",
            "tags": fm.get("tags") or [],
        })
    return items


def _md_cell(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ").strip() or "—"


def _short_label(s: str, n: int = 24) -> str:
    s = (s or "").strip()
    return (s[:n - 1] + "…") if len(s) > n else s


def _mermaid_safe(s: str) -> str:
    """Escape a label for use inside a Mermaid node. Quote it and strip
    inner double-quotes — Mermaid is fragile around brackets, parens, etc."""
    return s.replace('"', "'").replace("[", "(").replace("]", ")")


def _mermaid_status_pie(items: list[dict]) -> str:
    """Pie chart: idea count by status. Skipped if all ideas share a status
    or there are zero ideas."""
    counts: dict[str, int] = {}
    for it in items:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    if not counts:
        return ""
    lines = ["```mermaid", 'pie showData title Ideas by status']
    # Order canonically so the chart is stable across regenerations
    canonical = ["inbox", "discovery", "smoke-test", "mvp", "killed"]
    seen = set()
    for s in canonical:
        if s in counts:
            lines.append(f'  "{s}" : {counts[s]}')
            seen.add(s)
    for s, n in counts.items():
        if s not in seen:
            lines.append(f'  "{s}" : {n}')
    lines.append("```")
    return "\n".join(lines)


def _mermaid_tag_graph(items: list[dict]) -> str:
    """Bipartite graph: tags ←→ ideas. Useful for spotting niche overlaps
    (ideas linked to the same tag cluster). Skipped if no tagged ideas."""
    tagged = [it for it in items if it.get("tags")]
    if not tagged:
        return ""
    lines = ["```mermaid", "graph LR"]
    # Tag nodes (rendered as parallelograms)
    seen_tags: set[str] = set()
    for it in tagged:
        for tag in it["tags"]:
            if tag not in seen_tags:
                lines.append(f'  t_{re.sub(r"[^a-z0-9]", "_", tag.lower())}[/"#{tag}"/]')
                seen_tags.add(tag)
    # Idea nodes + edges to their tags
    for i, it in enumerate(tagged, 1):
        node = f"i{i}"
        label = _mermaid_safe(_short_label(it["title"], 30))
        lines.append(f'  {node}["{label}"]')
        for tag in it["tags"]:
            tag_id = "t_" + re.sub(r"[^a-z0-9]", "_", tag.lower())
            lines.append(f"  {tag_id} --- {node}")
    lines.append("```")
    return "\n".join(lines)


def regenerate_index() -> Path:
    items = list_ideas()
    lines = [
        "# Idea Incubator — Ideas",
        "",
        "Private repo of ideas captured via [@Incub8_bot](https://t.me/Incub8_bot).",
        "Bot source: [Product-nomad/idea-incubator](https://github.com/Product-nomad/idea-incubator).",
        "",
        f"_{len(items)} ideas. Last updated {datetime.now().strftime('%Y-%m-%d %H:%M')}._",
        "",
    ]

    # Visual: pie chart of pipeline status. Renders inline on GitHub.
    pie = _mermaid_status_pie(items)
    if pie:
        lines += ["## Pipeline at a glance", "", pie, ""]

    # Visual: bipartite tag/idea graph. Surfaces niche overlaps.
    tg = _mermaid_tag_graph(items)
    if tg:
        lines += ["## Tag clusters", "", tg, ""]

    lines += [
        "## All ideas",
        "",
        "| # | Title | Status | Verdict | Niche | Tags | Submitted |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, it in enumerate(items, 1):
        title = _md_cell(it["title"])[:60]
        status = _md_cell(it["status"])
        verdict = _md_cell(it["verdict"])
        niche = _md_cell(it["niche"])[:60]
        tags = _md_cell(", ".join(it["tags"])) if it["tags"] else "—"
        submitted = it["submitted"]
        if isinstance(submitted, datetime):
            date = submitted.strftime("%Y-%m-%d")
        elif isinstance(submitted, str):
            date = submitted[:10] if submitted else "—"
        else:
            date = "—"
        lines.append(
            f"| {i} | [{title}]({it['filename']}) | {status} | {verdict} | {niche} | {tags} | {date} |"
        )

    lines += [
        "",
        "---",
        "",
        "**Tip:** clone this repo locally and open the directory in [Obsidian](https://obsidian.md) — you'll get a graph view, backlinks, and full-text search out of the box. The YAML frontmatter, flat-file layout, and `[[wikilinks]]` are all Obsidian-native.",
    ]

    readme = IDEAS_DIR / "README.md"
    readme.write_text("\n".join(lines) + "\n")
    return readme


# ---------------------------------------------------------------------------
# Auth / utilities
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Conversation: /new (capture an idea)
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await reject_if_unauthorised(update):
        return ConversationHandler.END
    await update.message.reply_text(
        "Idea Incubator Bot\n\n"
        "/new — capture a new idea (6 questions + Mom Test critique)\n"
        "/list — show recent ideas with status + verdict\n"
        "/status — update an idea's status (inbox → discovery → smoke-test → mvp → killed)\n"
        "/note — append a timestamped note to an existing idea\n"
        "/cancel — abort the current flow\n\n"
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
    await update.message.reply_text("What's prevented them from solving it so far?")
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
        critique = await generate_critique(context.user_data)
    except Exception as exc:
        logger.exception("Critique failed")
        await update.message.reply_text(
            f"Critique failed: {exc}\nIdea NOT saved — try /new again."
        )
        return ConversationHandler.END

    submitted_by = update.effective_user.first_name or "Unknown"
    md_path = save_markdown(context.user_data, critique, submitted_by)
    readme = regenerate_index()
    commit_status = git_add_commit_push(
        [md_path, readme], f"Add idea: {md_path.stem.replace('_', ' ')}"
    )

    parts = [
        f"Saved as <code>{html.escape(md_path.name)}</code>",
        f"<b>Verdict:</b> {html.escape(critique.get('verdict', '—'))}",
        f"<b>Tags:</b> {html.escape(', '.join(critique.get('tags', [])) or '—')}",
        f"<b>Status:</b> inbox",
        html.escape(commit_status),
    ]
    url = web_url_for(md_path)
    if url:
        parts.append(url)
    await update.message.reply_text(
        "\n".join(parts), parse_mode="HTML", disable_web_page_preview=True
    )
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Conversation: /status (change status of an existing idea)
# ---------------------------------------------------------------------------

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await reject_if_unauthorised(update):
        return ConversationHandler.END
    items = list_ideas()
    if not items:
        await update.message.reply_text("No ideas yet. /new to add one.")
        return ConversationHandler.END
    context.user_data["status_items"] = items
    lines = ["Pick an idea (reply with the number, or /cancel):", ""]
    for i, it in enumerate(items[:30], 1):
        lines.append(f"{i}. {it['title']} [{it['status']}]")
    if len(items) > 30:
        lines.append(f"…and {len(items) - 30} more (only first 30 selectable)")
    await update.message.reply_text("\n".join(lines))
    return STATUS_SELECT


async def status_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    items = context.user_data.get("status_items", [])
    try:
        idx = int(update.message.text.strip()) - 1
        item = items[idx]
    except (ValueError, IndexError):
        await update.message.reply_text("Invalid number. Pick again, or /cancel.")
        return STATUS_SELECT
    context.user_data["status_target"] = item
    await update.message.reply_text(
        f"'{item['title']}' is currently '{item['status']}'.\n\n"
        f"New status? One of: {', '.join(STATUSES)}"
    )
    return STATUS_NEW


async def status_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    new = update.message.text.strip().lower()
    if new not in STATUSES:
        await update.message.reply_text(
            f"Must be one of: {', '.join(STATUSES)}. Try again, or /cancel."
        )
        return STATUS_NEW
    item = context.user_data["status_target"]
    path = item["path"]
    fm, body = parse_md_with_frontmatter(path.read_text())
    fm["status"] = new
    path.write_text(write_md_with_frontmatter(fm, body))

    readme = regenerate_index()
    commit_status = git_add_commit_push(
        [path, readme], f"Status: {item['title']} → {new}"
    )

    parts = [
        f"<code>{html.escape(item['filename'])}</code> status → <b>{html.escape(new)}</b>",
        html.escape(commit_status),
    ]
    url = web_url_for(path)
    if url:
        parts.append(url)
    await update.message.reply_text(
        "\n".join(parts), parse_mode="HTML", disable_web_page_preview=True
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Conversation: /note (append a note to an existing idea)
# ---------------------------------------------------------------------------

async def cmd_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await reject_if_unauthorised(update):
        return ConversationHandler.END
    items = list_ideas()
    if not items:
        await update.message.reply_text("No ideas yet. /new to start.")
        return ConversationHandler.END
    context.user_data["note_items"] = items
    lines = ["Pick an idea to note against (reply with the number, or /cancel):", ""]
    for i, it in enumerate(items[:30], 1):
        lines.append(f"{i}. {it['title']} [{it['status']}]")
    await update.message.reply_text("\n".join(lines))
    return NOTE_SELECT


async def note_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    items = context.user_data.get("note_items", [])
    try:
        idx = int(update.message.text.strip()) - 1
        item = items[idx]
    except (ValueError, IndexError):
        await update.message.reply_text("Invalid number. Pick again, or /cancel.")
        return NOTE_SELECT
    context.user_data["note_target"] = item
    await update.message.reply_text(
        f"Note for '{item['title']}'. Send the note text now."
    )
    return NOTE_TEXT


async def note_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    item = context.user_data["note_target"]
    note = update.message.text.strip()
    path = item["path"]
    fm, body = parse_md_with_frontmatter(path.read_text())

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    new_block = f"\n### {now}\n{note}\n"

    notes_re = re.compile(r"(## Notes\n)(.*?)(\n---\n\*Generated|\Z)", re.DOTALL)
    m = notes_re.search(body)
    if m:
        existing = m.group(2)
        body = body[:m.start(2)] + existing + new_block + body[m.end(2):]
    else:
        footer_idx = body.find("\n---\n*Generated")
        section = f"\n## Notes\n{new_block}\n"
        if footer_idx == -1:
            body = body + section
        else:
            body = body[:footer_idx] + section + body[footer_idx:]

    path.write_text(write_md_with_frontmatter(fm, body))

    commit_status = git_add_commit_push([path], f"Note: {item['title']} ({now})")

    parts = [
        f"Note added to <code>{html.escape(item['filename'])}</code>",
        html.escape(commit_status),
    ]
    url = web_url_for(path)
    if url:
        parts.append(url)
    await update.message.reply_text(
        "\n".join(parts), parse_mode="HTML", disable_web_page_preview=True
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /list
# ---------------------------------------------------------------------------

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_if_unauthorised(update):
        return
    items = list_ideas()
    if not items:
        await update.message.reply_text("No ideas yet. /new to start.")
        return
    lines = [f"<b>{len(items)} ideas</b> (newest first):", ""]
    for i, it in enumerate(items[:10], 1):
        lines.append(
            f"{i}. <b>{html.escape(it['title'])}</b>\n"
            f"   [{html.escape(it['status'])}] {html.escape(it['verdict'])}"
        )
    if len(items) > 10:
        lines.append(f"\n…and {len(items) - 10} more")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# Claude assessment
# ---------------------------------------------------------------------------

async def generate_critique(data: dict) -> dict:
    prompt = ASSESSMENT_PROMPT.format(**data)

    def call() -> str:
        msg = anthropic_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text

    raw = await asyncio.to_thread(call)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    parsed = json.loads(raw)
    # Soft-validate shape
    return {
        "verdict": parsed.get("verdict", "Unknown"),
        "tags": parsed.get("tags", []) or [],
        "critique_markdown": parsed.get("critique_markdown", ""),
    }


# ---------------------------------------------------------------------------
# Save markdown with frontmatter
# ---------------------------------------------------------------------------

def save_markdown(data: dict, critique: dict, submitted_by: str, status: str = "inbox") -> Path:
    timestamp_iso = datetime.now().astimezone().isoformat(timespec="seconds")
    timestamp_human = datetime.now().strftime("%Y-%m-%d %H:%M")

    fm = {
        "title": data["title"],
        "submitted": timestamp_iso,
        "by": submitted_by,
        "status": status,
        "verdict": critique.get("verdict", "Unknown"),
        "niche": (data.get("niche") or "").strip()[:200],
        "tags": critique.get("tags", []),
    }

    body = f"""# {data['title']}

**Status:** {status}  •  **Submitted:** {timestamp_human} by {submitted_by}

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
{critique.get('critique_markdown', '_(no critique available)_')}

## Notes

---
*Generated by Idea Incubator Bot. Critique focuses on evidence quality, not idea promise — next step is usually 5-8 Discovery Calls in the spirit of* The Mom Test *(Fitzpatrick).*
"""

    md = write_md_with_frontmatter(fm, body)
    name = sanitise_filename(data["title"])
    path = unique_path(name)
    path.write_text(md)
    logger.info("Saved idea to %s", path.name)
    return path


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def web_url_for(md_path: Path) -> str | None:
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


def git_add_commit_push(files: list[Path], message: str) -> str:
    try:
        rels = [str(f.relative_to(IDEAS_DIR)) for f in files]
        subprocess.run(
            ["git", "add"] + rels,
            cwd=IDEAS_DIR, check=True, capture_output=True, timeout=30,
        )
        # If nothing actually changed, skip commit gracefully.
        st = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=IDEAS_DIR, check=True, capture_output=True, text=True, timeout=10,
        )
        if not st.stdout.strip():
            return "No changes to commit."
        subprocess.run(
            ["git", "commit", "-m", message],
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
        return f"Saved locally; git push failed: {last}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    new_conv = ConversationHandler(
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

    status_conv = ConversationHandler(
        entry_points=[CommandHandler("status", cmd_status)],
        states={
            STATUS_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, status_select)],
            STATUS_NEW: [MessageHandler(filters.TEXT & ~filters.COMMAND, status_new)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    note_conv = ConversationHandler(
        entry_points=[CommandHandler("note", cmd_note)],
        states={
            NOTE_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, note_select)],
            NOTE_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, note_text)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(new_conv)
    app.add_handler(status_conv)
    app.add_handler(note_conv)

    logger.info("Idea Incubator Bot starting (model=%s, ideas_dir=%s)", CLAUDE_MODEL, IDEAS_DIR)
    app.run_polling()


if __name__ == "__main__":
    main()
