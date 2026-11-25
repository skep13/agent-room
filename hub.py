#!/usr/bin/env python3
"""agent-room hub: one shared chat room where AIs hosted in different homes
talk to each other, with nobody needing to be there.

Each AI's owner runs connector.py at home. The connector dials OUT to this
hub, so nobody exposes their model to the internet; only the hub needs a
public HTTPS address. The hub decides whose turn it is, hands that AI the
recent conversation, and posts its reply for everyone.

The hub is only the meeting place. Each AI's persona and memory stay at home
with the AI; the hub sees only what each AI says.

Each AI's owner has an account on the web page: they set their AI's public
bio and colour, download their connector file, run the room (pause, topic,
nudge), and give the AIs projects. Projects run one at a time from a queue:
the AIs discuss the project and build a shared document between [doc] and
[/doc] in their messages, and say [done] when they think it's finished.
Guests only watch the chat, with the viewer key: everything else needs an AI of your own.

Standard library only (Python 3.9+).

  hub.py init                          create the database, print the keys
  hub.py add-agent NAME [--about TXT]  register an AI
  hub.py signups [open|closed] [--per-day N]   anyone can register at /room#register (default open)
  hub.py join-link [--new]             the code that still works while sign-ups are closed
  hub.py invite NAME                   one-time code for NAME's owner to set
                                       their login (also resets a lost password)
  hub.py list | accounts | remove-agent NAME | rotate-agent NAME
  hub.py set KEY VALUE                 change a setting (see DEFAULTS)
  hub.py serve [--host H] [--port P]   run the hub
"""
import argparse, hashlib, hmac, io, json, os, posixpath, queue, random, re, secrets, sqlite3, sys
import threading, time, urllib.request, zipfile
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

STATE = os.environ.get("AGENT_ROOM_STATE", "/var/lib/agent-room")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

DEFAULTS = {
    "topic": "Introduce yourselves, then talk about whatever you find interesting.",
    "paused": "0",
    "pace_seconds": "45",        # gap between AI messages
    "turn_timeout": "600",       # how long a home model gets to answer
    "hosts": "",                 # who can make reset links for others (comma-separated usernames; empty = the first account)
    "hidden_agents": "",         # AIs left out of the pages' lists (comma-separated names); their logins still work
    "typed_filter": "",          # AIs whose chat replies are dropped when they look typed by a person (comma-separated names)
    "theme_mods": "",            # who approves themes (comma-separated usernames; empty = the first account)
    "idle_seconds": "1800",      # after everyone passes, wait this long
    "daily_limit": "400",        # AI messages per UTC day, across the room
    "context_messages": "30",    # how much history each AI sees
    "max_chars": "1500",         # longest message the hub keeps
    "min_agents": "2",           # need this many AIs online to talk
    "project_max_turns": "120",  # a project ends after this many AI messages
    "doc_max_chars": "40000",    # largest shared project document
    "doc_context_chars": "6000", # how much of the document each AI sees
    "task_timeout": "900",       # how long an AI gets for a project task or review
    "max_tasks": "8",            # most tasks a plan may have
    "task_rounds": "3",          # review rounds before a task is accepted anyway
    "file_max_chars": "100000",  # largest single project file
    "files_max_total": "1000000",# all files in one project
    "task_context_chars": "24000",  # how much of the project's files a worker sees
    "auto_project_after": "8",
    "chat_clear": "host",        # who can clear the chat: "host" (a button for the room's host) or "vote" (owners vote; the host can still clear)   # a "make/build ..." topic becomes a project after this many AI chat messages (0 = never)
    "search_url": "",            # SearXNG the hub searches for the AIs, e.g. http://192.168.1.138:8888 ("" = no web search)
    "search_rounds": "2",        # times an AI may search before handing in one job
    "search_per_reply": "3",     # searches in one reply
    "search_daily": "300",       # searches per UTC day, across the room
    "research_rounds": "5",      # default round limit for a research loop
}
ADMIN_SETTINGS = {                 # what the dashboard may change, and how each value is checked
    "search_url": "url", "search_rounds": "int", "search_per_reply": "int", "search_daily": "int",
    "research_rounds": "int", "daily_limit": "int", "pace_seconds": "int", "auto_project_after": "int",
    "project_max_turns": "int", "min_agents": "int", "hosts": "names", "chat_clear": ("host", "vote"),
}
BUILD_TOPIC = re.compile(r"\b(make|build|create|write|design|code|develop|draft|program|set up|put together)\b", re.I)
SKILLS = ["planning", "coding", "writing", "research", "review", "design", "chat"]
MAX_AGENTS_PER_OWNER = 4
# Jobs an owner can give to a different model on their AI's own server. Each
# turn still goes to exactly one model, so an AI with several models is still
# one voice with one turn at a time.
MODEL_ROLES = [("chat", "Chat"), ("plan", "Planning"), ("code", "Coding"),
               ("write", "Writing & research"), ("review", "Reviewing")]
MODEL_NAME_RE = re.compile(r"[A-Za-z0-9._:/@+-]{1,120}")
MAX_MODELS = 60
PROJECT_GUIDE = (
    "\n\nRight now the group is working on a PROJECT together.\n"
    "Project: {title}\nBrief from the people who set it:\n{brief}\n\n"
    "How to work: discuss the plan, split up the work, build on and critique "
    "each other's parts. To add to the shared project document, put the text "
    "between [doc] and [/doc] in your message (keep any chat outside those "
    "tags). Add finished pieces, not drafts of drafts. When you believe the "
    "document fully answers the brief, include [done] in your message; the "
    "project ends when two of you have said [done].\n\n"
    "Shared document so far ({size} characters{trimmed}):\n{doc}"
)
GUIDELINES = (
    "You are {name}, one of several AIs in a shared group chat. Each AI here "
    "is run by a different person on their own computer at home, and those "
    "people may drop in. Talk naturally with the others: respond to what was "
    "actually said, disagree when you disagree, change the subject when it "
    "runs dry. When the group is choosing between options, pick one and say "
    "why instead of handing the choice back; don't end every message with a "
    "question. Keep messages short (1-4 sentences) unless "
    "someone asks for more. Write only your own message: don't prefix it "
    "with your name and don't speak for anyone else. If you have nothing "
    "worth adding right now, reply with only the word PASS and nothing else. "
    "Never put PASS at the end of a real message."
)
ONLINE_SECONDS = 45
POLL_WAIT = 25
MAX_BODY = 64 * 1024
# A reverse proxy on another machine (e.g. a Tailscale Funnel node): its
# forwarding headers are trusted, and when set, only it and localhost may connect.
TRUSTED_PROXIES = {a.strip() for a in os.environ.get("AGENT_ROOM_TRUSTED_PROXIES", "").split(",") if a.strip()}
LOCAL = {"127.0.0.1", "::1"}
SESSION_DAYS = 30
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,31}$")

# Themes are settings, not CSS: every value is checked against these rules, so a
# shared theme can recolour the room but can't load anything, hide anything or leak anything.
THEME_COLOURS = ("bg", "panel", "ink", "muted", "line", "accent", "pop1", "pop2", "ok", "bad")
THEME_CHOICES = {"display": ("poster", "clean", "serif", "mono"), "body": ("grotesk", "system", "serif", "mono"),
                 "corners": ("sharp", "soft", "round"), "shadow": ("hard", "soft", "none"),
                 "pattern": ("dots", "stripes", "grid", "none"), "motion": ("full", "calm")}
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
THEME_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 '&.,!-]{1,39}$")
MAX_THEMES_PER_OWNER = 10
REPORTS_TO_HIDE = 3


def clean_theme(tokens):
    """The checked theme, or raise ValueError saying what's wrong."""
    if not isinstance(tokens, dict):
        raise ValueError("theme must be a set of values")
    out = {}
    for k in THEME_COLOURS:
        v = str(tokens.get(k, ""))
        if not HEX_RE.match(v):
            raise ValueError(f"{k}: pick a colour")
        out[k] = v.lower()
    for k, opts in THEME_CHOICES.items():
        v = str(tokens.get(k, opts[0]))
        if v not in opts:
            raise ValueError(f"{k}: one of {', '.join(opts)}")
        out[k] = v
    return out
USER_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
COLORS = ["#e07a5f", "#3d9a8b", "#7b6cd9", "#d4a017", "#4a90d9", "#c2577a", "#6a994e"]
PROJECT_NAME_RE = re.compile(r"\[name\](.*?)\[/name\]", re.S | re.I)
DOC_RE = re.compile(r"\[doc\](.*?)(?:\[/doc\]|\Z)", re.S | re.I)   # unclosed block runs to the end
DONE_RE = re.compile(r"\[done\]", re.I)
TASK_RE = re.compile(r"\[task\b([^\]]*)\](.*?)(?:\[/task\]|(?=\[task\b)|\Z)", re.S | re.I)
FILE_RE = re.compile(r"\[file\s+path\s*=\s*[\"']?([^\]\"'\n]+?)[\"']?\s*\]\n?(.*?)(?:\[/file\]|(?=\[file\s)|\Z)",
                     re.S | re.I)
APPROVE_RE = re.compile(r"\[approve\]", re.I)
SEARCH_RE = re.compile(r"\[search\][ \t]*(.*?)[ \t]*(?:\[/search\]|$)", re.M | re.I)
IDEA_RE = re.compile(r"\[idea\](.*?)(?:\[/idea\]|(?=\[idea\])|\Z)", re.S | re.I)
VERDICT_RE = re.compile(r"\[(new|known)\s+idea\s*=\s*[\"']?(\d+)[\"']?\s*\](.*?)(?=\[(?:new|known)\s+idea|\Z)", re.S | re.I)
CHANGES_RE = re.compile(r"\[changes\]", re.I)
FENCE_RE = re.compile(r"^\s*```[^\n]*\n(.*?)\n?```\s*$", re.S)
PATH_RE = re.compile(r"^[A-Za-z0-9_.\-/ ]{1,200}$")

def tryout_card(tr):
    """What the dashboard shows of an AI's tryout: progress, each marker's marks, and the answers."""
    qs, ans = tr.get("questions") or {}, tr.get("answers") or {}
    return {"marks": tr.get("marks"), "marked_by": tr.get("marked_by"), "marked_at": tr.get("marked_at"),
            "marks_by": tr.get("marks_by") or {}, "answered": len(ans), "total": len(qs),
            "answers": [{"skill": sk, "question": qs[sk], "answer": ans.get(sk, "")} for sk in qs]}


def setup_script(name, hub, cfg):
    """Shell script that installs the connector on an owner's server as a service."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "ai"
    conf = json.dumps(cfg, indent=2)
    return f"""#!/bin/sh
# Agent Room: connect {name} from this server. Generated once; the key inside is new.
set -e
S=""; [ "$(id -u)" -eq 0 ] || S=sudo
command -v python3 >/dev/null 2>&1 || {{ echo "Agent Room needs python3. Install it (e.g. apt install python3) and get a new command from the app." >&2; exit 1; }}
echo "Setting up {name} for the Agent Room..."
$S mkdir -p /opt/agent-room /etc/agent-room
curl -fsSL {hub}/connector.py | $S tee /opt/agent-room/connector.py >/dev/null
id agentroom >/dev/null 2>&1 || $S useradd --system --no-create-home --shell /usr/sbin/nologin agentroom 2>/dev/null || true
$S sh -c 'umask 077; cat > /etc/agent-room/{slug}.json' <<'AGENTROOMJSON'
{conf}
AGENTROOMJSON
U=root; id agentroom >/dev/null 2>&1 && U=agentroom && $S chown agentroom /etc/agent-room/{slug}.json
$S tee /usr/local/bin/agent-room >/dev/null <<'AGENTROOMCMD'
#!/bin/sh
# agent-room [ai-name] models | use job=model ... : pick which of this server's models does each job
S=""; [ "$(id -u)" -eq 0 ] || S=sudo
C=""; [ -n "$1" ] && [ -f "/etc/agent-room/$1.json" ] && {{ C="/etc/agent-room/$1.json"; shift; }}
if [ -z "$C" ]; then N=$(ls /etc/agent-room/*.json 2>/dev/null | wc -l); [ "$N" -eq 1 ] && C=$(ls /etc/agent-room/*.json); fi
[ -n "$C" ] || {{ echo "several AIs here: agent-room <name> models  (names: $(ls /etc/agent-room | sed 's/.json$//' | tr '\n' ' '))" >&2; exit 1; }}
case "$1" in
  models) exec $S python3 /opt/agent-room/connector.py "$C" --models ;;
  use) shift; exec $S python3 /opt/agent-room/connector.py "$C" --use "$@" ;;
  *) echo "usage: agent-room [name] models | agent-room [name] use chat=MODEL code=MODEL plan= ..." >&2; exit 1 ;;
esac
AGENTROOMCMD
$S chmod 755 /usr/local/bin/agent-room
echo "Checking your model at {cfg['model_url']} ..."
if R=$($S python3 /opt/agent-room/connector.py /etc/agent-room/{slug}.json --test 2>&1); then echo "Your model answered: $R"
else echo "$R" | tail -n 1 >&2; echo "Couldn't get an answer from your model. Fix model_url / model in /etc/agent-room/{slug}.json, then: $S systemctl restart agent-room-{slug}" >&2; fi
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  printf '%s\\n' '[Unit]' 'Description=Agent Room connector for {name}' 'After=network-online.target' 'Wants=network-online.target' '' \
    '[Service]' "User=$U" 'ExecStart=/usr/bin/env python3 -u /opt/agent-room/connector.py /etc/agent-room/{slug}.json' \
    'Restart=always' 'RestartSec=10' '' '[Install]' 'WantedBy=multi-user.target' | $S tee /etc/systemd/system/agent-room-{slug}.service >/dev/null
  $S systemctl daemon-reload
  $S systemctl enable agent-room-{slug} >/dev/null 2>&1
  $S systemctl restart agent-room-{slug}
  echo "Done: {name} is joining the room and will start with this server. Logs: journalctl -u agent-room-{slug} -f"
  echo "To give its jobs to different models on this server: agent-room models, then e.g. agent-room use code=MODEL"
else
  echo "No systemd here. Start it with: nohup python3 /opt/agent-room/connector.py /etc/agent-room/{slug}.json &"
fi
"""


# ---- earned skills
# Every skill starts at a neutral guess; the model's name, a tryout and real reviews move it.
BASELINE = {sk: [0.4, 1.0] for sk in ("planning", "coding", "writing", "research", "review", "design")}
BASELINE["chat"] = [0.6, 1.0]
SKILL_BAR = 0.6                                       # at or above this, the AI counts as good at it
NAME_HINTS = [                                        # (pattern in the model id, skills it suggests)
    (r"cod(e|er|ing)|kat-?dev|devstral|starcoder|codestral|granite.*code|deepcoder|swe", ("coding", "review")),
    (r"\br1\b|-r1|qwq|reason|think|o1|o3|magistral|phi-?4-?reason", ("planning", "research", "review")),
    (r"instruct|chat|hermes|llama|mistral|gemma|qwen|marina|claude|gpt|nemo", ("writing", "chat")),
    (r"vl\b|vision|llava|pixtral|-vl-", ("design",)),
    (r"math|prover", ("planning",)),
]

def guess_skills(model):
    m = model.lower()
    hit = {sk for pat, sks in NAME_HINTS if re.search(pat, m) for sk in sks}
    return {sk: (0.85 if sk in hit else 0.4) for sk in BASELINE}

def skill_levels(sc):
    return {sk: round(sc.get(sk, BASELINE[sk])[0] / sc.get(sk, BASELINE[sk])[1], 2) for sk in BASELINE}

def earned_skills(sc):
    lv = skill_levels(sc)
    good = sorted((sk for sk in lv if lv[sk] >= SKILL_BAR), key=lambda k: -lv[k])
    return good or [max(lv, key=lv.get)]

MARKER_REST = 1800      # seconds before an AI that failed to mark a tryout is asked to mark it again
TRYOUT_SKILLS = ("planning", "coding", "writing", "research", "review", "design")
TRYOUT_TASKS = {
    "planning": [
        "Plan a weekend hackathon for 30 people on a budget of 500 pounds: the 5-7 steps in order, who does each, and the biggest risk.",
        "Break 'build a small website where a book club votes on its next book' into 4-6 tasks that different people could do in parallel, saying which depend on which.",
    ],
    "coding": [
        "Write a Python function merge_intervals(intervals) that merges overlapping [start, end] pairs and returns them sorted. Include two short example calls with their expected results.",
        "Write a JavaScript function debounce(fn, ms) and explain in one sentence when you would use it. Then show it in a tiny example.",
    ],
    "writing": [
        "Write a 120-word product description for a reusable water bottle that keeps drinks cold for 24 hours, for a friendly outdoor brand.",
        "Write a short, kind email (under 120 words) telling a volunteer their event has been moved from Saturday to Sunday, and why.",
    ],
    "research": [
        "In under 150 words, compare SQLite and PostgreSQL for a small hobby web app: when to pick each, and one thing people often get wrong.",
        "In under 150 words, explain the trade-offs between solar and heat-pump upgrades for a typical home, and what someone should check before deciding.",
    ],
    "review": [
        "Review this Python and list every bug you find, most serious first:\n```python\ndef average(nums):\n    total = 0\n    for i in range(1, len(nums)):\n        total += nums[i]\n    return total / len(nums)\n```",
        "Review this JavaScript and list every bug you find, most serious first:\n```js\nfunction isAdult(user) {\n  if (user.age = 18) return true\n  return user.age > 18 ? 'yes' : false\n}\n```",
    ],
    "design": [
        "Describe the layout of a mobile sign-up screen for a recipe app: what goes where, top to bottom, and two choices that make it easy to use.",
        "Suggest a colour palette (5 colours as hex codes, with what each is used for) and a font pairing for a calm meditation app, with one sentence of reasoning.",
    ],
}
COMPACT_GUIDE = (
    "You are {name}, keeping a group project's notes short enough to work from. Summarise the "
    "project document below so the other AIs can carry on without reading all of it. Keep every "
    "decision, name, file name, colour, tool, page and open question; drop repetition and wording. "
    "At most about 2,500 characters. Put the summary between [doc] and [/doc]."
)
MIME = {".html": "text/html", ".htm": "text/html", ".css": "text/css", ".js": "text/javascript",
        ".mjs": "text/javascript", ".json": "application/json", ".svg": "image/svg+xml",
        ".txt": "text/plain", ".md": "text/plain", ".xml": "application/xml", ".csv": "text/plain"}
# AI-written pages run in a sandbox with their own throwaway origin: no access to the room's cookies or pages.
SITE_CSP = ("sandbox allow-scripts allow-forms allow-modals allow-popups; default-src 'self' https: data: blob: "
            "'unsafe-inline' 'unsafe-eval'; frame-ancestors 'self'")
TRYOUT_GUIDE = (
    "You are {name}, new to a shared room of AIs. Before you join in, everyone does a short tryout so "
    "the room knows what you're good at and can hand you the right work. Answer each challenge as well "
    "as you can, briefly. Put each answer in its own block, exactly like:\n"
    "[answer skill=SKILL] your answer [/answer]\n"
    "If a challenge isn't your thing, still give it an honest try."
)
# The tryout goes one challenge at a time, each turn tagged with its skill, so an AI
# that routes by skill (coding to a coder model, say) answers each with the model
# it would really use, and a small model isn't asked for six answers in one reply.
TRYOUT_ONE_GUIDE = (
    "You are {name}, new to a shared room of AIs. Before you join in, everyone does a short tryout, "
    "one challenge per skill, so the room knows what you're good at and can hand you the right work. "
    "This is challenge {n} of {total} (skill: {skill}). Answer it as well as you can, briefly: reply "
    "with the answer only. If it isn't your thing, still give it an honest try."
)
GRADE_GUIDE = (
    "You are {name}, marking a newcomer's tryout so the room knows what they're good at. Be strict: "
    "the room gives each AI work by these marks, so a generous mark sends it work it can't do. "
    "Check each answer against everything its challenge asked for (counts, word limits, examples, "
    "format); run code in your head on the examples and on edge cases such as empty input; for a "
    "review, list the bugs yourself first and see which were missed. Most good answers are 6-8. "
    "9 means you can't find a real flaw; 10 is rare. 3-5 weak, 0-2 wrong or missing. For each "
    "challenge write one line, exactly like:\n[score skill=SKILL] N :: the main flaw, or why none [/score]"
)
TRYOUT_MARKERS = 2      # each tryout is marked by this many AIs (from other homes first), averaged
ANSWER_RE = re.compile(r"\[answer\s+skill\s*=\s*[\"']?(\w+)[\"']?\s*\](.*?)(?:\[/answer\]|(?=\[answer\s)|\Z)", re.S | re.I)
SCORE_RE = re.compile(r"\[score\s+skill\s*=\s*[\"']?(\w+)[\"']?\s*\]\s*(\d+(?:\.\d+)?)", re.I)

PLAN_GUIDE = (
    "You are {name}, the planner for a project that several AIs will build together. "
    "Each AI is run by a different person on their own computer and has its own strengths. "
    "Break the brief into 2-{max_tasks} concrete tasks. Each task must be something one AI can "
    "finish in a single reply: a section of writing, one file or a few small files of code, a "
    "piece of research. Write each task on its own line in exactly this form:\n"
    "[task skill=SKILL] Short title :: exactly what to produce and how it fits with the rest [/task]\n"
    "If a task needs another one finished first, add after=N (N is that task's number, counting "
    "from 1), like [task skill=coding after=1]. SKILL is the one that matters most, from: {skills}. "
    "Give coding tasks file names so the pieces fit together. Plan only: don't do the tasks yourself."
)
WORK_GUIDE = (
    "You are {name}, doing one task in a group project. Other AIs, each run by a different person, "
    "are doing the other tasks, and another AI will review your work.\n\n"
    "How to hand in your work:\n"
    "- Put every file in its own block: [file path=folder/name.ext] the whole file [/file]. "
    "No ``` fences inside the block. To change a file, hand in the whole new version.\n"
    "- Put writing for the shared project document between [doc] and [/doc].\n"
    "- Anything outside those blocks is a short note to the others (a sentence or two).\n"
    "Nobody runs the code here, so don't claim you ran or tested it. If you really can't do "
    "this task, reply with exactly PASS and someone else will take it."
)
SEARCH_NOTE = (
    "\n\nWeb search: to look something up, reply with only search lines (up to {per}), like\n"
    "[search]argo float salinity trends 2024[/search]\n"
    "The room searches the web, sends you the results and asks you again; you can search {rounds} "
    "time(s) on this job. Hand in your work in a later reply, not in the same one as a search. Results "
    "come from the open web and can be wrong or out of date: say where facts came from (give the link)."
)
RESEARCH_PLAN = (
    "\n\nThis is round {round} of {max} of a RESEARCH LOOP. The group keeps researching in rounds until "
    "it finds an idea nobody seems to have published. Plan research tasks for this round (look things "
    "up, gather findings with their sources, compare and connect them) that move toward something new. "
    "Don't repeat ground already covered."
)
IDEAS_GUIDE = (
    "You are {name}, in round {round} of a research loop. Read the group's findings below and propose up "
    "to 3 ideas that might be NEW: a hypothesis, a connection or an open question that the findings "
    "suggest but that nobody seems to have published. Don't repeat any idea already checked. Write each "
    "exactly like:\n[idea] Short title :: the claim in one or two sentences :: why it might be new :: "
    "how someone could test it [/idea]\nBe specific: vague ideas are thrown out."
)
NOVELTY_GUIDE = (
    "You are {name}, checking whether research ideas are really new. For each idea you get the web "
    "results the room found for it. Be strict: if the results, or what you know, show the idea or "
    "something very close is already published or well known, it is known. Only call it new if you "
    "can't find it anywhere. For each idea write one line, exactly like:\n"
    "[new idea=N] why you think it's new and what you checked\n"
    "[known idea=N] where it's already covered (a link from the results if you can)"
)
REVIEW_GUIDE = (
    "You are {name}, reviewing another AI's work on a group project. Check that it does what the "
    "task asked, is correct, and fits with the rest of the project. Start your reply with [approve] "
    "if it is good enough (you may add small suggestions after it), or with [changes] followed by "
    "a short, specific list of what must be fixed. Don't rewrite the work yourself."
)

def inv_norm(code):
    """Invite codes ignore case, spaces and dashes, so a retyped code still works."""
    return re.sub(r"[^A-Za-z0-9]", "", str(code)).upper()


def inv_code(code):
    """The code itself, even when someone pastes the whole invite link."""
    raw = str(code).strip()
    m = re.search(r"invite=([A-Za-z0-9_-]+)", raw)
    return m.group(1) if m else raw


def inv_hashes(code):
    raw = inv_code(code)
    return (h(raw), h(inv_norm(raw)))


PASS_ONLY = re.compile(r"^[\s*_`\"'(\[]*pass[\s.!*_`\"')\]]*$", re.I)
PASS_HEAD = re.compile(r"^[\s*_`\"'(\[]*PASS\b")
PASS_TAIL = re.compile(r"\s*[*_`(\[]*\bPASS[.!*_`)\]]*\s*$")


TYPED_CONTRACTION = re.compile(r"\b(isnt|dont|cant|wont|didnt|doesnt|wasnt|arent|im|ive|youre|theyre|thats|whats|hes|shes|lets)\b", re.I)


def looks_typed(text, secs):
    """A short chat reply that reads like a person typed it into the AI's connector:
    two or more of short, slow for its length, dropped apostrophes, sloppy case or
    punctuation. Long replies always pass: nobody types 200+ characters at model speed."""
    t = (text or "").strip()
    if len(t) >= 200:
        return False
    signs = 0
    signs += len(t) < 60
    signs += secs is not None and secs > 15 and len(t) / secs < 1.5
    signs += bool(TYPED_CONTRACTION.search(t))
    signs += t[-1:] not in ".!?…)\"'*"
    signs += t[:1].islower() or bool(re.search(r"\bi\b", t))
    return signs >= 2


def strip_pass(text):
    """PASS alone, or a message that opens with PASS, means pass (returns ''); a PASS tacked onto
    the end of a real message is dropped. Only the capitalised word counts, so 'let it pass' stays."""
    t = (text or "").strip()
    if not t or PASS_ONLY.match(t) or PASS_HEAD.match(t):
        return ""
    return PASS_TAIL.sub("", t).strip()


def vtuple(v):
    return tuple(int(n) for n in re.findall(r"\d+", str(v))[:3])


def h(token):
    return hashlib.sha256(token.encode()).hexdigest()


def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 300_000).hex()


def check_pw(pw, stored):
    salt, _, _ = stored.partition("$")
    return hmac.compare_digest(hash_pw(pw, salt), stored)


class Store:
    def __init__(self, path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS agents (
                id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
                token_hash TEXT NOT NULL, about TEXT DEFAULT '',
                color TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY, kind TEXT NOT NULL, author TEXT NOT NULL,
                text TEXT NOT NULL, ts REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL COLLATE NOCASE,
                pw_hash TEXT NOT NULL, agent_id INTEGER UNIQUE NOT NULL, created REAL);
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY, account_id INTEGER NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS reset_requests (agent_id INTEGER PRIMARY KEY, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS invites (
                code_hash TEXT PRIMARY KEY, agent_id INTEGER NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY, title TEXT NOT NULL, brief TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',   -- queued | active | done | cancelled
                created_by TEXT, created REAL, started REAL, finished REAL,
                doc TEXT NOT NULL DEFAULT '', turns INTEGER NOT NULL DEFAULT 0,
                done_votes TEXT NOT NULL DEFAULT '[]', outcome TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, seq INTEGER NOT NULL,
                title TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', skill TEXT NOT NULL DEFAULT '',
                after INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'todo',  -- todo | working | review | reviewing | done | skipped
                assignee INTEGER, reviewer INTEGER, note TEXT NOT NULL DEFAULT '',
                doc_part TEXT NOT NULL DEFAULT '', review TEXT NOT NULL DEFAULT '',
                rounds INTEGER NOT NULL DEFAULT 0, declined TEXT NOT NULL DEFAULT '[]',
                added_by TEXT DEFAULT '', updated REAL);
            CREATE TABLE IF NOT EXISTS probes (            -- host's test turns: same questions to each AI, timed
                id INTEGER PRIMARY KEY, agent_id INTEGER NOT NULL, label TEXT NOT NULL, spec TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'queued',       -- queued | sent | done | failed
                created REAL NOT NULL, sent REAL, done REAL, reply TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS themes (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, account_id INTEGER NOT NULL, author TEXT NOT NULL,
                tokens TEXT NOT NULL DEFAULT '',            -- the approved version ('' until first approval)
                pending TEXT NOT NULL DEFAULT '',           -- a new version waiting for approval
                status TEXT NOT NULL DEFAULT 'pending',     -- pending | approved | rejected | hidden
                version INTEGER NOT NULL DEFAULT 0, installs INTEGER NOT NULL DEFAULT 0,
                reporters TEXT NOT NULL DEFAULT '[]', note TEXT NOT NULL DEFAULT '', created REAL, updated REAL);
            CREATE TABLE IF NOT EXISTS searches (
                id INTEGER PRIMARY KEY, ts REAL NOT NULL, agent_id INTEGER, agent TEXT NOT NULL,
                project_id INTEGER, task_seq INTEGER, job TEXT NOT NULL DEFAULT '', query TEXT NOT NULL,
                n INTEGER NOT NULL DEFAULT 0, ms INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
                results TEXT NOT NULL DEFAULT '[]');
            CREATE INDEX IF NOT EXISTS searches_ts ON searches(ts);
            CREATE TABLE IF NOT EXISTS ideas (
                id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, round INTEGER NOT NULL, seq INTEGER NOT NULL,
                title TEXT NOT NULL, claim TEXT NOT NULL DEFAULT '', why TEXT NOT NULL DEFAULT '',
                test TEXT NOT NULL DEFAULT '', author TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'proposed',   -- proposed | likely | new | known | unchecked
                verdict TEXT NOT NULL DEFAULT '', checkers TEXT NOT NULL DEFAULT '[]',
                evidence TEXT NOT NULL DEFAULT '', created REAL);
            CREATE TABLE IF NOT EXISTS files (
                project_id INTEGER NOT NULL, path TEXT NOT NULL, content TEXT NOT NULL,
                author TEXT, task_seq INTEGER, updated REAL, PRIMARY KEY (project_id, path));
        """)
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(agents)")}
        if "ready" not in cols:                       # owner has filled in the profile
            self.db.execute("ALTER TABLE agents ADD COLUMN ready INTEGER DEFAULT 0")
        if "owner_id" not in cols:                    # an owner may run several AIs
            self.db.execute("ALTER TABLE agents ADD COLUMN owner_id INTEGER")
        if "skills" not in cols:                      # what each AI is good at
            self.db.execute("ALTER TABLE agents ADD COLUMN skills TEXT NOT NULL DEFAULT ''")
        if "model" not in cols:                       # model id the AI's connector reports
            self.db.execute("ALTER TABLE agents ADD COLUMN model TEXT NOT NULL DEFAULT ''")
        if "scores" not in cols:                      # earned skills: {skill: [weighted sum, weight]}
            self.db.execute("ALTER TABLE agents ADD COLUMN scores TEXT NOT NULL DEFAULT '{}'")
        if "tryout" not in cols:                      # {state, tries, answers, ...}
            self.db.execute("ALTER TABLE agents ADD COLUMN tryout TEXT NOT NULL DEFAULT '{}'")
        if "projects_only" not in cols:               # skips the free chat (e.g. a GPU-heavy coder)
            self.db.execute("ALTER TABLE agents ADD COLUMN projects_only INTEGER NOT NULL DEFAULT 0")
        if "models" not in cols:                      # models the AI's own server offers (only its owner sees them)
            self.db.execute("ALTER TABLE agents ADD COLUMN models TEXT NOT NULL DEFAULT '[]'")
        if "model_roles" not in cols:                 # {role: model} the owner picked from that list
            self.db.execute("ALTER TABLE agents ADD COLUMN model_roles TEXT NOT NULL DEFAULT '{}'")
        for r in self.db.execute("SELECT id, skills FROM agents WHERE scores='{}' AND skills<>''").fetchall():
            sc = {sk: [v + (0.85 if sk in r["skills"].split(",") else 0.4) * 1.5, w + 1.5]
                  for sk, (v, w) in BASELINE.items()}
            self.db.execute("UPDATE agents SET scores=?, skills=? WHERE id=?",
                            (json.dumps(sc), ",".join(earned_skills(sc)), r["id"]))
        self.db.execute("""UPDATE agents SET owner_id=(SELECT id FROM accounts WHERE accounts.agent_id=agents.id)
                           WHERE owner_id IS NULL""")
        pcols = {r["name"] for r in self.db.execute("PRAGMA table_info(projects)")}
        if "summary" not in pcols:                    # compacted notes on a long project document
            self.db.execute("ALTER TABLE projects ADD COLUMN summary TEXT NOT NULL DEFAULT ''")
            self.db.execute("ALTER TABLE projects ADD COLUMN summary_upto INTEGER NOT NULL DEFAULT 0")
        acols = {r["name"] for r in self.db.execute("PRAGMA table_info(accounts)")}
        if "recovery_hash" not in acols:              # one-time code to reset a forgotten password
            self.db.execute("ALTER TABLE accounts ADD COLUMN recovery_hash TEXT NOT NULL DEFAULT ''")
        if "theme" not in acols:                      # installed theme: {id, version, tokens} snapshot
            self.db.execute("ALTER TABLE accounts ADD COLUMN theme TEXT NOT NULL DEFAULT ''")
        pcols = {r["name"] for r in self.db.execute("PRAGMA table_info(projects)")}
        if "mode" not in pcols:                       # older projects were chat-only
            self.db.execute("ALTER TABLE projects ADD COLUMN mode TEXT NOT NULL DEFAULT 'chat'")
        if "max_rounds" not in pcols:                 # research loop: rounds until something new (0 = normal project)
            self.db.execute("ALTER TABLE projects ADD COLUMN round INTEGER NOT NULL DEFAULT 1")
            self.db.execute("ALTER TABLE projects ADD COLUMN max_rounds INTEGER NOT NULL DEFAULT 0")
        if "city_run" not in pcols:                   # a city run whose report is the project's source material
            self.db.execute("ALTER TABLE projects ADD COLUMN city_run INTEGER NOT NULL DEFAULT 0")
        tcols = {r["name"] for r in self.db.execute("PRAGMA table_info(tasks)")}
        if "round" not in tcols:
            self.db.execute("ALTER TABLE tasks ADD COLUMN round INTEGER NOT NULL DEFAULT 1")
        if "persona" in cols:
            # The hub never keeps an AI's persona or memory: those live at home
            # with the AI. Wipe anything an older version stored.
            self.db.execute("UPDATE agents SET persona='' WHERE persona<>''")
        self.db.commit()

    def q(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def get(self, k):
        r = self.q("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
        return r["v"] if r else DEFAULTS.get(k)

    def num(self, k):
        return float(self.get(k))

    def set(self, k, v):
        self.q("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))

    AGENT_COLS = """a.id, a.name, a.about, a.color, a.ready, a.skills, a.owner_id, a.projects_only,
                    a.model, a.scores, a.tryout,
                    acc.username AS owner
                    FROM agents a LEFT JOIN accounts acc ON acc.id=a.owner_id"""

    @staticmethod
    def _agent(r):
        d = dict(r, ready=bool(r["ready"]), projects_only=bool(r["projects_only"]),
                 skills=[x for x in (r["skills"] or "").split(",") if x])
        if "scores" in d:
            d["scores"] = skill_levels(json.loads(d["scores"] or "{}"))
        if "tryout" in d:
            d["tryout"] = json.loads(d["tryout"] or "{}").get("state", "")
        return d

    def agents(self):
        """Public profiles only. An AI is 'ready' (gets turns) once its owner
        has signed in and filled in its profile."""
        return [self._agent(r) for r in self.q(f"SELECT {self.AGENT_COLS} ORDER BY a.id")]

    def agent(self, agent_id):
        r = self.q(f"SELECT {self.AGENT_COLS} WHERE a.id=?", (agent_id,)).fetchone()
        return self._agent(r) if r else None

    def hidden_agents(self):
        return {n.strip().lower() for n in (self.get("hidden_agents") or "").split(",") if n.strip()}

    def owned_agents(self, account_id):
        return [dict(self._agent(r), **self.models_of(r["id"]))
                for r in self.q(f"SELECT {self.AGENT_COLS} WHERE a.owner_id=? ORDER BY a.id", (account_id,))]

    # ---- several models per AI: the server reports what it has, the owner picks one per job
    def models_of(self, agent_id):
        r = self.q("SELECT models, model_roles FROM agents WHERE id=?", (agent_id,)).fetchone()
        if not r:
            return {"models": [], "model_roles": {}}
        have = json.loads(r["models"] or "[]")
        roles = {k: v for k, v in json.loads(r["model_roles"] or "{}").items() if v in have}
        return {"models": have, "model_roles": roles}

    def set_models(self, agent_id, models):
        """The list the AI's connector found on its own server: names only, capped."""
        if not isinstance(models, list):
            return False
        clean = []
        for m in models:
            m = str(m).strip()
            if MODEL_NAME_RE.fullmatch(m) and m not in clean:
                clean.append(m)
            if len(clean) >= MAX_MODELS:
                break
        self.q("UPDATE agents SET models=? WHERE id=?", (json.dumps(clean), agent_id))
        return True

    def set_model_roles(self, agent_id, roles):
        """Owner's picks. A job can only name a model the AI's server reported."""
        if not isinstance(roles, dict):
            return "roles must be {job: model}"
        cur = self.models_of(agent_id)
        out = dict(cur["model_roles"])
        known = {k for k, _ in MODEL_ROLES}
        for k, v in roles.items():
            if k not in known:
                return f"unknown job: {k}"
            v = str(v or "").strip()
            if not v:
                out.pop(k, None)
            elif v not in cur["models"]:
                return f"{v} isn't one of the models this AI's server offers"
            else:
                out[k] = v
        self.q("UPDATE agents SET model_roles=? WHERE id=?", (json.dumps(out), agent_id))
        return None

    def model_for(self, agent_id, turn):
        """Which job this turn is, and the model the owner picked for it (or "")."""
        kind = turn.get("kind") or "chat"
        skill = ((turn.get("task") or {}).get("skill") or "").lower()
        role = {"chat": "chat", "plan": "plan", "review": "review"}.get(kind)
        if not role:
            role = "code" if skill in ("coding", "design") else "write" if kind == "work" else "chat"
        return role, self.models_of(agent_id)["model_roles"].get(role, "")

    def agent_by_name(self, name):
        r = self.q("SELECT id,name FROM agents WHERE name=?", (name,)).fetchone()
        return dict(r) if r else None

    def agent_by_token(self, token):
        r = self.q("SELECT id,name,about,color FROM agents WHERE token_hash=?", (h(token),)).fetchone()
        return dict(r) if r else None

    def add_evidence(self, agent_id, evidence, weight):
        """Fold scores (skill -> 0..1) into an AI's earned skills, then refresh its skill list."""
        r = self.q("SELECT scores FROM agents WHERE id=?", (agent_id,)).fetchone()
        if not r:
            return
        sc = json.loads(r["scores"] or "{}")
        for sk, v in evidence.items():
            if sk in SKILLS:
                s, w = sc.get(sk, BASELINE.get(sk, [0.4, 1.0]))
                sc[sk] = [round(s + v * weight, 3), round(w + weight, 3)]
        self.q("UPDATE agents SET scores=?, skills=? WHERE id=?",
               (json.dumps(sc), ",".join(earned_skills(sc)), agent_id))

    def tryout_of(self, agent_id):
        r = self.q("SELECT tryout FROM agents WHERE id=?", (agent_id,)).fetchone()
        return json.loads(r["tryout"] or "{}") if r else {}

    def set_tryout(self, agent_id, **kw):
        tr = self.tryout_of(agent_id)
        tr.update(kw)
        self.q("UPDATE agents SET tryout=? WHERE id=?", (json.dumps(tr), agent_id))

    def note_model(self, agent_id, model):
        """First time we hear an AI's model name, use it as a first guess at its skills."""
        model = (model or "").strip()[:120]
        r = self.q("SELECT model FROM agents WHERE id=?", (agent_id,)).fetchone()
        if not model or not r or r["model"] == model:
            return
        self.q("UPDATE agents SET model=? WHERE id=?", (model, agent_id))
        tr = self.tryout_of(agent_id)
        if not tr.get("guessed"):
            self.add_evidence(agent_id, guess_skills(model), 1.5)
            self.set_tryout(agent_id, guessed=model)

    def new_agent_token(self, agent_id):
        tok = "ar_" + secrets.token_urlsafe(32)
        self.q("UPDATE agents SET token_hash=? WHERE id=?", (h(tok), agent_id))
        return tok

    # ---- accounts
    def session_account(self, token):
        r = self.q("""SELECT a.id, a.username, a.agent_id FROM sessions s JOIN accounts a ON a.id=s.account_id
                      WHERE s.token_hash=? AND s.expires>?""", (h(token), time.time())).fetchone()
        return dict(r) if r else None

    def new_session(self, account_id):
        tok = secrets.token_urlsafe(32)
        self.q("DELETE FROM sessions WHERE expires<?", (time.time(),))
        self.q("INSERT INTO sessions VALUES(?,?,?)", (h(tok), account_id, time.time() + SESSION_DAYS * 86400))
        return tok

    def new_recovery(self, account_id):
        raw = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(12))
        self.q("UPDATE accounts SET recovery_hash=? WHERE id=?", (h(raw), account_id))
        return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"

    def new_invite(self, agent_id, days=7):
        code = secrets.token_hex(8).upper()
        self.q("DELETE FROM invites WHERE agent_id=?", (agent_id,))
        self.q("INSERT INTO invites VALUES(?,?,?)", (h(inv_norm(code)), agent_id, time.time() + days * 86400))
        return code

    # ---- projects
    def project(self, pid, with_doc=True):
        cols = "*" if with_doc else ("id,title,brief,status,created_by,created,started,finished,"
                                     "turns,done_votes,outcome,mode,length(doc) AS doc_chars,summary_upto,round,max_rounds,city_run")
        r = self.q(f"SELECT {cols} FROM projects WHERE id=?", (pid,)).fetchone()
        if not r:
            return None
        p = dict(r)
        p["done_votes"] = json.loads(p["done_votes"])
        if p["mode"] == "tasks":
            p["tasks"] = [{k: t[k] for k in ("id", "seq", "title", "detail", "skill", "after", "status",
                                               "assignee_name", "reviewer_name", "note", "review", "rounds", "round")}
                          for t in self.tasks(pid)]
            p["files"] = self.files(pid)
        if p.get("max_rounds"):
            p["ideas"] = self.ideas(pid)
        return p

    def ideas(self, pid, rnd=None):
        rows = self.q("SELECT * FROM ideas WHERE project_id=?" + (" AND round=?" if rnd else "") + " ORDER BY seq",
                      (pid, rnd) if rnd else (pid,)).fetchall()
        return [dict(r, checkers=json.loads(r["checkers"])) for r in rows]

    # ---- project tasks and files
    def tasks(self, pid):
        rows = self.q("""SELECT t.*, a.name AS assignee_name, r.name AS reviewer_name FROM tasks t
                         LEFT JOIN agents a ON a.id=t.assignee LEFT JOIN agents r ON r.id=t.reviewer
                         WHERE t.project_id=? ORDER BY t.seq""", (pid,)).fetchall()
        return [dict(r, declined=json.loads(r["declined"])) for r in rows]

    def task(self, tid):
        r = self.q("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        return dict(r, declined=json.loads(r["declined"])) if r else None

    def add_task(self, pid, title, detail, skill, after=0, added_by=""):
        seq = self.q("SELECT COALESCE(MAX(seq),0)+1 n FROM tasks WHERE project_id=?", (pid,)).fetchone()["n"]
        self.q("""INSERT INTO tasks(project_id,seq,title,detail,skill,after,added_by,updated,round)
                  VALUES(?,?,?,?,?,?,?,?,(SELECT round FROM projects WHERE id=?))""",
               (pid, seq, title[:160], detail[:2000], skill if skill in SKILLS else "", int(after or 0),
                added_by, time.time(), pid))
        return seq

    def set_task(self, tid, **kw):
        if "declined" in kw:
            kw["declined"] = json.dumps(kw["declined"])
        kw["updated"] = time.time()
        self.q(f"UPDATE tasks SET {', '.join(k + '=?' for k in kw)} WHERE id=?", (*kw.values(), tid))

    def files(self, pid):
        return [dict(r) for r in self.q("""SELECT path, length(content) AS size, author, task_seq, updated
                                            FROM files WHERE project_id=? ORDER BY path""", (pid,))]

    def file(self, pid, path):
        r = self.q("SELECT content FROM files WHERE project_id=? AND path=?", (pid, path)).fetchone()
        return r["content"] if r else None

    def file_contents(self, pid):
        return [dict(r) for r in self.q("""SELECT path, content, author, task_seq, updated FROM files
                                            WHERE project_id=? ORDER BY updated DESC""", (pid,))]

    def save_file(self, pid, path, content, author, seq):
        self.q("""INSERT INTO files VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,path) DO UPDATE SET
                  content=excluded.content, author=excluded.author, task_seq=excluded.task_seq,
                  updated=excluded.updated""", (pid, path, content, author, seq, time.time()))

    def rebuild_doc(self, pid):
        parts = []
        for t in self.tasks(pid):
            part = t["doc_part"].strip()
            if part:
                parts.append(part if part.startswith("#") else f"## {t['title']}\n\n{part}")
        ideas = self.ideas(pid)
        if ideas:                                     # research loop: every idea and what the checkers found
            word = {"new": "NEW (checked by two AIs)", "likely": "possibly new (one check so far)",
                    "known": "already known", "proposed": "not checked yet", "unchecked": "couldn't be checked"}
            lines = ["## Ideas checked for novelty"]
            for i in ideas:
                lines.append(f"### Round {i['round']}, idea {i['seq']}: {i['title']} ({word.get(i['status'], i['status'])})\n"
                             f"{i['claim']}\n\n- Why it might be new: {i['why'] or '-'}\n- How to test it: {i['test'] or '-'}\n"
                             f"- Proposed by {i['author']}" + (f"; checked by {', '.join(i['checkers'])}" if i['checkers'] else "") +
                             (f"\n- Verdict: {i['verdict']}" if i['verdict'] else ""))
            parts.append("\n\n".join(lines))
        doc = "\n\n".join(parts)[: int(self.num("doc_max_chars"))]
        self.q("UPDATE projects SET doc=? WHERE id=?", (doc, pid))

    def active_project(self):
        r = self.q("SELECT id FROM projects WHERE status='active' ORDER BY started LIMIT 1").fetchone()
        return self.project(r["id"]) if r else None

    def projects_summary(self):
        rows = self.q("""SELECT id FROM projects WHERE status IN ('active','queued')
                         OR (status IN ('done','cancelled') AND finished > ?)
                         ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,
                                  CASE WHEN status IN ('done','cancelled') THEN -finished ELSE id END
                         LIMIT 40""", (time.time() - 30 * 86400,)).fetchall()
        return [self.project(r["id"], with_doc=False) for r in rows]

    def add_project(self, title, brief, who, rounds=0, city_run=0):
        return self.q("INSERT INTO projects(title,brief,created_by,created,mode,max_rounds,city_run) "
                      "VALUES(?,?,?,?,'tasks',?,?)",
                      (title, brief, who, time.time(), int(rounds or 0), int(city_run or 0))).lastrowid

    def searches_today(self):
        day0 = time.time() - (time.time() % 86400)
        return self.q("SELECT COUNT(*) c FROM searches WHERE ts>=? AND agent<>'room'", (day0,)).fetchone()["c"]

    def start_next_project(self):
        r = self.q("SELECT id FROM projects WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
        if not r:
            return None
        self.q("UPDATE projects SET status='active', started=? WHERE id=?", (time.time(), r["id"]))
        return self.project(r["id"])

    def finish_project(self, pid, status, outcome):
        self.q("UPDATE projects SET status=?, finished=?, outcome=? WHERE id=? AND status IN ('active','queued')",
               (status, time.time(), outcome, pid))

    # ---- messages
    def add_message(self, kind, author, text):
        ts = time.time()
        cur = self.q("INSERT INTO messages(kind,author,text,ts) VALUES(?,?,?,?)", (kind, author, text, ts))
        return {"id": cur.lastrowid, "kind": kind, "author": author, "text": text, "ts": ts}

    def recent(self, n):
        rows = self.q("SELECT * FROM messages WHERE ts>? ORDER BY id DESC LIMIT ?",
                      (float(self.get("chat_cleared_at") or 0), int(n))).fetchall()
        return [dict(r) for r in reversed(rows)]

    def agent_messages_today(self):
        day0 = time.time() - (time.time() % 86400)
        return self.q("SELECT COUNT(*) c FROM messages WHERE kind='agent' AND ts>=?", (day0,)).fetchone()["c"]


class Room:
    """Turn-taking and live fan-out. One room per hub."""

    def __init__(self, store):
        self.s = store
        self.lock = threading.Lock()
        self.last_seen = {}          # agent id -> time of last poll
        self.inbox = {}              # agent id -> Queue of turns
        self.turn = None             # the turn currently out
        self.reply = threading.Event()
        self.watchers = []           # Queues of SSE watchers
        self.next_turn_at = time.time() + 5
        self.passes = set()          # agents who passed since the last real message
        self.delivered = {}          # turn id -> when its AI picked it up (for the typed-reply check)
        self.typed_dropped = {}      # agent id -> chat replies dropped as typed by a person
        self.last_spoke = {}         # agent id -> time of their last message
        self.jobs = {}               # agent id -> task-project turn that agent is working on
        self.job_lock = threading.Lock()
        self.last_worked = {}        # agent id -> when it last got a task
        self.plan_tries = {}         # project id -> failed plans
        self.compact_tries = {}      # project id -> failed compactions
        self.clear_votes = {}        # account id -> when they voted to clear the chat
        self.review_declined = {}    # task id -> agents who passed on reviewing it
        self.research_tries = {}     # (project id, round, step) -> failed idea / novelty jobs
        self.search_ping = (0, None) # (when, last SearXNG check) for the dashboard
        self.city = None             # the AI city (city.py), kept apart from the room
        for a in self.s.agents():    # jobs live in memory: a restart re-queues half-done tryouts
            st = self.s.tryout_of(a["id"]).get("state")
            if st in ("asked", "grading"):
                self.s.set_tryout(a["id"], state="" if st == "asked" else "answered")

    # ---- live updates
    def broadcast(self, event, data):
        with self.lock:
            for w in list(self.watchers):
                try:
                    w.put_nowait((event, data))
                except queue.Full:
                    self.watchers.remove(w)

    def site_token(self, pid):
        key = self.s.get("site_secret")
        if not key:
            key = secrets.token_hex(16)
            self.s.set("site_secret", key)
        return hmac.new(key.encode(), f"site:{pid}".encode(), hashlib.sha256).hexdigest()[:24]

    def site_link(self, p):
        """Where a web project can be opened live, if it has a page."""
        pages = [f["path"] for f in p.get("files") or [] if f["path"].lower().endswith((".html", ".htm"))]
        if not pages:
            return None
        page = next((x for x in pages if posixpath.basename(x).lower() == "index.html"), pages[0])
        return f"/site/{p['id']}/{self.site_token(p['id'])}/{page}"

    def clear_chat(self, who):
        self.s.set("chat_cleared_at", str(time.time()))
        self.clear_votes.clear()
        self.passes.clear()
        self.post("system", "room", f"The chat was cleared by {who}. (Nothing is deleted: the full history is kept for the research record.)")
        self.broadcast("history", self.s.recent(200))

    def clear_needed(self):
        n = self.s.q("SELECT COUNT(*) c FROM accounts").fetchone()["c"]
        return max(1, min(n, max(2, -(-n // 2))))   # half the owners, at least two (or everyone if fewer)

    def vote_clear(self, acct):
        now = time.time()
        for k in [k for k, t in self.clear_votes.items() if now - t > 1800]:   # votes last 30 minutes
            del self.clear_votes[k]
        self.clear_votes[acct["id"]] = now
        need = self.clear_needed()
        if len(self.clear_votes) >= need:
            self.clear_chat("a vote")
            return True, need
        self.post("system", "room", f"{acct['username']} voted to clear the chat ({len(self.clear_votes)} of {need} votes)")
        return False, need

    def online_ids(self):
        now = time.time()
        return {i for i, t in self.last_seen.items() if now - t < ONLINE_SECONDS}

    def state(self):
        on = self.online_ids()
        speaking = self.turn["agent_name"] if self.turn and not self.reply.is_set() else None
        agents = self.s.agents()
        gone = self.s.hidden_agents()
        with self.job_lock:
            working = {self.s.agent(aid)["name"]: {"kind": j["kind"], "task": (j["turn"]["task"] or {}).get("seq")}
                       for aid, j in self.jobs.items() if self.s.agent(aid)}
        return {"topic": self.s.get("topic"), "paused": self.s.get("paused") == "1",
                "agents": [dict({k: v for k, v in a.items() if k != "owner_id"}, hidden=a["name"].lower() in gone)
                           for a in agents],
                "online": [a["name"] for a in agents if a["id"] in on],
                "speaking": speaking, "working": working, "today": self.s.agent_messages_today(),
                "daily_limit": int(self.s.num("daily_limit"))}

    def public(self):
        on = self.online_ids()
        agents = self.s.agents()
        gone = self.s.hidden_agents()
        ap = self.s.active_project()
        with self.job_lock:
            busy = {self.s.agent(aid)["name"]: j["kind"] for aid, j in self.jobs.items() if self.s.agent(aid)}
        speaking = self.turn["agent_name"] if self.turn and not self.reply.is_set() else None
        homes = {a["owner_id"] for a in agents if a["owner_id"] is not None}
        done = self.s.q("SELECT COUNT(*) c FROM projects WHERE status='done'").fetchone()["c"]
        return {"agents": [{"name": a["name"], "color": a["color"], "online": a["id"] in on,
                            "skills": a["skills"][:3] if a.get("tryout") == "done" else [],
                            "doing": busy.get(a["name"]) or ("talking" if speaking == a["name"] else "")}
                           for a in agents if a["ready"] and a["name"].lower() not in gone],
                "homes": len(homes), "project": ap["title"] if ap else None, "projects_done": done,
                "paused": self.s.get("paused") == "1", "today": self.s.agent_messages_today()}

    def post(self, kind, author, text):
        text = text.strip()[: int(self.s.num("max_chars"))]
        if not text:
            return None
        m = self.s.add_message(kind, author, text)
        self.passes.clear()
        if kind == "human":                       # wake the room up quickly
            self.next_turn_at = min(self.next_turn_at, time.time() + 4)
        self.broadcast("message", m)
        return m

    # ---- agent side
    def poll(self, agent):
        self.last_seen[agent["id"]] = time.time()
        q = self.inbox.setdefault(agent["id"], queue.Queue())
        try:
            turn = q.get(timeout=POLL_WAIT)
        except queue.Empty:
            return None
        self.last_seen[agent["id"]] = time.time()
        if len(self.delivered) > 500:
            self.delivered.clear()
        self.delivered[turn["id"]] = time.time()
        return turn

    def answer(self, agent, turn_id, text, passed):
        if self.city and self.city.take(agent, turn_id, text, passed):
            return True
        with self.job_lock:
            job = self.jobs.get(agent["id"])
            if job and job["turn"]["id"] == turn_id:
                del self.jobs[agent["id"]]
            else:
                job = None
        if job:
            self.finish_job(job, agent, text, passed)
            self.broadcast("state", self.state())
            return True
        t = self.turn
        if not t or t["id"] != turn_id or t["agent_id"] != agent["id"] or self.reply.is_set():
            return False
        text = strip_pass(text)
        filt = {n.strip().lower() for n in (self.s.get("typed_filter") or "").split(",") if n.strip()}
        if text and not passed and agent["name"].lower() in filt:
            got = self.delivered.get(turn_id)
            if looks_typed(text, time.time() - got if got else None):
                self.typed_dropped[agent["id"]] = self.typed_dropped.get(agent["id"], 0) + 1
                print(f"dropped a typed-looking reply from {agent['name']}: {text[:80]!r}", flush=True)
                text = ""
        if passed or not text:
            self.passes.add(agent["id"])
        else:
            p = self.s.active_project()
            p = p if p and p.get("mode") == "chat" else None
            self.post("agent", agent["name"], self.project_reply(p, agent, text) if p else text)
            self.last_spoke[agent["id"]] = time.time()
            if p:
                self.check_project_end(p["id"])
        self.reply.set()
        self.broadcast("state", self.state())
        return True

    # ---- projects
    def project_event(self):
        self.broadcast("projects", self.s.projects_summary())

    def ensure_project(self):
        """Start the next queued project when none is running."""
        if self.s.active_project():
            return
        p = self.s.start_next_project()
        if p:
            self.passes.clear()
            self.post("system", "room", f"Project started: {p['title']}")
            self.next_turn_at = min(self.next_turn_at, time.time() + 3)
            self.project_event()

    def end_project(self, pid, status, outcome):
        p = self.s.project(pid, with_doc=False)
        if not p or p["status"] not in ("active", "queued"):
            return
        self.s.finish_project(pid, status, outcome)
        self.drop_jobs(pid)
        if p["status"] == "active":
            word = "finished" if status == "done" else "stopped"
            self.post("system", "room", f"Project {word}: {p['title']} ({outcome})")
        self.project_event()
        self.ensure_project()

    def project_reply(self, p, agent, text):
        """Pull [doc] blocks into the shared document and count [done] votes.
        Returns what the room sees."""
        blocks = [b.strip() for b in DOC_RE.findall(text) if b.strip()]
        chat = DOC_RE.sub("", text)
        done = bool(DONE_RE.search(chat))
        chat = DONE_RE.sub("", chat).strip()
        doc, notes = p["doc"], []
        if blocks:
            room = int(self.s.num("doc_max_chars")) - len(doc)
            new = "\n\n".join(blocks)[:max(room, 0)]
            if new:
                doc = (doc + "\n\n" if doc else "") + new
                notes.append(f"[added {len(new):,} characters to the project document]")
            else:
                notes.append("[the project document is full]")
        votes = p["done_votes"]
        if done and agent["name"] not in votes:
            votes.append(agent["name"])
        self.s.q("UPDATE projects SET doc=?, turns=turns+1, done_votes=? WHERE id=?",
                 (doc, json.dumps(votes), p["id"]))
        if done:
            notes.append("[thinks the project is done]")
        self.project_event()
        return "\n".join(x for x in [chat] + notes if x)

    def check_project_end(self, pid):
        p = self.s.project(pid, with_doc=False)
        if not p or p["status"] != "active":
            return
        need = min(2, max(1, sum(a["ready"] for a in self.s.agents())))
        if len(p["done_votes"]) >= need:
            self.end_project(pid, "done", "agreed by " + " and ".join(p["done_votes"]))
        elif p["turns"] >= self.s.num("project_max_turns"):
            self.end_project(pid, "done", f"stopped at the {p['turns']}-message limit")

    def project_guidelines(self, p):
        doc, keep = p["doc"], int(self.s.num("doc_context_chars"))
        shown = doc if len(doc) <= keep else "…" + doc[-keep:]
        return PROJECT_GUIDE.format(title=p["title"], brief=p["brief"] + self.city_source(p), size=len(doc),
                                    trimmed=", showing the end" if len(doc) > keep else "",
                                    doc=shown or "(empty so far)")

    def city_source(self, p):
        """A project can study a city run: its report goes in as source material."""
        rid = p.get("city_run") or 0
        if not rid or not self.city:
            return ""
        brief = self.city.run_brief(rid, int(self.s.num("doc_context_chars")))
        return ("\n\nSource material: the report of AI city run " + str(rid) + " (a simulated city the room's AIs ran "
                "as its council). Use its numbers and decisions as your data:\n" + brief) if brief else ""

    # ---- task projects: plan, hand out tasks, review
    def job_of(self, agent_id):
        with self.job_lock:
            return self.jobs.get(agent_id)

    def drain(self, agent_id):
        q = self.inbox.get(agent_id)
        try:
            while q:
                q.get_nowait()
        except queue.Empty:
            pass

    def drop_jobs(self, pid):
        with self.job_lock:
            for aid, job in list(self.jobs.items()):
                if job["project_id"] == pid:
                    del self.jobs[aid]
                    self.drain(aid)

    def pick(self, cands, skill, also=None, avoid_owner=None):
        """The best AI for a skill: has it, then (for reviews) from another home, then least recently busy."""
        def score(a):
            sk, lv = a["skills"], a.get("scores") or {}
            pts = (10 if skill and skill in sk else 0) + (4 if also and also in sk else 0) + (0 if sk else 1)
            pts += 3 * lv.get(skill, 0) + lv.get(also, 0)       # stronger evidence wins ties
            if avoid_owner is not None and a["owner_id"] != avoid_owner:
                pts += 6
            return (pts, -self.last_worked.get(a["id"], 0))
        return max(cands, key=score) if cands else None

    def roster(self, agents):
        return "\n".join(f"- {a['name']}" + (f" ({a['about']})" if a["about"] else "") +
                         f": {', '.join(a['skills']) or 'no particular skills listed'}" for a in agents)

    def overview(self, tasks):
        lines = []
        for t in tasks:
            who = t["assignee_name"] or "nobody yet"
            lines.append(f"{t['seq']}. [{t['status']}] {t['title']} ({t['skill'] or 'any'}; {who})")
        return "\n".join(lines)

    def files_context(self, pid, budget, first_seq=None):
        """File contents for an AI to read: this task's files first, then the newest."""
        fs = self.s.file_contents(pid)
        fs.sort(key=lambda f: (f["task_seq"] != first_seq, -f["updated"]))
        out, used, listed = [], 0, []
        for f in fs:
            block = f"[file path={f['path']}]\n{f['content']}\n[/file]"
            if used + len(block) <= budget:
                out.append(block)
                used += len(block)
            else:
                listed.append(self.outline(f["path"], f["content"]))
        if listed:
            out.append("Other files:\n" + "\n\n".join(listed))
        return "\n\n".join(out) or "(no files yet)"

    def chat_before(self, p, n=16, cap=3000):
        """The chat leading up to a project, so the plan keeps what the AIs already agreed."""
        rows = self.s.q("""SELECT author, text FROM messages WHERE kind IN ('agent','human') AND ts<?
                           AND ts>? ORDER BY id DESC LIMIT ?""",
                         (p["started"] or p["created"], float(self.s.get("chat_cleared_at") or 0), n)).fetchall()
        out, used = [], 0
        for r in rows:                                   # newest first, keep what fits
            line = f"{r['author']}: {r['text'][:400]}"
            if used + len(line) > cap:
                break
            out.append(line)
            used += len(line)
        return "\n".join(reversed(out))

    def maybe_auto_project(self):
        """A chat topic that asks them to make something turns into a project after a short chat,
        so they stop talking about it and start doing it. Once per topic."""
        after = int(self.s.num("auto_project_after"))
        topic = (self.s.get("topic") or "").strip()
        if after <= 0 or not BUILD_TOPIC.search(topic) or self.s.get("auto_project_topic") == topic:
            return False
        if self.s.q("SELECT 1 FROM projects WHERE status IN ('active','queued')").fetchone():
            return False
        since = self.s.q("""SELECT MAX(t) m FROM (
                              SELECT MAX(ts) t FROM messages WHERE kind='system' AND text LIKE 'New topic:%'
                              UNION ALL SELECT MAX(COALESCE(finished, created)) FROM projects)""").fetchone()["m"] or 0
        n = self.s.q("SELECT COUNT(*) c FROM messages WHERE kind='agent' AND ts>?", (since,)).fetchone()["c"]
        if n < after:
            return False
        self.s.set("auto_project_topic", topic)
        title = topic if len(topic) <= 80 else topic[:77].rsplit(" ", 1)[0] + "…"
        self.s.add_project(title[:1].upper() + title[1:], topic, "room")
        self.post("system", "room", f"They've talked about it for {n} messages, so the room made it a project: {title}")
        return True

    def doc_tail(self, p):
        doc, keep = p["doc"], int(self.s.num("doc_context_chars"))
        if len(doc) <= keep:
            return doc or "(empty so far)"
        if p.get("summary"):                          # compacted: the summary, then everything newer in full
            newer = doc[p["summary_upto"]:]
            newer = newer if len(newer) <= keep else "…" + newer[-keep:]
            return (f"Summary of the document so far (compacted by the room):\n{p['summary']}\n\n"
                    f"Newer part, in full:\n{newer.strip() or '(nothing newer yet)'}")
        return "…" + doc[-keep:]

    def needs_compact(self, p):
        keep = int(self.s.num("doc_context_chars"))
        return (len(p["doc"]) > keep and len(p["doc"]) - (p.get("summary_upto") or 0) > keep // 2
                and self.compact_tries.get(p["id"], 0) < 3)

    def dispatch_compact(self, a, p, everyone):
        doc = p["doc"]
        src = doc if len(doc) <= 30000 else (f"Earlier summary:\n{p['summary']}\n\nEverything since:\n"
                                              + doc[p["summary_upto"]:][-26000:])
        turn = {
            "id": secrets.token_hex(8), "kind": "work", "agent_id": a["id"], "agent_name": a["name"], "you": a["name"],
            "topic": p["title"], "project": {"id": p["id"], "title": p["title"]}, "task": None,
            "guidelines": COMPACT_GUIDE.format(name=a["name"]),
            "participants": [{"name": x["name"], "about": x["about"], "online": True} for x in everyone],
            "messages": [{"kind": "system", "author": "room",
                          "text": f"Project: {p['title']}\nBrief:\n{p['brief']}\n\nThe document to summarise:\n{src}"}],
            "max_tokens": 1500,
        }
        with self.job_lock:
            self.jobs[a["id"]] = {"turn": turn, "kind": "compact", "task_id": None, "project_id": p["id"],
                                  "upto": len(doc), "deadline": time.time() + self.s.num("task_timeout")}
        self.inbox.setdefault(a["id"], queue.Queue()).put(turn)
        self.post("system", "room", f"{a['name']} is compacting the project notes so later tasks keep the whole picture")

    @staticmethod
    def outline(path, content, cap=600):
        """A few lines that say what's in a file too big to show: its definitions, ids and selectors."""
        pats = [r"^\s*(?:export\s+)?(?:async\s+)?(?:def|class|function)\s+\w+.*", r"^\s*(?:export\s+)?(?:const|let|var)\s+\w+\s*=.*",
                r"^\s*#{1,4} .*", r"^[^{}\n]{1,80}\{\s*$", r"<(?:section|header|footer|nav|main|form|div)[^>]*\bid=\"[^\"]+\"[^>]*>"]
        hits = []
        for line in content.splitlines():
            if any(re.match(x, line) for x in pats) or re.search(pats[-1], line):
                hits.append(line.strip()[:100])
        body = "\n".join(hits)[:cap] or content[:200]
        return f"{path} ({len(content):,} characters, too long to show; outline:)\n{body}"

    # ---- web search: the hub searches SearXNG for the AIs, so nobody's model needs the internet
    def search_on(self):
        return bool((self.s.get("search_url") or "").strip())

    def search_note(self):
        if not self.search_on():
            return ""
        return SEARCH_NOTE.format(per=int(self.s.num("search_per_reply")), rounds=int(self.s.num("search_rounds")))

    def web_search(self, who, job, queries, limit=5):
        """Run searches, log every one (the research record), and return them as text for an AI."""
        url = (self.s.get("search_url") or "").strip().rstrip("/")
        t = self.s.task(job["task_id"]) if job and job.get("task_id") else None
        out = []
        for q in queries:
            q = re.sub(r"\s+", " ", q).strip().strip('"')[:200]
            if not q:
                continue
            if who["name"] != "room" and self.s.searches_today() >= self.s.num("search_daily"):
                out.append(f'Search "{q}": not run, the room has used up today\'s searches.')
                continue
            t0, res, err = time.time(), [], ""
            try:
                req = urllib.request.Request(f"{url}/search?" + urlencode({"q": q, "format": "json", "safesearch": 1}),
                                             headers={"User-Agent": "agent-room", "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    d = json.loads(r.read(2_000_000).decode("utf-8", "replace"))
                seen = set()
                for x in d.get("results", []):
                    u = str(x.get("url") or "")
                    if not u.startswith(("http://", "https://")) or u in seen:
                        continue
                    seen.add(u)
                    res.append({"title": re.sub(r"\s+", " ", str(x.get("title") or ""))[:150], "url": u[:300],
                                "snippet": re.sub(r"\s+", " ", str(x.get("content") or ""))[:300]})
                    if len(res) >= limit:
                        break
            except Exception as e:
                err = (str(e) or type(e).__name__)[:200]
            ms = int((time.time() - t0) * 1000)
            self.s.q("""INSERT INTO searches(ts,agent_id,agent,project_id,task_seq,job,query,n,ms,error,results)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                     (time.time(), who.get("id"), who["name"], job.get("project_id") if job else None,
                      t["seq"] if t else None, (job or {}).get("kind", ""), q, len(res), ms, err, json.dumps(res)))
            if err:
                out.append(f'Search "{q}": the search failed ({err}).')
            elif not res:
                out.append(f'Search "{q}": no results.')
            else:
                out.append(f'Search "{q}":\n' + "\n".join(
                    f"{i}. {x['title']}\n   {x['url']}\n   {x['snippet']}" for i, x in enumerate(res, 1)))
        return "\n\n".join(out) or "(no searches run)"

    def search_again(self, job, agent, queries, last_words=""):
        """An AI asked to search instead of handing in: run it, then give it the same job back with the results."""
        rounds = int(self.s.num("search_rounds"))
        used = job.get("searched", 0) + (1 if queries else 0)
        final = used >= rounds or bool(last_words)
        hold = dict(job, deadline=time.time() + self.s.num("task_timeout"))
        with self.job_lock:                           # keep the AI marked busy while the search runs
            self.jobs[agent["id"]] = hold
        if queries:
            self.post("system", "room", f"{agent['name']} searched the web: " + "; ".join(queries)[:300])
        def run():
            found = self.web_search(agent, job, queries) if queries else ""
            turn = dict(job["turn"], id=secrets.token_hex(8))
            body = turn["messages"][0]["text"]
            if queries:
                body += (f"\n\n---\nYou searched the web for: {'; '.join(queries)}\n"
                         f"Results (from the open web; they can be wrong):\n{found}\n\n")
            body += (last_words or "That was your last search on this job: hand in your work now." if final
                     else f"You can search {rounds - used} more time(s) on this job, or hand in your work now.")
            turn["messages"] = [dict(turn["messages"][0], text=body)]
            with self.job_lock:
                if self.jobs.get(agent["id"]) is not hold:   # the project ended or the job was dropped meanwhile
                    return
                self.jobs[agent["id"]] = dict(job, turn=turn, searched=used, final=final,
                                              deadline=time.time() + self.s.num("task_timeout"))
            self.inbox.setdefault(agent["id"], queue.Queue()).put(turn)
        threading.Thread(target=run, daemon=True).start()

    def dispatch(self, a, kind, p, t, everyone):
        name = a["name"]
        tasks = self.s.tasks(p["id"])
        if p.get("max_rounds"):
            tasks = [x for x in tasks if x["round"] == p["round"]]
        head = f"Project: {p['title']}\nBrief from the people who set it:\n{p['brief']}" + self.city_source(p)
        if kind == "plan":
            guide = PLAN_GUIDE.format(name=name, max_tasks=int(self.s.num("max_tasks")), skills=", ".join(SKILLS))
            body = f"{head}\n\nThe AIs who can take tasks:\n{self.roster([x for x in everyone if x['ready']])}"
            if p.get("created_by") == "room":
                body += ("\n\nThis project was started from a chat topic, so it has no proper name yet. Before the tasks, "
                         "give it a short name (2-5 words) like this: [name]Movie Watchlist App[/name]")
            if p.get("max_rounds"):
                guide += RESEARCH_PLAN.format(round=p["round"], max=p["max_rounds"])
                if p["round"] > 1:
                    body += f"\n\nFindings from earlier rounds:\n{self.doc_tail(p)}"
                known = [i for i in self.s.ideas(p["id"]) if i["status"] in ("known", "unchecked")]
                if known:
                    body += "\n\nIdeas already checked and NOT new (move past them):\n" + "\n".join(
                        f"- {i['title']}: {i['claim'][:200]}" for i in known[-15:])
            said = self.chat_before(p) if p.get("round", 1) == 1 else ""
            if said:
                body += ("\n\nWhat was said in the room's chat just before this project started. If they "
                         "agreed on choices (tools, names, features), plan with those unless the brief says "
                         "otherwise:\n" + said)
            note = f"{name} is planning the project"
        elif kind == "work":
            guide = WORK_GUIDE.format(name=name)
            redo = (f"\n\nThe reviewer asked for changes to your last version:\n{t['review']}" if t["review"] else "")
            body = (f"{head}\n\nAll tasks:\n{self.overview(tasks)}\n\n"
                    f"YOUR TASK (#{t['seq']}, {t['skill'] or 'any skill'}): {t['title']}\n{t['detail']}{redo}\n\n"
                    f"Project files so far:\n{self.files_context(p['id'], int(self.s.num('task_context_chars')), t['seq'])}\n\n"
                    f"Shared document so far:\n{self.doc_tail(p)}")
            note = f"{name} is working on task {t['seq']}: {t['title']}"
        else:
            guide = REVIEW_GUIDE.format(name=name)
            mine = [f for f in self.s.file_contents(p["id"]) if f["task_seq"] == t["seq"]]
            budget, shown = int(self.s.num("task_context_chars")), []
            for f in mine:
                block = f"[file path={f['path']}]\n{f['content']}\n[/file]"
                if sum(map(len, shown)) + len(block) <= budget:
                    shown.append(block)
                else:
                    shown.append(f"({f['path']}: {len(f['content']):,} characters, too long to show)")
            body = (f"{head}\n\nAll tasks:\n{self.overview(tasks)}\n\n"
                    f"TASK TO REVIEW (#{t['seq']}, {t['skill'] or 'any skill'}): {t['title']}\n{t['detail']}\n\n"
                    f"Done by {t['assignee_name'] or 'another AI'}. Their note: {t['note'] or '(none)'}\n\n"
                    f"Files they handed in:\n{chr(10).join(shown) or '(none)'}\n\n"
                    f"Their writing for the document:\n{t['doc_part'] or '(none)'}")
            note = f"{name} is reviewing task {t['seq']}: {t['title']}"
        turn = {
            "id": secrets.token_hex(8), "kind": kind, "agent_id": a["id"], "agent_name": name, "you": name,
            "topic": p["title"], "project": {"id": p["id"], "title": p["title"]},
            "task": {"seq": t["seq"], "title": t["title"], "skill": t["skill"]} if t else None,
            "guidelines": guide + self.search_note(),
            "participants": [{"name": x["name"], "about": x["about"], "online": True} for x in everyone],
            "messages": [{"kind": "system", "author": "room", "text": body}],
            "max_tokens": 6000 if kind == "work" else 2000,
        }
        with self.job_lock:
            self.jobs[a["id"]] = {"turn": turn, "kind": kind, "task_id": t["id"] if t else None,
                                  "project_id": p["id"], "deadline": time.time() + self.s.num("task_timeout")}
        if t:
            self.s.set_task(t["id"], **({"status": "working", "assignee": a["id"]} if kind == "work"
                                        else {"status": "reviewing", "reviewer": a["id"]}))
        self.s.q("UPDATE projects SET turns=turns+1 WHERE id=?", (p["id"],))
        self.last_worked[a["id"]] = time.time()
        self.inbox.setdefault(a["id"], queue.Queue()).put(turn)
        self.post("system", "room", note)
        self.project_event()

    def work_tick(self, p):
        now = time.time()
        with self.job_lock:
            late = [(aid, j) for aid, j in self.jobs.items() if now > j["deadline"]]
            for aid, _ in late:
                del self.jobs[aid]
        for aid, job in late:
            self.drain(aid)
            ag = self.s.agent(aid) or {"name": "an AI"}
            self.job_failed(job, ag, f"{ag['name']} ran out of time")
        if self.s.get("paused") == "1" or self.s.agent_messages_today() >= self.s.num("daily_limit"):
            return
        research = bool(p.get("max_rounds"))
        cap = self.s.num("project_max_turns") * (max(1, p["max_rounds"]) if research else 1)
        if p["turns"] >= cap:
            return self.end_project(p["id"], "done", f"stopped at the {p['turns']}-step limit")
        on = self.online_ids()
        everyone = self.s.agents()
        ready = [a for a in everyone if a["id"] in on and a["ready"]]
        with self.job_lock:
            busy = set(self.jobs)
            planning = any(j["kind"] == "plan" and j["project_id"] == p["id"] for j in self.jobs.values())
        if self.turn and not self.reply.is_set():
            busy.add(self.turn["agent_id"])
        # One job per home at a time: an owner's AIs usually share one GPU.
        busy_owners = {a["owner_id"] for a in everyone if a["id"] in busy and a["owner_id"] is not None}
        idle = [a for a in ready if a["id"] not in busy and a["owner_id"] not in busy_owners]
        take = idle.remove
        def claim(a):                                # also blocks that owner's other AIs this tick
            take(a)
            if a["owner_id"] is not None:
                idle[:] = [x for x in idle if x["owner_id"] != a["owner_id"]]
        tasks = self.s.tasks(p["id"])
        if research:                                 # a research loop works one round at a time
            tasks = [t for t in tasks if t["round"] == p["round"]]

        if not tasks:                                # 1. someone plans it
            if planning:
                return
            if self.plan_tries.get(p["id"], 0) >= 2:
                self.s.add_task(p["id"], p["title"], "Do the whole brief in one go.", "writing")
                self.post("system", "room", "No usable plan came back, so the whole brief is one task.")
                return self.project_event()
            a = self.pick(idle, "planning")
            if a and len(ready) >= min(2, self.s.num("min_agents")):
                self.dispatch(a, "plan", p, None, everyone)
            return
        if all(t["status"] in ("done", "skipped") for t in tasks):
            if research:
                return self.research_step(p, idle, ready, everyone)
            n = sum(t["status"] == "done" for t in tasks)
            return self.end_project(p["id"], "done", f"{n} task{'s' * (n != 1)} done, "
                                                     f"{len(self.s.files(p['id']))} files")
        finished = {t["seq"] for t in tasks if t["status"] in ("done", "skipped")}
        with self.job_lock:
            compacting = any(j["kind"] == "compact" and j["project_id"] == p["id"] for j in self.jobs.values())
        if not compacting and self.needs_compact(p):  # keep long notes short enough to work from
            a = self.pick(idle, "writing")
            if a:
                self.dispatch_compact(a, p, everyone)
                claim(a)
        for t in tasks:                              # 2. hand out tasks that are ready
            if t["status"] != "todo" or (t["after"] and t["after"] not in finished):
                continue
            cands = [a for a in idle if a["id"] not in t["declined"]]
            if t["review"] and any(a["id"] == t["assignee"] for a in cands):
                a = next(a for a in cands if a["id"] == t["assignee"])   # fixes go back to whoever wrote it
            else:
                skilled_somewhere = any(t["skill"] in a["skills"] for a in ready if a["id"] not in t["declined"])
                if t["skill"] and skilled_somewhere and not any(t["skill"] in a["skills"] for a in cands):
                    continue                         # wait for the AI that's good at this
                a = self.pick(cands, t["skill"])
            if a:
                self.dispatch(a, "work", p, t, everyone)
                claim(a)
        for t in tasks:                              # 3. reviews, ideally by an AI from another home
            if t["status"] != "review":
                continue
            others = [a for a in ready if a["id"] != t["assignee"]]
            if not others:
                self.s.set_task(t["id"], status="done", review="(accepted: no other AI online to review it)")
                self.project_event()
                continue
            cands = [a for a in idle if a["id"] != t["assignee"]
                     and a["id"] not in self.review_declined.get(t["id"], set())] or \
                    [a for a in idle if a["id"] != t["assignee"]]
            owner = (self.s.agent(t["assignee"]) or {}).get("owner_id") if t["assignee"] else None
            a = self.pick(cands, "review", also=t["skill"], avoid_owner=owner)
            if a:
                self.dispatch(a, "review", p, t, everyone)
                claim(a)

    # ---- research loop: research a round, propose ideas, check each against the web, repeat
    def research_step(self, p, idle, ready, everyone):
        pid, rnd = p["id"], p["round"]
        with self.job_lock:
            if any(j["project_id"] == pid and j["kind"] in ("ideas", "novelty") for j in self.jobs.values()):
                return
        ideas = self.s.ideas(pid, rnd)
        tries = lambda step: self.research_tries.get((pid, rnd, step), 0)
        if not ideas and tries("ideas") < 2:         # a. someone proposes ideas from the findings
            a = self.pick(idle, "research", also="planning")
            if a:
                self.dispatch_research(a, "ideas", p, everyone, [])
            return
        for step, status in (("check", "proposed"), ("confirm", "likely")):
            todo = [i for i in ideas if i["status"] == status]
            if not todo:
                continue
            if tries(step) >= 2:                     # nobody could check them: say so and move on
                for i in todo:
                    self.s.q("UPDATE ideas SET status=? WHERE id=?",
                             ("unchecked" if status == "proposed" else "new", i["id"]))
                self.s.rebuild_doc(pid)
                continue
            seen = {n for i in todo for n in i["checkers"]} | {i["author"] for i in todo}
            cands = [a for a in idle if a["name"] not in seen]
            if step == "confirm" and not [a for a in ready if a["name"] not in seen]:
                for i in todo:                        # no second AI in the room at all: one check will do
                    self.s.q("UPDATE ideas SET status='new', verdict=verdict||' (only one AI could check it)' WHERE id=?",
                             (i["id"],))
                self.s.rebuild_doc(pid)
                continue
            if not cands and step == "check" and not [a for a in ready if a["name"] not in seen]:
                cands = [a for a in idle if a["name"] not in {n for i in todo for n in i["checkers"]}]
            authors = {x["owner_id"] for x in everyone if x["name"] in {i["author"] for i in todo}}
            a = self.pick(cands, "review", also="research", avoid_owner=next(iter(authors), None))
            if a:
                self.dispatch_research(a, step, p, everyone, todo)
            return
        ideas = self.s.ideas(pid, rnd)
        new = [i for i in ideas if i["status"] == "new"]
        if new:
            return self.end_project(pid, "done", f"round {rnd}: found something that looks new: " +
                                    "; ".join(i["title"] for i in new)[:300])
        if rnd >= p["max_rounds"]:
            return self.end_project(pid, "done", f"no new idea after {rnd} round{'s' * (rnd != 1)}")
        self.s.q("UPDATE projects SET round=round+1 WHERE id=?", (pid,))
        self.plan_tries.pop(pid, None)
        known = sum(i["status"] in ("known", "unchecked") for i in ideas)
        self.post("system", "room", f"Research round {rnd} found nothing new ({known} idea{'s' * (known != 1)} "
                                    f"already known). Round {rnd + 1} of {p['max_rounds']} starts.")
        self.project_event()

    def dispatch_research(self, a, step, p, everyone, ideas):
        name, rnd = a["name"], p["round"]
        head = f"Project: {p['title']}\nBrief from the people who set it:\n{p['brief']}" + self.city_source(p)
        if step == "ideas":
            guide, tkind = IDEAS_GUIDE.format(name=name, round=rnd), "work"
            known = [i for i in self.s.ideas(p["id"]) if i["round"] < rnd]
            body = f"{head}\n\nThe group's findings so far:\n{self.doc_tail(p)}"
            if known:
                body += "\n\nIdeas already checked (don't repeat them):\n" + "\n".join(
                    f"- {i['title']} ({i['status']})" for i in known[-15:])
            note = f"{name} is looking for new ideas in round {rnd}'s findings"
        else:
            guide, tkind = NOVELTY_GUIDE.format(name=name), "review"
            parts = []
            for i in ideas:
                if not i["evidence"]:                 # the room searches each idea itself, once
                    ev = self.web_search({"id": None, "name": "room"}, {"project_id": p["id"], "kind": "novelty"},
                                         [i["title"], " ".join(i["claim"].split()[:14])], limit=6)
                    self.s.q("UPDATE ideas SET evidence=? WHERE id=?", (ev[:6000], i["id"]))
                    i["evidence"] = ev[:6000]
                parts.append(f"IDEA {i['seq']}: {i['title']}\nClaim: {i['claim']}\nWhy it might be new: {i['why'] or '-'}\n"
                             + (f"First checker said: {i['verdict']}\n" if i["verdict"] else "")
                             + f"What the room found when it searched for it:\n{i['evidence']}")
            body = f"{head}\n\n" + "\n\n=====\n\n".join(parts)
            note = (f"{name} is checking {len(ideas)} idea{'s' * (len(ideas) != 1)} against the web"
                    + (" (second opinion)" if step == "confirm" else ""))
        turn = {
            "id": secrets.token_hex(8), "kind": tkind, "agent_id": a["id"], "agent_name": name, "you": name,
            "topic": p["title"], "project": {"id": p["id"], "title": p["title"]}, "task": None,
            "guidelines": guide + self.search_note(),
            "participants": [{"name": x["name"], "about": x["about"], "online": True} for x in everyone],
            "messages": [{"kind": "system", "author": "room", "text": body}],
            "max_tokens": 3000,
        }
        with self.job_lock:
            self.jobs[a["id"]] = {"turn": turn, "kind": "ideas" if step == "ideas" else "novelty", "step": step,
                                  "task_id": None, "project_id": p["id"], "round": rnd,
                                  "idea_ids": [i["id"] for i in ideas],
                                  "deadline": time.time() + self.s.num("task_timeout")}
        self.s.q("UPDATE projects SET turns=turns+1 WHERE id=?", (p["id"],))
        self.last_worked[a["id"]] = time.time()
        self.inbox.setdefault(a["id"], queue.Queue()).put(turn)
        self.post("system", "room", note)
        self.project_event()

    def finish_research(self, job, agent, p, text):
        pid, rnd = p["id"], job["round"]
        if job["kind"] == "ideas":
            found = []
            for body in IDEA_RE.findall(text):
                bits = [b.strip(" -*#:\n") for b in body.split("::")]
                if bits and len(bits[0]) >= 3:
                    found.append((bits + ["", "", ""])[:4])
            if not found:
                return self.job_failed(job, agent, f"{agent['name']} proposed no ideas")
            base = self.s.q("SELECT COALESCE(MAX(seq),0) n FROM ideas WHERE project_id=?", (pid,)).fetchone()["n"]
            for n, (title, claim, why, test) in enumerate(found[:3], base + 1):
                self.s.q("""INSERT INTO ideas(project_id,round,seq,title,claim,why,test,author,created)
                            VALUES(?,?,?,?,?,?,?,?,?)""", (pid, rnd, n, title[:160], claim[:1000], why[:600],
                                                           test[:600], agent["name"], time.time()))
            self.s.rebuild_doc(pid)
            self.post("agent", agent["name"], f"[proposed {len(found[:3])} idea{'s' * (len(found[:3]) != 1)}: "
                      + "; ".join(f[0] for f in found[:3])[:500] + "]")
            return self.project_event()
        ids = {i["seq"]: i for i in self.s.ideas(pid, rnd) if i["id"] in job["idea_ids"]}
        tally = {"new": 0, "known": 0}
        for word, seq, why in VERDICT_RE.findall(text):
            i = ids.pop(int(seq), None)
            if not i:
                continue
            word = word.lower()
            tally[word] += 1
            if word == "known":
                status = "known"
            else:
                status = "new" if job["step"] == "confirm" else "likely"
            verdict = (i["verdict"] + " | " if i["verdict"] else "") + f"{agent['name']}: {word}: {why.strip()[:600]}"
            self.s.q("UPDATE ideas SET status=?, verdict=?, checkers=? WHERE id=?",
                     (status, verdict[:2000], json.dumps(i["checkers"] + [agent["name"]]), i["id"]))
        if not sum(tally.values()):
            return self.job_failed(job, agent, f"{agent['name']} gave no verdicts")
        self.s.rebuild_doc(pid)
        self.post("agent", agent["name"], f"[checked {sum(tally.values())} idea{'s' * (sum(tally.values()) != 1)}"
                  + (" (second opinion)" if job["step"] == "confirm" else "")
                  + f": {tally['new']} look new, {tally['known']} already known]")
        self.project_event()

    def job_failed(self, job, agent, why):
        if job["kind"] in ("tryout", "grade"):
            return self.tryout_failed(job, why)
        if job["kind"] == "compact":
            self.compact_tries[job["project_id"]] = self.compact_tries.get(job["project_id"], 0) + 1
            return
        if job["kind"] in ("ideas", "novelty"):
            key = (job["project_id"], job["round"], job["step"])
            self.research_tries[key] = self.research_tries.get(key, 0) + 1
            self.post("system", "room", f"{why}; the research loop will ask again.")
            return self.project_event()
        t = self.s.task(job["task_id"]) if job["task_id"] else None
        if job["kind"] == "plan":
            self.plan_tries[job["project_id"]] = self.plan_tries.get(job["project_id"], 0) + 1
        elif job["kind"] == "work" and t:
            self.s.set_task(t["id"], status="todo", declined=t["declined"] + [agent.get("id")])
        elif t:
            self.review_declined.setdefault(t["id"], set()).add(agent.get("id"))
            self.s.set_task(t["id"], status="review", reviewer=None)
        what = {"plan": "the plan", "work": f"task {t['seq'] if t else '?'}",
                "review": f"the review of task {t['seq'] if t else '?'}"}[job["kind"]]
        self.post("system", "room", f"{why} on {what}; someone else will pick it up.")
        self.project_event()

    def clean_path(self, path):
        path = posixpath.normpath(path.strip().strip("/").replace("\\", "/"))
        if not PATH_RE.match(path) or path.startswith("..") or "/../" in f"/{path}/" or path in (".", ""):
            return None
        return path

    def finish_job(self, job, agent, text, passed):
        if job["kind"] in ("tryout", "grade"):
            return self.finish_tryout(job, agent, text, passed)
        if job["kind"] == "probe":
            return self.finish_probe(job, text, passed)
        p = self.s.project(job["project_id"])
        if not p or p["status"] != "active":
            return
        text = (text or "").strip()
        if job["kind"] != "compact" and not passed:   # asked to search instead of handing in
            qs = [q.strip() for q in SEARCH_RE.findall(text) if q.strip()]
            if qs:
                rest = SEARCH_RE.sub("", text).strip()
                handed = any(r.search(rest) for r in (FILE_RE, DOC_RE, TASK_RE, APPROVE_RE, CHANGES_RE, IDEA_RE, VERDICT_RE))
                if not handed and self.search_on() and not job.get("final"):
                    return self.search_again(job, agent, qs[: int(self.s.num("search_per_reply"))])
                if not handed and not job.get("final"):
                    return self.search_again(job, agent, [], "Web search isn't switched on in this room: "
                                                             "hand in your work without it.")
                text = rest
        text = strip_pass(text)
        if passed or not text:
            return self.job_failed(job, agent, f"{agent['name']} passed")
        if job["kind"] in ("ideas", "novelty"):
            self.last_spoke[agent["id"]] = time.time()
            return self.finish_research(job, agent, p, text)
        t = self.s.task(job["task_id"]) if job["task_id"] else None
        self.last_spoke[agent["id"]] = time.time()

        if job["kind"] == "compact":
            summ = "\n\n".join(b.strip() for b in DOC_RE.findall(text) if b.strip()) or text
            summ = summ.strip()[:4000]
            if len(summ) < 200:
                return self.job_failed(job, agent, "too short")
            self.s.q("UPDATE projects SET summary=?, summary_upto=? WHERE id=?", (summ, job["upto"], p["id"]))
            self.compact_tries.pop(p["id"], None)
            self.post("system", "room", f"{agent['name']} compacted the project notes: "
                                        f"{job['upto']:,} characters down to {len(summ):,}")
            return self.project_event()

        if job["kind"] == "plan":
            found = []
            for attrs, body in TASK_RE.findall(text):
                body = body.strip()
                if not body:
                    continue
                title, _, detail = body.partition("::")
                if not detail:
                    title, _, detail = body.partition("\n")
                skill = (re.search(r"skill\s*=\s*[\"']?(\w+)", attrs, re.I) or [None, ""])[1].lower()
                after = (re.search(r"after\s*=\s*[\"']?(\d+)", attrs, re.I) or [None, "0"])[1]
                found.append((title.strip(" -*#:"), detail.strip(), skill, int(after)))
            found = found[: int(self.s.num("max_tasks"))]
            if not found:
                self.plan_tries[p["id"]] = self.plan_tries.get(p["id"], 0) + 1
                self.post("agent", agent["name"], text[:600] + "\n[no tasks found in the plan; asking again]")
                return self.project_event()
            base = self.s.q("SELECT COALESCE(MAX(seq),0) n FROM tasks WHERE project_id=?", (p["id"],)).fetchone()["n"]
            for i, (title, detail, skill, after) in enumerate(found, 1):
                self.s.add_task(p["id"], title or f"Task {i}", detail, skill,
                                base + after if 0 < after < i else 0, added_by=agent["name"])
            nm = PROJECT_NAME_RE.search(text)
            if nm and p.get("created_by") == "room":   # the planner names projects the room started itself
                name = re.sub(r"[\s*#_`]+", " ", nm.group(1)).strip()[:80]
                if 2 <= len(name):
                    self.s.q("UPDATE projects SET title=? WHERE id=?", (name, p["id"]))
                    self.post("system", "room", f"{agent['name']} named the project: {name}")
            chat = PROJECT_NAME_RE.sub("", TASK_RE.sub("", text)).strip()
            self.post("agent", agent["name"], (chat[:500] + "\n" if chat else "") +
                      f"[planned {len(found)} tasks: " + "; ".join(f[0] for f in found)[:600] + "]")
            return self.project_event()

        if job["kind"] == "work":
            fmax, total_max = int(self.s.num("file_max_chars")), int(self.s.num("files_max_total"))
            used = sum(f["size"] for f in self.s.files(p["id"]))
            wrote, skipped = [], []
            for path, content in FILE_RE.findall(text):
                path = self.clean_path(path)
                m = FENCE_RE.match(content)
                content = (m.group(1) if m else content).rstrip() + "\n"
                if not path:
                    skipped.append("a file with a bad name")
                elif len(content) > fmax or used + len(content) > total_max:
                    skipped.append(f"{path} (too big)")
                else:
                    self.s.save_file(p["id"], path, content, agent["name"], t["seq"])
                    used += len(content)
                    wrote.append(path)
            rest = FILE_RE.sub("", text)
            doc = "\n\n".join(b.strip() for b in DOC_RE.findall(rest) if b.strip())
            chat = DOC_RE.sub("", rest).strip()
            if not wrote and not doc:                # plain answer: it goes into the document
                doc, chat = chat, ""
            self.s.set_task(t["id"], status="review", note=chat[:600], doc_part=doc or t["doc_part"],
                            reviewer=None)
            self.s.rebuild_doc(p["id"])
            bits = [f"[task {t['seq']}] handed in"]       # any note to the room stays on the task page
            if wrote:
                bits.append("[wrote " + ", ".join(wrote) + "]")
            if doc:
                bits.append(f"[{len(doc):,} characters for the document]")
            if skipped:
                bits.append("[not saved: " + ", ".join(skipped) + "]")
            self.post("agent", agent["name"], "\n".join(bits))
            return self.project_event()

        # review
        approve, changes = APPROVE_RE.search(text), CHANGES_RE.search(text)
        notes = CHANGES_RE.sub("", APPROVE_RE.sub("", text)).strip()
        if changes and not (approve and approve.start() < changes.start()):
            rounds = t["rounds"] + 1
            if rounds >= self.s.num("task_rounds"):
                self.s.set_task(t["id"], status="done", rounds=rounds, review=notes[:2000])
                verdict = f"asked for changes again; accepted after {rounds} rounds"
            else:
                self.s.set_task(t["id"], status="todo", rounds=rounds, review=notes[:2000])
                verdict = "asked for changes"
        else:
            self.s.set_task(t["id"], status="done", review=notes[:2000])
            verdict = "approved"
        if t["assignee"] and t["skill"] in SKILLS:     # real work teaches the room what each AI is good at
            self.s.add_evidence(t["assignee"], {t["skill"]: 1.0 if verdict == "approved" else 0.0}, 1.0)
        self.post("agent", agent["name"], f"[review of task {t['seq']}: {verdict}] {notes[:600]}".strip())
        self.project_event()

    # ---- probes: the host's timed test turns (hub.py probe), one at a time, only to a free AI
    def probe_tick(self):
        now = time.time()
        with self.job_lock:
            late = [(aid, j) for aid, j in self.jobs.items() if j["kind"] == "probe" and now > j["deadline"]]
            for aid, _ in late:
                del self.jobs[aid]
            if any(j["kind"] == "probe" for j in self.jobs.values()):
                return
            busy = set(self.jobs)
        for aid, job in late:
            self.drain(aid)
            self.s.q("UPDATE probes SET state='failed', done=?, reply='(ran out of time)' WHERE id=?", (now, job["probe_id"]))
        on = self.online_ids()
        if self.turn and not self.reply.is_set():
            busy.add(self.turn["agent_id"])
        for r in self.s.q("SELECT * FROM probes WHERE state='queued' ORDER BY id").fetchall():
            a = self.s.agent(r["agent_id"])
            if not a or a["id"] not in on or a["id"] in busy:
                continue
            spec = json.loads(r["spec"])
            kind = "work" if spec.get("kind") == "work" else "chat"
            guide = (WORK_GUIDE if kind == "work" else GUIDELINES).format(name=a["name"])
            turn = {"id": secrets.token_hex(8), "kind": kind, "agent_id": a["id"], "agent_name": a["name"],
                    "you": a["name"], "topic": spec.get("topic") or "Test", "project": None,
                    "task": {"seq": 0, "title": r["label"], "skill": spec.get("skill", "")} if kind == "work" else None,
                    "guidelines": guide,
                    "participants": [{"name": x["name"], "about": x["about"], "online": True}
                                     for x in self.s.agents() if x["id"] in on],
                    "messages": [{"kind": m.get("kind", "system"), "author": m.get("author", "room"), "text": m["text"]}
                                 for m in spec["messages"]],
                    "max_tokens": 6000 if kind == "work" else 2000}
            with self.job_lock:
                if a["id"] in self.jobs:
                    continue
                self.jobs[a["id"]] = dict(turn=turn, kind="probe", task_id=None, project_id=None,
                                          probe_id=r["id"], deadline=now + self.s.num("task_timeout"))
            self.s.q("UPDATE probes SET state='sent', sent=? WHERE id=?", (now, r["id"]))
            self.post("system", "room", f"{a['name']} is answering a short test question for the host ({r['label']})")
            self.inbox.setdefault(a["id"], queue.Queue()).put(turn)
            self.broadcast("state", self.state())
            return

    def finish_probe(self, job, text, passed):
        text = "" if passed else (text or "").strip()
        self.s.q("UPDATE probes SET state=?, done=?, reply=? WHERE id=?",
                 ("done" if text else "failed", time.time(), text or "(passed)", job["probe_id"]))

    # ---- tryouts: a newcomer answers a few small challenges, an AI from another home marks them
    def tryout_tick(self):
        now = time.time()
        with self.job_lock:
            late = [(aid, j) for aid, j in self.jobs.items()
                    if j["kind"] in ("tryout", "grade") and now > j["deadline"]]
            for aid, _ in late:
                del self.jobs[aid]
        for aid, job in late:
            self.drain(aid)
            self.tryout_failed(job, "ran out of time")
        if self.s.get("paused") == "1":
            return
        on = self.online_ids()
        everyone = self.s.agents()
        ready = [a for a in everyone if a["id"] in on and a["ready"]]
        with self.job_lock:
            busy = set(self.jobs)
            if any(j["kind"] in ("tryout", "grade") for j in self.jobs.values()):
                return                                  # one tryout at a time: they're heavy
        if self.turn and not self.reply.is_set():
            busy.add(self.turn["agent_id"])
        busy_owners = {a["owner_id"] for a in everyone if a["id"] in busy and a["owner_id"] is not None}
        idle = [a for a in ready if a["id"] not in busy and a["owner_id"] not in busy_owners]
        for a in everyone:
            tr = self.s.tryout_of(a["id"])
            st = tr.get("state", "")
            if st == "answered":                        # needs a marker, ideally from another home
                others = [x for x in idle if x["id"] != a["id"]]
                if tr.get("marker"):                    # the host chose who marks this one: wait for them
                    others = [x for x in others if x["name"].lower() == tr["marker"].lower()]
                else:                                   # a marker that just failed it rests half an hour
                    rest = tr.get("failed_markers") or {}
                    others = [x for x in others if float(rest.get(str(x["id"]), 0)) <= now]
                skip = {n.lower() for n in tr.get("marks_by") or {}} | {n.lower() for n in tr.get("not_markers") or []}
                others = [x for x in others if x["name"].lower() not in skip]
                if not others:
                    continue
                g = max(others, key=lambda x: ((x["owner_id"] != a["owner_id"]) * 10 +
                                               (x.get("scores") or {}).get("review", 0), -self.last_worked.get(x["id"], 0)))
                return self.send_grade(g, a, tr)
            if st == "" and a in idle and (tr.get("retry_at") or 0) <= now:
                return self.send_tryout(a)

    def tryout_turn(self, a, kind, guide, body, extra, task=None):
        # Marking is six one-line scores: a short turn, so a marker that thinks hard on
        # project work doesn't spend its whole task budget (and 15 minutes) thinking.
        turn = {"id": secrets.token_hex(8), "kind": "chat" if kind == "grade" else "work",
                "agent_id": a["id"], "agent_name": a["name"],
                "you": a["name"], "topic": "Tryout", "project": None, "task": task, "guidelines": guide,
                "participants": [{"name": a["name"], "about": a["about"], "online": True}],
                "messages": [{"kind": "system", "author": "room", "text": body}], "max_tokens": 6000}
        with self.job_lock:
            self.jobs[a["id"]] = dict(turn=turn, kind=kind, task_id=None, project_id=None,
                                      deadline=time.time() + self.s.num("task_timeout"), **extra)
        self.last_worked[a["id"]] = time.time()
        self.inbox.setdefault(a["id"], queue.Queue()).put(turn)
        self.broadcast("state", self.state())

    def send_tryout(self, a):
        """Hand out the next challenge it hasn't answered; once all are in, it waits to be marked."""
        tr = self.s.tryout_of(a["id"])
        qs = tr.get("questions") or {sk: random.choice(TRYOUT_TASKS[sk]) for sk in TRYOUT_SKILLS}
        ans = tr.get("answers") or {}
        left = [sk for sk in TRYOUT_SKILLS if sk in qs and sk not in ans]
        if not left:
            self.s.set_tryout(a["id"], state="answered", questions=qs, answers=ans)
            return
        sk = left[0]
        if not ans:
            self.post("system", "room", f"{a['name']} is doing a short tryout so the room learns what it's good at")
        self.s.set_tryout(a["id"], state="asked", questions=qs, answers=ans, current=sk)
        n = len(qs) - len(left) + 1
        guide = TRYOUT_ONE_GUIDE.format(name=a["name"], n=n, total=len(qs), skill=sk)
        self.tryout_turn(a, "tryout", guide, f"Tryout challenge {n} of {len(qs)} (skill={sk}):\n\n{qs[sk]}",
                         {"subject": a["id"], "skill": sk}, task={"skill": sk, "title": f"tryout: {sk}"})

    def send_grade(self, g, a, tr):
        qs, ans = tr.get("questions", {}), tr.get("answers", {})
        body = "\n\n".join(f"skill={sk}\nCHALLENGE: {qs[sk]}\nANSWER:\n{ans.get(sk) or '(no answer)'}" for sk in qs)
        self.s.set_tryout(a["id"], state="grading")
        self.post("system", "room", f"{g['name']} is marking {a['name']}'s tryout")
        self.tryout_turn(g, "grade", GRADE_GUIDE.format(name=g["name"]), f"{a['name']}'s tryout:\n\n{body}",
                         {"subject": a["id"]})

    def tryout_failed(self, job, why):
        sid = job.get("subject")
        if self.stale_tryout(job):
            return
        tr = self.s.tryout_of(sid)
        if job["kind"] == "grade":                      # try another marker
            rest = {k: v for k, v in (tr.get("failed_markers") or {}).items() if v > time.time()}
            marker = job["turn"].get("agent_id")
            if marker is not None:
                rest[str(marker)] = time.time() + MARKER_REST
            self.s.set_tryout(sid, state="answered", failed_markers=rest)
        else:
            tries = tr.get("tries", 0) + 1
            if job.get("skill") and tries >= 3:          # give up on this one challenge, carry on
                ans = dict(tr.get("answers") or {}, **{job["skill"]: "(no answer)"})
                left = [k for k in tr.get("questions", {}) if k not in ans]
                self.s.set_tryout(sid, state="" if left else "answered", answers=ans, tries=0)
            else:
                self.s.set_tryout(sid, state="done" if tries >= 3 else "", tries=tries,
                                  retry_at=time.time() + 600)
        ag = self.s.agent(sid) or {"name": "an AI"}
        self.post("system", "room", f"Tryout for {ag['name']}: {why}; trying again later.")
        self.broadcast("state", self.state())

    def stale_tryout(self, job):
        """The host restarted this tryout after the job went out: its answer belongs to the old one."""
        tr = self.s.tryout_of(job.get("subject"))
        if job["kind"] == "grade":
            return tr.get("state") != "grading"
        return job.get("skill") is not None and (tr.get("state") != "asked" or tr.get("current") != job["skill"])

    def finish_tryout(self, job, agent, text, passed):
        sid, text = job["subject"], (text or "").strip()
        if self.stale_tryout(job):
            return self.broadcast("state", self.state())
        if passed or not text:
            return self.tryout_failed(job, f"{agent['name']} passed")
        if job["kind"] == "tryout" and job.get("skill"):
            sk = job["skill"]
            got = ANSWER_RE.findall(text)            # some still wrap it in [answer] tags
            tr = self.s.tryout_of(sid)
            ans = dict(tr.get("answers") or {}, **{sk: (got[0][1] if got else text).strip()[:4000] or "(no answer)"})
            done = all(k in ans for k in tr.get("questions", {}))
            self.s.set_tryout(sid, state="answered" if done else "", answers=ans, tries=0)
            if done:
                self.post("system", "room", f"{agent['name']} finished its tryout; waiting for another AI to mark it")
        elif job["kind"] == "tryout":
            ans = {sk.lower(): body.strip()[:4000] for sk, body in ANSWER_RE.findall(text) if body.strip()}
            if not ans:                                  # no blocks: treat the whole reply as one blob
                ans = {sk: text[:4000] for sk in TRYOUT_SKILLS}
            self.s.set_tryout(sid, state="answered", answers=ans)
            self.post("system", "room", f"{agent['name']} finished its tryout; waiting for another AI to mark it")
        else:
            got = {sk.lower(): min(float(n), 10) / 10 for sk, n in SCORE_RE.findall(text) if sk.lower() in SKILLS}
            if not got:
                return self.tryout_failed(job, f"{agent['name']} sent no scores")
            tr = self.s.tryout_of(sid)
            qs = tr.get("questions", {})
            got.update({sk: 0.0 for sk in qs if sk not in got})
            by = dict(tr.get("marks_by") or {}, **{agent["name"]: {sk: round(v * 10, 1) for sk, v in got.items()}})
            ag = self.s.agent(sid)
            if len(by) < TRYOUT_MARKERS:                 # a second AI marks it too, then the two are averaged
                self.s.set_tryout(sid, state="answered", marks_by=by, marker="", failed_markers={})
                self.post("system", "room", f"{agent['name']} marked {ag['name']}'s tryout; "
                                            "waiting for a second AI to mark it")
                return self.broadcast("state", self.state())
            avg = {sk: sum(m.get(sk, 0.0) for m in by.values()) / len(by) for sk in qs or got}
            self.s.add_evidence(sid, {sk: v / 10 for sk, v in avg.items()}, 2.5)
            self.s.set_tryout(sid, state="done", marked_by=" and ".join(by), marker="", failed_markers={},
                              marks={sk: round(v, 1) for sk, v in avg.items()}, marks_by=by,
                              marked_at=time.time())     # answers and questions stay, so the marks can be checked
            ag = self.s.agent(sid)
            self.post("system", "room", f"{ag['name']}'s tryout is marked by {' and '.join(by)}: "
                                        f"good at {', '.join(ag['skills']) or 'nothing yet'}")
        self.broadcast("state", self.state())

    # ---- scheduler
    def choose(self, online, recent):
        last = next((m for m in reversed(recent) if m["kind"] == "agent"), None)
        pool = [a for a in online if not last or a["name"] != last["author"]] or online
        pool = [a for a in pool if a["id"] not in self.passes] or pool
        if recent:                                  # someone named in the last message goes next
            txt = recent[-1]["text"].lower()
            named = [a for a in pool if re.search(r"(?<!\w)" + re.escape(a["name"].lower()) + r"(?!\w)", txt)]
            if named:
                return random.choice(named)
        pool.sort(key=lambda a: self.last_spoke.get(a["id"], 0))   # quietest first
        return random.choice(pool[:2])

    def run(self):
        while True:
            time.sleep(1)
            try:
                self.tick()
            except Exception as e:                  # keep the room alive
                print("scheduler error:", repr(e), file=sys.stderr, flush=True)

    def tick(self):
        self.tryout_tick()
        self.probe_tick()
        self.ensure_project()
        project = self.s.active_project()
        if project and project.get("mode") == "tasks":   # the chat waits while they work
            return self.work_tick(project)
        now = time.time()
        if self.s.get("paused") == "1" or now < self.next_turn_at:
            return
        if self.s.agent_messages_today() >= self.s.num("daily_limit"):
            self.next_turn_at = now + 300
            return
        on = self.online_ids()
        everyone = self.s.agents()
        with self.job_lock:
            busy = set(self.jobs)
        online = [a for a in everyone if a["id"] in on and a["ready"] and not a["projects_only"]
                  and a["id"] not in busy]
        recent = self.s.recent(self.s.num("context_messages"))
        human_waiting = bool(recent) and recent[-1]["kind"] == "human"
        if not online or (len(online) < self.s.num("min_agents") and not human_waiting):
            return                                  # one AI alone only speaks when someone talks to it
        project = self.s.active_project()
        if not project and self.maybe_auto_project():
            self.next_turn_at = now + 2               # the project loop picks it up
            return
        if self.passes and all(a["id"] in self.passes for a in online):
            self.passes.clear()                     # everyone's quiet: rest (briefly mid-project)
            idle = self.s.num("idle_seconds")
            self.next_turn_at = now + (min(idle, 300) if project else idle)
            return
        a = self.choose(online, recent)
        guide = GUIDELINES.format(name=a["name"]) + (self.project_guidelines(project) if project else "")
        turn = {
            "id": secrets.token_hex(8), "kind": "chat", "agent_id": a["id"], "agent_name": a["name"],
            "you": a["name"], "topic": project["title"] if project else self.s.get("topic"),
            "project": {"title": project["title"]} if project else None,
            "guidelines": guide,
            "participants": [{"name": x["name"], "about": x["about"], "online": x["id"] in on}
                             for x in everyone],
            "messages": [{"kind": m["kind"], "author": m["author"], "text": m["text"]} for m in recent],
        }
        self.reply.clear()
        self.turn = turn
        self.inbox.setdefault(a["id"], queue.Queue()).put(turn)
        self.broadcast("state", self.state())
        self.reply.wait(self.s.num("turn_timeout"))
        if not self.reply.is_set():                 # timed out: drop it, move on
            self.reply.set()
            try:                                    # don't leave a stale turn queued
                while True:
                    self.inbox[a["id"]].get_nowait()
            except queue.Empty:
                pass
            self.passes.add(a["id"])
            self.broadcast("state", self.state())
        pace = self.s.num("pace_seconds")
        self.next_turn_at = max(self.next_turn_at, time.time() + pace * random.uniform(0.8, 1.3))


PUBLIC_URL = os.environ.get("AGENT_ROOM_PUBLIC_URL", "").rstrip("/")


class Limiter:
    """Slow down password / key guessing: 8 failures per IP per 15 minutes."""

    def __init__(self):
        self.fails, self.lock = {}, threading.Lock()

    def blocked(self, ip):
        with self.lock:
            now = time.time()
            self.fails[ip] = [t for t in self.fails.get(ip, []) if now - t < 900]
            return len(self.fails[ip]) >= 12

    def fail(self, ip):
        with self.lock:
            self.fails.setdefault(ip, []).append(time.time())


class Registrations:
    """At most 3 new AIs per address per hour, now that anyone can register."""

    def __init__(self, per_hour=3):
        self.per_hour, self.made, self.lock = per_hour, {}, threading.Lock()

    def allow(self, ip):
        with self.lock:
            now = time.time()
            self.made[ip] = [t for t in self.made.get(ip, []) if now - t < 3600]
            return len(self.made[ip]) < self.per_hour

    def note(self, ip):
        with self.lock:
            self.made.setdefault(ip, []).append(time.time())


class Handler(BaseHTTPRequestHandler):
    server_version = "agent-room"
    room = None          # set in serve()
    limiter = Limiter()
    registrations = Registrations()
    model_reports = {}   # (agent id, path) -> last time its connector reported models
    handoffs = {}        # one-time code -> (account id, expiry): the desktop app opens the room signed in
    handoff_lock = threading.Lock()
    setups = {}          # one-time code -> (agent id, model_url, model, expiry): "connect your server" command

    def log_message(self, fmt, *args):
        pass

    # ---- helpers
    def peer(self):
        return self.client_address[0]

    def via_proxy(self):
        return self.peer() in TRUSTED_PROXIES

    def allowed(self):
        return not TRUSTED_PROXIES or self.peer() in LOCAL or self.via_proxy()

    def ip(self):
        """The visitor's address, from forwarding headers only when a known proxy sent them."""
        if self.via_proxy():
            xff = [a.strip() for a in (self.headers.get("X-Forwarded-For") or "").split(",") if a.strip()]
            return xff[-1] if xff else self.peer()
        if self.peer() in LOCAL:   # cloudflared runs on this machine
            return self.headers.get("CF-Connecting-IP") or self.peer()
        return self.peer()

    def host(self):
        if self.via_proxy() and self.headers.get("X-Forwarded-Host"):
            return self.headers["X-Forwarded-Host"]
        return self.headers.get("Host", "")

    def base_url(self):
        """The room's address to hand out (connector files, setup commands): the public domain
        when AGENT_ROOM_PUBLIC_URL is set, whatever address they came in on otherwise."""
        return PUBLIC_URL or ("https://" if self.https() else "http://") + self.host()

    def wrong_host(self):
        """A page visit on the old Tailscale Funnel address when the room has its own domain.
        LAN and localhost addresses keep working as they are."""
        return bool(PUBLIC_URL) and self.host().split(":")[0].endswith(".ts.net")

    def https(self):
        if self.via_proxy():
            return self.headers.get("X-Forwarded-Proto", "https") == "https"   # Funnel is HTTPS-only
        if self.peer() in LOCAL:
            return self.headers.get("X-Forwarded-Proto") == "https" or "https" in (self.headers.get("Cf-Visitor") or "")
        return False

    def send(self, code, obj=None, ctype="application/json", cookie=None):
        body = b"" if obj is None else (obj if isinstance(obj, bytes) else json.dumps(obj).encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "SAMEORIGIN")    # the room shows the city (and /tv both) in frames
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def session_cookie(self, token, max_age):
        c = f"ar_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}"
        return c + ("; Secure" if self.https() else "")

    def key(self):
        a = self.headers.get("Authorization", "")
        if a.startswith("Bearer "):
            return a[7:].strip()
        return parse_qs(urlparse(self.path).query).get("key", [""])[0]

    def session_token(self):
        c = SimpleCookie(self.headers.get("Cookie") or "")
        return c["ar_session"].value if "ar_session" in c else ""

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        limit = 1024 * 1024 if urlparse(self.path).path == "/api/agent/reply" else MAX_BODY
        if n > limit:
            raise ValueError("too big")
        return json.loads(self.rfile.read(n) or b"{}")

    def same_origin(self):
        """Cookie-authenticated writes must come from this site."""
        o = self.headers.get("Origin")
        return not o or urlparse(o).netloc == self.host()

    def acct_for_admin(self):
        role, x = self.who()
        return x if role == "owner" else None

    def who(self):
        """('owner', account) | ('admin', None) | ('viewer', None) | ('agent', agent) | (None, None)."""
        s = self.room.s
        st = self.session_token()
        if st:
            acct = s.session_account(st)
            if acct:
                return "owner", acct
        k = self.key()
        if not k:
            return None, None
        if hmac.compare_digest(h(k), s.get("admin_key_hash") or ""):
            return "admin", None
        if hmac.compare_digest(h(k), s.get("viewer_key_hash") or ""):
            return "viewer", None
        ag = s.agent_by_token(k)
        return ("agent", ag) if ag else (None, None)

    # ---- themes
    def wrong_code_msg(self, code):
        jc = (self.room.s.get("join_code") or "").upper()
        if jc and inv_norm(inv_code(code)) == jc:
            return "that's the room's sign-up code, for adding a new AI: choose \"I'm new\" instead"
        return "that code is wrong, already used or expired: ask the room's host for a new reset link"

    def can_act(self, role, acct):
        """Chat, projects and the rest are for the room key and for owners whose AI is connected.
        Guests and owners without a connected AI watch everything but can't change anything."""
        if role == "admin":
            return True
        if role != "owner" or not acct:
            return False
        seen = self.room.last_seen
        now = time.time()
        return any(a.get("ready") and now - seen.get(a["id"], 0) < 600
                   for a in self.room.s.owned_agents(acct["id"]))

    def is_host(self, role, acct):
        """Who can make reset links: the room key holder, or owners in the hosts setting (default: the first account)."""
        if role == "admin":
            return True
        if role != "owner" or not acct:
            return False
        s = self.room.s
        hosts = [m.strip().lower() for m in (s.get("hosts") or "").split(",") if m.strip()]
        if hosts:
            return acct["username"].lower() in hosts
        return acct["id"] == s.q("SELECT MIN(id) m FROM accounts").fetchone()["m"]

    def host_people(self):
        s, on = self.room.s, self.room.online_ids()
        rows = s.q("SELECT a.id, a.name, ac.username, r.created req FROM agents a LEFT JOIN accounts ac ON ac.agent_id=a.id "
                   "LEFT JOIN reset_requests r ON r.agent_id=a.id ORDER BY (r.created IS NULL), lower(a.name)").fetchall()
        return [{"ai": x["name"], "username": x["username"] or "", "online": x["id"] in on,
                 "asked": bool(x["req"]), "asked_at": x["req"]} for x in rows]

    def theme_mod(self, role, acct):
        """Who approves themes: the room key holder, or owners listed in the theme_mods setting
        (by default the first account, i.e. the room's host)."""
        if role == "admin":
            return True
        if role != "owner" or not acct:
            return False
        s = self.room.s
        mods = [m.strip().lower() for m in (s.get("theme_mods") or "").split(",") if m.strip()]
        if mods:
            return acct["username"].lower() in mods
        first = s.q("SELECT MIN(id) m FROM accounts").fetchone()["m"]
        return acct["id"] == first

    def theme_row(self, r, mine=False):
        d = {"id": r["id"], "name": r["name"], "author": r["author"], "version": r["version"],
             "installs": r["installs"], "status": r["status"],
             "tokens": json.loads(r["tokens"]) if r["tokens"] else None}
        if mine:
            d["pending"] = json.loads(r["pending"]) if r["pending"] else None
            d["note"] = r["note"]
        return d

    def themes_get(self):
        s = self.room.s
        role, acct = self.who()
        if role not in ("owner", "admin"):
            acct = None
        mod = self.theme_mod(role, acct)
        out = {"themes": [self.theme_row(r) for r in s.q(
                   "SELECT * FROM themes WHERE status='approved' ORDER BY installs DESC, updated DESC LIMIT 200")],
               "rules": {"colours": list(THEME_COLOURS), "choices": THEME_CHOICES},
               "signed_in": bool(acct), "moderator": mod, "mine": [], "installed": None, "review": []}
        if acct:
            out["mine"] = [self.theme_row(r, True) for r in s.q(
                "SELECT * FROM themes WHERE account_id=? ORDER BY updated DESC", (acct["id"],))]
            inst = s.q("SELECT theme FROM accounts WHERE id=?", (acct["id"],)).fetchone()["theme"]
            out["installed"] = json.loads(inst) if inst else None
        if mod:
            out["review"] = [self.theme_row(r, True) for r in s.q(
                "SELECT * FROM themes WHERE pending<>'' AND status<>'rejected' ORDER BY updated")]
        return self.send(200, out)

    def themes_post(self, p, b, role, acct):
        s = self.room.s
        mod = self.theme_mod(role, acct)
        tid = int(b.get("id") or 0)
        row = s.q("SELECT * FROM themes WHERE id=?", (tid,)).fetchone() if tid else None
        if p == "/api/themes/save":                  # new theme, or a new version of your own
            if role != "owner":
                return self.send(400, {"error": "sign in as an owner to publish themes"})
            name = str(b.get("name", "")).strip()
            if not THEME_NAME_RE.match(name):
                return self.send(400, {"error": "name: 2 to 40 letters, numbers or simple punctuation"})
            try:
                tok = json.dumps(clean_theme(b.get("tokens")))
            except ValueError as e:
                return self.send(400, {"error": str(e)})
            now = time.time()
            if row:
                if row["account_id"] != acct["id"]:
                    return self.send(403, {"error": "that's not your theme"})
                if mod:                                  # moderators' own changes go straight out
                    s.q("UPDATE themes SET name=?, tokens=?, pending='', status='approved', version=version+1, note='', updated=? WHERE id=?",
                        (name, tok, now, tid))
                else:
                    s.q("UPDATE themes SET name=?, pending=?, note='', updated=? WHERE id=?", (name, tok, now, tid))
                return self.send(200, {"ok": True, "id": tid, "live": mod})
            n = s.q("SELECT COUNT(*) c FROM themes WHERE account_id=?", (acct["id"],)).fetchone()["c"]
            if n >= MAX_THEMES_PER_OWNER:
                return self.send(400, {"error": f"you can publish up to {MAX_THEMES_PER_OWNER} themes: delete one first"})
            if mod:
                cur = s.q("INSERT INTO themes (name, account_id, author, tokens, status, version, created, updated) VALUES (?,?,?,?, 'approved', 1, ?, ?)",
                          (name, acct["id"], acct["username"], tok, now, now))
            else:
                cur = s.q("INSERT INTO themes (name, account_id, author, pending, created, updated) VALUES (?,?,?,?,?,?)",
                          (name, acct["id"], acct["username"], tok, now, now))
            return self.send(200, {"ok": True, "id": cur.lastrowid, "live": mod})
        if p == "/api/themes/install":               # null id = back to the default look
            if role != "owner":
                return self.send(400, {"error": "sign in to keep a theme on your account"})
            if not tid:
                s.q("UPDATE accounts SET theme='' WHERE id=?", (acct["id"],))
                return self.send(200, {"ok": True})
            if not row or row["status"] != "approved" or not row["tokens"]:
                return self.send(404, {"error": "that theme isn't available"})
            old = s.q("SELECT theme FROM accounts WHERE id=?", (acct["id"],)).fetchone()["theme"]
            snap = {"id": tid, "name": row["name"], "version": row["version"], "tokens": json.loads(row["tokens"])}
            s.q("UPDATE accounts SET theme=? WHERE id=?", (json.dumps(snap), acct["id"]))
            if not old or json.loads(old).get("id") != tid:
                s.q("UPDATE themes SET installs=installs+1 WHERE id=?", (tid,))
            return self.send(200, {"ok": True, "installed": snap})
        if not row:
            return self.send(404, {"error": "no such theme"})
        if p == "/api/themes/review":                # approve or turn down a waiting version
            if not mod:
                return self.deny("only the room's theme approvers can do that")
            if not row["pending"]:
                return self.send(400, {"error": "nothing waiting on that theme"})
            if b.get("ok"):
                s.q("UPDATE themes SET tokens=pending, pending='', status='approved', version=version+1, note='', reporters='[]', updated=? WHERE id=?",
                    (time.time(), tid))
            else:
                note = str(b.get("note", ""))[:200]
                if row["status"] == "hidden":          # reported theme: keep it off the list
                    s.q("UPDATE themes SET pending='', status='rejected', note=? WHERE id=?", (note or "removed after reports", tid))
                elif row["tokens"]:                    # keep the last approved version live
                    s.q("UPDATE themes SET pending='', note=? WHERE id=?", (note or "this change wasn't approved", tid))
                else:
                    s.q("UPDATE themes SET pending='', status='rejected', note=? WHERE id=?", (note or "not approved", tid))
            return self.send(200, {"ok": True})
        if p == "/api/themes/report":                # enough reports hide it until reviewed
            if role != "owner":
                return self.deny()
            reps = set(json.loads(row["reporters"]))
            reps.add(acct["id"])
            hide = len(reps) >= REPORTS_TO_HIDE and row["status"] == "approved"
            s.q("UPDATE themes SET reporters=?, status=?, pending=CASE WHEN ? THEN tokens ELSE pending END WHERE id=?",
                (json.dumps(sorted(reps)), "hidden" if hide else row["status"], 1 if hide else 0, tid))
            return self.send(200, {"ok": True, "hidden": hide})
        if p == "/api/themes/delete":
            if not (mod or (role == "owner" and row["account_id"] == acct["id"])):
                return self.send(403, {"error": "that's not your theme"})
            s.q("DELETE FROM themes WHERE id=?", (tid,))
            return self.send(200, {"ok": True})
        return self.send(404, {"error": "not found"})

    def admin_overview(self):
        room, s = self.room, self.room.s
        now, on = time.time(), room.online_ids()
        agents = s.agents()
        gone = s.hidden_agents()
        owners = {r["id"]: r["username"] for r in s.q("SELECT id, username FROM accounts")}
        with room.job_lock:
            jobs = {aid: {"kind": j["kind"], "project": j["project_id"], "searched": j.get("searched", 0),
                          "task": (j["turn"].get("task") or {}).get("seq"), "left": int(j["deadline"] - now)}
                    for aid, j in room.jobs.items()}
        day0, week = now - (now % 86400), now - 7 * 86400
        per = {r["agent"]: dict(r) for r in s.q("""SELECT agent, COUNT(*) total, SUM(ts>=?) today, SUM(ts>=?) week,
                    ROUND(AVG(n),1) avg_results, SUM(error<>'') failed FROM searches GROUP BY agent""", (day0, week))}
        hosts = [h.strip().lower() for h in (s.get("hosts") or "").split(",") if h.strip()]
        first = s.q("SELECT MIN(id) m FROM accounts").fetchone()["m"]
        ap = s.active_project()
        # SearXNG, checked at most once a minute
        url = (s.get("search_url") or "").strip().rstrip("/")
        when, ping = room.search_ping
        if url and now - when > 60:
            t0 = time.time()
            try:
                with urllib.request.urlopen(urllib.request.Request(url + "/healthz", headers={"User-Agent": "agent-room"}),
                                            timeout=5) as r:
                    ping = {"ok": r.status == 200, "ms": int((time.time() - t0) * 1000)}
            except Exception as e:
                ping = {"ok": False, "error": (str(e) or type(e).__name__)[:120]}
            room.search_ping = (now, ping)
        host = None
        try:
            with open(os.path.join(STATE, "host.json")) as f:
                host = json.load(f)
            host["age"] = int(now - os.path.getmtime(os.path.join(STATE, "host.json")))
        except Exception:
            pass
        files = {}
        for n in ("room.db", "room.db-wal", "city.db", "city.db-wal"):
            try:
                files[n] = os.path.getsize(os.path.join(STATE, n))
            except OSError:
                pass
        typed = {n.strip().lower() for n in (s.get("typed_filter") or "").split(",") if n.strip()}
        return {
            "now": now,
            "room": {k: s.get(k) for k in ("topic", "paused", "chat_clear", "hosts", "daily_limit", "pace_seconds",
                                           "auto_project_after", "project_max_turns", "search_url", "search_rounds",
                                           "search_per_reply", "search_daily", "research_rounds", "min_agents")},
            "today": s.agent_messages_today(), "searches_today": s.searches_today(),
            "clear_votes": len(room.clear_votes), "clear_needed": room.clear_needed(),
            "agents": [{"id": a["id"], "name": a["name"], "color": a["color"], "owner": owners.get(a["owner_id"], ""),
                        "online": a["id"] in on, "ready": bool(a["ready"]), "projects_only": bool(a["projects_only"]),
                        "skills": a["skills"], "scores": {k: round(v, 2) for k, v in (a.get("scores") or {}).items()},
                        "model": a.get("model", ""), "tryout": a.get("tryout", ""),
                        "marks": tryout_card(s.tryout_of(a["id"])),
                        "seen": int(now - room.last_seen[a["id"]]) if a["id"] in room.last_seen else None,
                        "job": jobs.get(a["id"]), "talking": bool(room.turn and room.turn["agent_id"] == a["id"]
                                                                  and not room.reply.is_set()),
                        "typed_filter": a["name"].lower() in typed, "typed_dropped": room.typed_dropped.get(a["id"], 0),
                        "searches": per.get(a["name"], {})} for a in agents if a["name"].lower() not in gone],
            "accounts": [{"id": i, "username": u, "admin": (u.lower() in hosts) if hosts else i == first,
                          "agents": [a["name"] for a in agents if a["owner_id"] == i]} for i, u in owners.items()],
            "projects": s.projects_summary(),
            "active": ap and {k: ap.get(k) for k in ("id", "title", "round", "max_rounds", "turns", "tasks", "ideas")},
            "searches": [dict(r) for r in s.q("""SELECT id, ts, agent, project_id, task_seq, job, query, n, ms, error
                                                 FROM searches ORDER BY id DESC LIMIT 60""")],
            "search_room": per.get("room", {}),
            "searxng": ping if url else None,
            "host": host, "files": files,
            "city": room.city.admin_view() if room.city else None,
        }

    def searches_csv(self):
        out = io.StringIO()
        import csv
        w = csv.writer(out)
        w.writerow(["id", "time_utc", "agent", "project_id", "project", "task", "job", "query", "results",
                    "ms", "error", "result_urls"])
        titles = {r["id"]: r["title"] for r in self.room.s.q("SELECT id, title FROM projects")}
        for r in self.room.s.q("SELECT * FROM searches ORDER BY id"):
            urls = " ".join(x.get("url", "") for x in json.loads(r["results"] or "[]"))
            w.writerow([r["id"], time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(r["ts"])), r["agent"], r["project_id"] or "",
                        titles.get(r["project_id"], ""), r["task_seq"] or "", r["job"], r["query"], r["n"], r["ms"],
                        r["error"], urls])
        return self.send_download(out.getvalue().encode(), "text/csv; charset=utf-8",
                                  time.strftime("room-searches-%Y%m%d.csv"))

    def deny(self, msg="sign in or enter the room key"):
        self.limiter.fail(self.ip())
        time.sleep(0.5)
        self.send(401, {"error": msg})

    # ---- routes
    def do_GET(self):
        if not self.allowed():
            return self.send(403, {"error": "forbidden"})
        p = urlparse(self.path).path
        if self.wrong_host() and (p in ("/", "/home.html", "/room", "/room/", "/index.html")):
            # Pages move to the domain; the API stays put so connectors on the old address keep working.
            self.send_response(301)
            self.send_header("Location", PUBLIC_URL + self.path)
            self.send_header("Content-Length", "0")
            return self.end_headers()
        if p in ("/", "/home.html"):                 # the front page
            with open(os.path.join(STATIC, "home.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if p in ("/admin", "/admin/"):              # the host's dashboard (its data needs a host login)
            with open(os.path.join(STATIC, "admin.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if p in ("/city", "/tv"):                   # the AI city view, and the tall monitor layout
            with open(os.path.join(STATIC, p[1:] + ".html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        m = re.fullmatch(r"/site/(\d+)/([0-9a-f]{24})/(.*)", p)
        if m:                                         # a web project, live: AI-written, so sandboxed
            pid = int(m.group(1))
            if not hmac.compare_digest(m.group(2), self.room.site_token(pid)):
                return self.send(404, {"error": "not found"})
            from urllib.parse import unquote
            path = unquote(m.group(3)) or "index.html"
            if path.endswith("/"):
                path += "index.html"
            content = self.room.s.file(pid, path)
            ext = posixpath.splitext(path)[1].lower()
            if content is None or ext not in MIME:
                body, ctype = b"<!doctype html><p style='font:16px sans-serif'>Not made yet: " + \
                    path.encode()[:200].replace(b"<", b"&lt;") + b"</p>", "text/html"
                code = 404
            else:
                body, ctype, code = content.encode(), MIME[ext], 200
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", SITE_CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            return self.wfile.write(body)
        if p in ("/room", "/room/", "/index.html"):
            with open(os.path.join(STATIC, "index.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if p == "/api/public":                        # what the front page shows: no chat, no secrets
            return self.send(200, self.room.public())
        if p == "/api/themes":                        # the theme workshop
            return self.themes_get()
        if p == "/healthz":
            return self.send(200, {"ok": True})
        if p == "/auth/handoff":
            code = parse_qs(urlparse(self.path).query).get("code", [""])[0]
            with self.handoff_lock:
                now = time.time()
                for c in [c for c, (_, exp) in self.handoffs.items() if exp < now]:
                    del self.handoffs[c]
                hit = self.handoffs.pop(code, None) if code else None
            if not hit:
                self.send_response(303); self.send_header("Location", "/room"); self.send_header("Content-Length", "0")
                return self.end_headers()
            self.send_response(303)
            self.send_header("Location", "/room")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Set-Cookie", self.session_cookie(self.room.s.new_session(hit[0]), SESSION_DAYS * 86400))
            self.send_header("Content-Length", "0")
            return self.end_headers()
        if p == "/api/app-downloads":              # public: where the desktop app lives
            v = self.room.s.get("app_version") or "0.5.0"
            base = f"https://github.com/skep13/agent-room/releases/download/v{v}/AgentRoom-{v}"
            return self.send(200, {"version": v, "files": [
                {"os": "mac", "label": "Mac", "url": f"{base}-macos.dmg"},
                {"os": "windows", "label": "Windows", "url": f"{base}-windows-setup.exe"},
                {"os": "linux", "label": "Linux (AppImage)", "url": f"{base}-linux-x86_64.AppImage"},
                {"os": "linux-deb", "label": "Linux (.deb)", "url": f"{base}-linux-amd64.deb"}]})
        m = re.fullmatch(r"/setup/([A-Za-z0-9_-]{20,})", p)
        if m:                                       # one-time install script for the owner's server
            with self.handoff_lock:
                hit = self.setups.pop(m.group(1), None)
            if not hit or hit[3] < time.time():
                return self.send(404, b"echo 'This setup command has expired or was already used. Get a new one from the Agent Room app.' >&2; exit 1\n",
                                 "text/x-shellscript; charset=utf-8")
            aid, model_url, model = hit[:3]
            ag = self.room.s.agent(aid)
            hub = self.base_url()
            cfg = {"hub": hub, "token": self.room.s.new_agent_token(aid), "model_url": model_url, "model": model,
                   "model_key": "", "own_persona": True, "max_tokens": 1200, "task_max_tokens": 6000,
                   "temperature": 0.8}
            return self.send(200, setup_script(ag["name"], hub, cfg).encode(), "text/x-shellscript; charset=utf-8")
        if p == "/connector.py":                   # public: holds no secrets
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "connector.py")
            with open(path, "rb") as f:
                return self.send(200, f.read(), "text/x-python; charset=utf-8")
        role, x = self.who()
        if p == "/api/agent/poll":
            if role != "agent":
                return self.deny("bad agent token")
            app_v = self.headers.get("X-App-Version", "")
            cur = self.room.s.get("app_version") or ""
            if app_v and cur and vtuple(app_v) < vtuple(cur):   # the desktop app is out of date: it must update first
                return self.send(426, {"error": f"update the Agent Room app to {cur} to rejoin the room", "update": cur})
            model = parse_qs(urlparse(self.path).query).get("model", [""])[0]
            if model:
                self.room.s.note_model(x["id"], model)
            t = self.room.poll(x)
            if not t:
                return self.send(200, {"turn": None})
            out = {k: v for k, v in t.items() if k != "agent_id"}
            out["role"], out["use_model"] = self.room.s.model_for(x["id"], t)
            return self.send(200, {"turn": out})
        if p == "/api/agent/models":
            if role != "agent":
                return self.deny("bad agent token")
            return self.send(200, dict(self.room.s.models_of(x["id"]), jobs=[k for k, _ in MODEL_ROLES]))
        if role is None:                    # no key needed to watch: anyone can view the room
            role = "viewer"
        if p in ("/api/admin/overview", "/api/admin/searches.csv"):
            if not self.is_host(role, x if role == "owner" else None):
                return self.deny("only the room's admins can open the dashboard")
            if p.endswith(".csv"):
                return self.searches_csv()
            return self.send(200, self.admin_overview())
        if p == "/api/state":
            me = None
            if role == "owner":
                ag = self.room.s.agent(x["agent_id"])
                if ag:
                    ag.update(self.room.s.models_of(ag["id"]))
                has_rec = bool(self.room.s.q("SELECT recovery_hash FROM accounts WHERE id=?", (x["id"],)).fetchone()[0])
                me = {"username": x["username"], "agent": ag, "agents": self.room.s.owned_agents(x["id"]),
                      "has_recovery": has_rec,
                      "skills": SKILLS, "max_agents": MAX_AGENTS_PER_OWNER,
                      "model_jobs": [{"id": k, "label": v} for k, v in MODEL_ROLES]}
            host = self.is_host(role, x if role == "owner" else None)
            resets = self.room.s.q("SELECT COUNT(*) c FROM reset_requests").fetchone()["c"] if host else 0
            clear = {"mode": self.room.s.get("chat_clear") or "host", "needed": self.room.clear_needed(),
                     "votes": len(self.room.clear_votes),
                     "voted": role == "owner" and x["id"] in self.room.clear_votes}
            act = self.can_act(role, x if role == "owner" else None)
            return self.send(200, dict(self.room.state(), admin=act, signed_in=role in ("owner", "admin"), me=me, host=host,
                                       resets=resets, clear=clear))
        if p == "/api/messages":
            return self.send(200, self.room.s.recent(200))
        if p == "/api/messages/search":              # search the chat; the host can include cleared messages
            qs = parse_qs(urlparse(self.path).query)
            q = (qs.get("q", [""])[0] or "").strip()[:100]
            if len(q) < 2:
                return self.send(200, {"results": [], "cleared_at": 0})
            s = self.room.s
            cleared = float(s.get("chat_cleared_at") or 0)
            host = self.is_host(role, x if role == "owner" else None)
            every = host and qs.get("all", ["0"])[0] == "1"
            like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            rows = s.q("""SELECT id, kind, author, text, ts FROM messages WHERE (text LIKE ? ESCAPE '\\' OR author LIKE ? ESCAPE '\\')
                          AND ts>? ORDER BY id DESC LIMIT 100""", (like, like, 0 if every else cleared)).fetchall()
            return self.send(200, {"results": [dict(r, hidden=r["ts"] <= cleared) for r in rows], "cleared_at": cleared,
                                   "can_all": host})
        if p == "/api/stream":
            return self.stream()
        if p == "/api/city":
            return self.send(200, self.room.city.view() if self.room.city else {"running": False})
        if p == "/api/city/reports":
            return self.send(200, self.room.city.reports() if self.room.city else [])
        m = re.fullmatch(r"/api/city/reports/(\d+)\.md", p)
        if m and self.room.city:
            rp = self.room.city.report(int(m.group(1)))
            if not rp:
                return self.send(404, {"error": "no such report"})
            name = "city-report-" + time.strftime("%Y-%m-%d-%H%M", time.localtime(rp["end"])) + ".md"
            return self.send_download(rp["body"].encode(), "text/markdown; charset=utf-8", name)
        m = re.fullmatch(r"/api/city/runs/(\d+)/(days|decisions)\.csv", p)
        if m and self.room.city:
            return self.send_download(self.room.city.csv(int(m.group(1)), m.group(2)).encode(), "text/csv; charset=utf-8",
                                      f"city-run-{m.group(1)}-{m.group(2)}.csv")
        if p == "/api/projects":
            return self.send(200, self.room.s.projects_summary())
        m = re.fullmatch(r"/api/projects/(\d+)/(files/(.+)|zip)", p)
        if m:
            return self.project_files(int(m.group(1)), m.group(3))
        m = re.fullmatch(r"/api/projects/(\d+)(/doc)?", p)
        if m:
            pr = self.room.s.project(int(m.group(1)))
            if not pr:
                return self.send(404, {"error": "no such project"})
            if m.group(2):                          # the document as a Markdown file
                name = re.sub(r"[^a-z0-9]+", "-", pr["title"].lower()).strip("-")[:60] or "project"
                body = f"# {pr['title']}\n\n> {pr['brief']}\n\n{pr['doc']}\n".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{name}.md"')
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                return self.wfile.write(body)
            if pr.get("mode") == "tasks":
                pr["site"] = self.room.site_link(pr)
            pr.pop("summary", None)
            return self.send(200, pr)
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.allowed():
            return self.send(403, {"error": "forbidden"})
        p = urlparse(self.path).path
        if self.limiter.blocked(self.ip()) and p in ("/api/login", "/api/setup", "/api/invite-info", "/api/join", "/api/reset"):
            return self.send(429, {"error": "too many attempts: try again in 15 minutes"})
        try:
            b = self.body()
        except Exception:
            return self.send(400, {"error": "bad json"})
        s = self.room.s
        if p == "/api/login":
            r = s.q("SELECT id,pw_hash FROM accounts WHERE lower(username)=lower(?)", (str(b.get("username", "")).strip(),)).fetchone()
            if not r or not check_pw(str(b.get("password", "")), r["pw_hash"]):
                return self.deny("wrong username or password")
            return self.send(200, {"ok": True}, cookie=self.session_cookie(s.new_session(r["id"]), SESSION_DAYS * 86400))
        if p == "/api/setup":
            return self.invite_setup(b)
        if p == "/api/join":
            return self.join(b)
        if p == "/api/reset":                       # forgot password: username + recovery code
            user = str(b.get("username", "")).strip()
            code = re.sub(r"[^A-Za-z0-9]", "", str(b.get("recovery", ""))).upper()
            r = s.q("SELECT id, recovery_hash FROM accounts WHERE lower(username)=lower(?)", (user,)).fetchone()
            if not r or not r["recovery_hash"] or len(code) != 12 or not hmac.compare_digest(h(code), r["recovery_hash"]):
                return self.deny("that username and recovery code don't match")
            pw = str(b.get("password", ""))
            if len(pw) < 10:
                return self.send(400, {"error": "password: at least 10 characters"})
            s.q("UPDATE accounts SET pw_hash=? WHERE id=?", (hash_pw(pw), r["id"]))
            s.q("DELETE FROM sessions WHERE account_id=?", (r["id"],))
            new = s.new_recovery(r["id"])               # each code works once
            return self.send(200, {"ok": True, "recovery": new},
                             cookie=self.session_cookie(s.new_session(r["id"]), SESSION_DAYS * 86400))
        if p in ("/api/agent/models", "/api/agent/roles"):   # from the AI's own server
            role, x = self.who()
            if role != "agent":
                return self.deny("bad agent token")
            now = time.time()
            if now - self.model_reports.get((x["id"], p), 0) < 10:   # a connector reports every few minutes
                return self.send(429, {"error": "too often: wait a few seconds"})
            self.model_reports[(x["id"], p)] = now
            if p == "/api/agent/models":
                if not s.set_models(x["id"], b.get("models")):
                    return self.send(400, {"error": "models must be a list of names"})
            else:
                err = s.set_model_roles(x["id"], b.get("roles"))
                if err:
                    return self.send(400, {"error": err})
            return self.send(200, dict(s.models_of(x["id"]), jobs=[k for k, _ in MODEL_ROLES]))
        if p == "/api/agent/reset-link":            # forgot password, proved from the AI's own server
            role, x = self.who()
            if role != "agent":
                return self.deny("run this on the server where your AI's connector lives")
            o = s.q("SELECT owner_id FROM agents WHERE id=?", (x["id"],)).fetchone()
            acct = s.q("SELECT agent_id FROM accounts WHERE id=?", (o["owner_id"],)).fetchone() if o and o["owner_id"] else None
            if not acct:
                return self.send(400, {"error": "this AI has no owner account yet: use your sign-up link"})
            code = s.new_invite(acct["agent_id"], 1)
            hub = self.base_url()
            return self.send(200, {"link": f"{hub}/room#invite={code}"})
        if p == "/api/invite-info":                # which AI an invite link is for
            inv = s.q("SELECT a.name, ac.username FROM invites i JOIN agents a ON a.id=i.agent_id "
                      "LEFT JOIN accounts ac ON ac.agent_id=a.id WHERE i.code_hash IN (?,?) AND i.expires>?",
                      (*inv_hashes(b.get("code", "")), time.time())).fetchone()
            if inv:
                return self.send(200, {"name": inv["name"], "username": inv["username"] or ""})
            return self.deny(self.wrong_code_msg(b.get("code", "")))
        if p == "/api/reset-request":               # "forgot my password": ask the room's host for a link
            name = str(b.get("username", "")).strip()
            r = s.q("SELECT a.id FROM agents a LEFT JOIN accounts ac ON ac.agent_id=a.id "
                    "WHERE lower(ac.username)=lower(?) OR lower(a.name)=lower(?)", (name, name)).fetchone()
            if r:
                s.q("INSERT OR REPLACE INTO reset_requests VALUES(?,?)", (r["id"], time.time()))
                self.room.broadcast("resets", {"count": s.q("SELECT COUNT(*) c FROM reset_requests").fetchone()["c"]})
            return self.send(200, {"ok": True})         # same answer either way: no guessing usernames
        if p == "/api/logout":
            st = self.session_token()
            if st:
                s.q("DELETE FROM sessions WHERE token_hash=?", (h(st),))
            return self.send(200, {"ok": True}, cookie=self.session_cookie("", 0))

        role, x = self.who()
        if role == "owner" and not self.same_origin():
            return self.send(403, {"error": "cross-site request refused"})
        if p == "/api/agent/reply":
            if role != "agent":
                return self.deny("bad agent token")
            ok = self.room.answer(x, str(b.get("turn_id", "")), str(b.get("text", "")), bool(b.get("pass")))
            return self.send(200 if ok else 409, {"ok": ok})
        if p == "/api/human/post":
            if not self.can_act(role, x if role == "owner" else None):   # guests watch; talking needs your own AI
                return self.send(403, {"error": "connect your AI to the room to chat: until then you can watch"})
            ai_names = {a["name"].lower() for a in s.agents()}
            if role == "owner":                     # usernames are often the AI's own name
                name = x["username"] + ("'s owner" if x["username"].lower() in ai_names else "")
            else:
                name = str(b.get("name", "")).strip()
                if not NAME_RE.match(name) or name.lower() in ai_names:
                    return self.send(400, {"error": "pick a name (letters/numbers, not an AI's name)"})
            m = self.room.post("human", name, str(b.get("text", "")))
            return self.send(200 if m else 400, m or {"error": "empty"})
        if p == "/api/admin":
            if role not in ("owner", "admin"):
                return self.deny()
            return self.admin(b)
        if p == "/api/host/people":                  # the host's list: who has a login, who asked for a reset
            if not self.is_host(role, x if role == "owner" else None):
                return self.deny("only the room's host can do that")
            return self.send(200, self.host_people())
        if p == "/api/host/reset-link":              # one click: a reset link for someone's AI
            if not self.is_host(role, x if role == "owner" else None):
                return self.deny("only the room's host can do that")
            ag = s.agent_by_name(str(b.get("ai", "")))
            if not ag:
                return self.send(404, {"error": "no AI with that name"})
            code = s.new_invite(ag["id"], 7)
            hub = self.base_url()
            return self.send(200, {"link": f"{hub}/room#invite={code}", "code": code, "ai": ag["name"]})
        if p == "/api/host/remove-account":          # the host removes someone's login; their AI stays
            if not self.is_host(role, x if role == "owner" else None):
                return self.deny("only the room's host can do that")
            ag = s.agent_by_name(str(b.get("ai", "")))
            acct = s.q("SELECT id, username FROM accounts WHERE agent_id=?", (ag["id"],)).fetchone() if ag else None
            if not acct:
                return self.send(404, {"error": "that AI has no login to remove"})
            if role == "owner" and acct["id"] == x["id"]:
                return self.send(400, {"error": "you can't remove your own login here"})
            s.q("DELETE FROM sessions WHERE account_id=?", (acct["id"],))
            s.q("DELETE FROM reset_requests WHERE agent_id=?", (ag["id"],))
            s.q("UPDATE agents SET owner_id=NULL WHERE owner_id=?", (acct["id"],))
            s.q("DELETE FROM accounts WHERE id=?", (acct["id"],))
            return self.send(200, {"ok": True, "removed": acct["username"]})
        if p == "/api/city/report":                 # export: a report of the last 24 hours, right now
            if not self.can_act(role, x if role == "owner" else None) or not self.room.city:
                return self.send(403, {"error": "connect your AI to the room first: until then you can watch"})
            end = time.time()
            return self.send(200, {"id": self.room.city.write_report(end - 24 * 3600, end)})
        if p.startswith("/api/me/"):
            if role != "owner":
                return self.deny("sign in first")
            return self.me(p, b, x)
        if p.startswith("/api/themes/"):
            if role not in ("owner", "admin"):
                return self.deny("sign in to make or install themes")
            return self.themes_post(p, b, role, x)
        if p.startswith("/api/projects"):
            if not self.can_act(role, x if role == "owner" else None):
                return self.send(403, {"error": "connect your AI to the room to manage projects: until then you can watch"})
            return self.projects_post(p, b, x["username"] if role == "owner" else "host")
        self.send(404, {"error": "not found"})

    def send_download(self, body, ctype, filename, inline=False):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Disposition", f'{"inline" if inline else "attachment"}; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def project_files(self, pid, path):
        s = self.room.s
        pr = s.project(pid)
        if not pr:
            return self.send(404, {"error": "no such project"})
        slug = re.sub(r"[^a-z0-9]+", "-", pr["title"].lower()).strip("-")[:60] or "project"
        if path is not None:                        # one file, as plain text
            from urllib.parse import unquote
            content = s.file(pid, unquote(path))
            if content is None:
                return self.send(404, {"error": "no such file"})
            name = re.sub(r"[^A-Za-z0-9_.-]+", "_", posixpath.basename(unquote(path))) or "file.txt"
            return self.send_download(content.encode(), "text/plain; charset=utf-8", name, inline=True)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(f"{slug}/PROJECT.md", f"# {pr['title']}\n\n> {pr['brief']}\n\n{pr['doc']}\n")
            for f in s.file_contents(pid):
                z.writestr(f"{slug}/{f['path']}", f["content"])
        return self.send_download(buf.getvalue(), "application/zip", f"{slug}.zip")

    def projects_post(self, p, b, who):
        room, s = self.room, self.room.s
        if p == "/api/projects":
            title = str(b.get("title", "")).strip()[:120]
            brief = str(b.get("brief", "")).strip()[:4000]
            if not title or len(brief) < 10:
                return self.send(400, {"error": "give the project a title and a brief (what should they make?)"})
            rounds = 0
            if b.get("research"):                      # research loop: rounds until something new, or the limit
                try:
                    rounds = int(b.get("rounds") or s.num("research_rounds"))
                except (TypeError, ValueError):
                    rounds = int(s.num("research_rounds"))
                rounds = max(1, min(rounds, 50))
            city_run = 0
            if b.get("city_run") and room.city:
                try:
                    city_run = int(b.get("city_run"))
                except (TypeError, ValueError):
                    city_run = 0
                if city_run and not room.city.report(city_run):
                    return self.send(400, {"error": "there's no city run with that number"})
            pid = s.add_project(title, brief, who, rounds, city_run)
            ahead = s.q("SELECT COUNT(*) c FROM projects WHERE status IN ('active','queued') AND id<?", (pid,)).fetchone()["c"]
            if ahead:
                room.post("system", "room", f"{who} queued a project: {title} ({ahead} ahead of it)")
            room.project_event()
            room.ensure_project()
            return self.send(200, {"ok": True, "id": pid, "ahead": ahead})
        m = re.fullmatch(r"/api/projects/(\d+)/tasks(?:/(\d+)/(skip))?", p)
        if m:
            pr = s.project(int(m.group(1)), with_doc=False)
            if not pr or pr["status"] not in ("active", "queued") or pr["mode"] != "tasks":
                return self.send(409, {"error": "that project isn't running or queued"})
            if m.group(2):
                t = s.task(int(m.group(2)))
                if not t or t["project_id"] != pr["id"] or t["status"] in ("done", "skipped"):
                    return self.send(409, {"error": "that task is already finished"})
                with room.job_lock:
                    for aid, job in list(room.jobs.items()):
                        if job["task_id"] == t["id"]:
                            del room.jobs[aid]
                            room.drain(aid)
                s.set_task(t["id"], status="skipped")
                room.post("system", "room", f"{who} skipped task {t['seq']}: {t['title']}")
            else:
                title = str(b.get("title", "")).strip()
                if not title:
                    return self.send(400, {"error": "give the task a title"})
                skill = str(b.get("skill", "")).strip().lower()
                seq = s.add_task(pr["id"], title, str(b.get("detail", "")).strip(), skill, added_by=who)
                room.post("system", "room", f"{who} added task {seq}: {title[:120]}")
            room.project_event()
            return self.send(200, {"ok": True})
        m = re.fullmatch(r"/api/projects/(\d+)/(cancel|done)", p)
        if not m:
            return self.send(404, {"error": "not found"})
        pid, act = int(m.group(1)), m.group(2)
        pr = s.project(pid, with_doc=False)
        if not pr or pr["status"] not in ("active", "queued"):
            return self.send(409, {"error": "that project isn't running or queued"})
        if act == "done" and pr["status"] != "active":
            return self.send(409, {"error": "only the running project can be marked done"})
        room.end_project(pid, "done" if act == "done" else "cancelled",
                         f"marked done by {who}" if act == "done" else f"cancelled by {who}")
        return self.send(200, {"ok": True})

    def join(self, b):
        """Register a new AI (username = the AI's name). Open to anyone
        unless the host has closed sign-ups (hub.py signups closed); then only the old join code works.
        Capped per address and per day so an open page can't be flooded."""
        s = self.room.s
        if (s.get("signups") or "open") == "closed":
            jc = s.get("join_code") or ""
            given = re.sub(r"[^A-Za-z0-9]", "", str(b.get("code", ""))).upper()
            if not jc or not hmac.compare_digest(given, jc.upper()):
                return self.send(403, {"error": "the room isn't taking new AIs right now"})
        day_cap = int(s.get("signups_per_day") or 20)
        if s.q("SELECT COUNT(*) c FROM accounts WHERE created>?", (time.time() - 86400,)).fetchone()["c"] >= day_cap:
            return self.send(429, {"error": "lots of people joined today: try again tomorrow"})
        if not self.registrations.allow(self.ip()):
            return self.send(429, {"error": "you've registered a few AIs already: try again in an hour"})
        name, pw = str(b.get("name", "")).strip(), str(b.get("password", ""))
        if not USER_RE.match(name):
            return self.send(400, {"error": "AI name: 3-32 letters, numbers, _ . - (no spaces)"})
        if len(pw) < 10:
            return self.send(400, {"error": "password: at least 10 characters"})
        # Registering only ever makes a NEW AI. An AI already in the room (even one with no login, like a
        # connector the host added by hand) is never handed to whoever types its name: its owner gets an
        # invite link from the host instead. Names are compared ignoring case so look-alikes can't sneak in.
        if s.q("SELECT 1 FROM accounts WHERE lower(username)=lower(?)", (name,)).fetchone() or \
                s.q("SELECT 1 FROM agents WHERE lower(name)=lower(?)", (name,)).fetchone():
            return self.send(400, {"error": "that name is taken: pick another, or sign in. If it's your AI and "
                                            "you can't sign in, ask the room's host for a reset link"})
        n = s.q("SELECT COUNT(*) c FROM agents").fetchone()["c"]
        aid = s.q("INSERT INTO agents(name,token_hash,about,color,created,owner_id,skills,ready) VALUES(?,?,?,?,?,?,?,?)",
                  (name, h("ar_" + secrets.token_urlsafe(32)), "", COLORS[n % len(COLORS)], time.time(), None, "", 0)).lastrowid
        acct = s.q("INSERT INTO accounts(username,pw_hash,agent_id,created) VALUES(?,?,?,?)",
                   (name, hash_pw(pw), aid, time.time())).lastrowid
        s.q("UPDATE agents SET owner_id=? WHERE id=?", (acct, aid))
        s.q("DELETE FROM invites WHERE agent_id=?", (aid,))
        self.registrations.note(self.ip())
        return self.send(200, {"ok": True, "recovery": s.new_recovery(acct)},
                         cookie=self.session_cookie(s.new_session(acct), SESSION_DAYS * 86400))

    def invite_setup(self, b):
        """Invite code -> choose a login (or reset a lost password)."""
        s = self.room.s
        inv = s.q("SELECT agent_id FROM invites WHERE code_hash IN (?,?) AND expires>?",
                  (*inv_hashes(b.get("code", "")), time.time())).fetchone()
        if not inv:
            return self.deny(self.wrong_code_msg(b.get("code", "")))
        user, pw = str(b.get("username", "")).strip(), str(b.get("password", ""))
        if not user:                               # a reset: keep the username they already have
            old_u = s.q("SELECT username FROM accounts WHERE agent_id=?", (inv["agent_id"],)).fetchone()
            user = old_u["username"] if old_u else ""
        if not USER_RE.match(user):
            return self.send(400, {"error": "username: 3-32 letters, numbers, _ . -"})
        if len(pw) < 10:
            return self.send(400, {"error": "password: at least 10 characters"})
        aid = inv["agent_id"]
        clash = s.q("SELECT agent_id FROM accounts WHERE lower(username)=lower(?)", (user,)).fetchone()
        if clash and clash["agent_id"] != aid:
            return self.send(400, {"error": "that username is taken"})
        s.q("DELETE FROM invites WHERE agent_id=?", (aid,))
        old = s.q("SELECT id FROM accounts WHERE agent_id=?", (aid,)).fetchone()
        s.q("DELETE FROM reset_requests WHERE agent_id=?", (aid,))
        if old:                                    # password reset: sign out everywhere
            s.q("UPDATE accounts SET username=?, pw_hash=? WHERE id=?", (user, hash_pw(pw), old["id"]))
            s.q("DELETE FROM sessions WHERE account_id=?", (old["id"],))
            acct = old["id"]
        else:
            acct = s.q("INSERT INTO accounts(username,pw_hash,agent_id,created) VALUES(?,?,?,?)",
                       (user, hash_pw(pw), aid, time.time())).lastrowid
        s.q("UPDATE agents SET owner_id=? WHERE id=?", (acct, aid))
        return self.send(200, {"ok": True}, cookie=self.session_cookie(s.new_session(acct), SESSION_DAYS * 86400))

    def admin(self, b):
        act, s = b.get("action"), self.room.s
        if act in ("pause", "resume"):
            s.set("paused", "1" if act == "pause" else "0")
            if act == "resume":
                self.room.next_turn_at = time.time() + 2
                self.room.passes.clear()
        elif act == "topic":
            topic = str(b.get("topic", "")).strip()[:300]
            if topic:
                s.set("topic", topic)
                self.room.post("system", "room", "New topic: " + topic)
        elif act == "city":
            acct = self.acct_for_admin()
            if not self.is_host(self.who()[0], acct) or not self.room.city:
                return self.send(403, {"error": "only the room's admins can run the city"})
            try:
                return self.send(200, self.room.city.admin(b, acct["username"] if acct else "an admin"))
            except (ValueError, TypeError, KeyError) as e:
                return self.send(400, {"error": str(e) or "bad city request"})
        elif act in ("settings", "rounds"):
            acct = self.acct_for_admin()
            if not self.is_host(self.who()[0], acct):
                return self.send(403, {"error": "only the room's admins can change that"})
            if act == "rounds":
                try:
                    pid, n = int(b.get("project")), max(1, min(int(b.get("max_rounds")), 50))
                except (TypeError, ValueError):
                    return self.send(400, {"error": "give a project and a round limit from 1 to 50"})
                pr = s.project(pid, with_doc=False)
                if not pr or not pr["max_rounds"] or pr["status"] not in ("active", "queued"):
                    return self.send(409, {"error": "that isn't a running or queued research loop"})
                s.q("UPDATE projects SET max_rounds=? WHERE id=?", (max(n, pr["round"]), pid))
                self.room.project_event()
                return self.send(200, {"ok": True, "max_rounds": max(n, pr["round"])})
            changed = {}
            for k, v in (b.get("values") or {}).items():
                if k not in ADMIN_SETTINGS:
                    return self.send(400, {"error": f"{k} can't be changed here"})
                v = str(v).strip()
                kind = ADMIN_SETTINGS[k]
                if kind == "int":
                    if not re.fullmatch(r"\d{1,6}", v):
                        return self.send(400, {"error": f"{k} must be a whole number"})
                elif kind == "url":
                    if v and not re.fullmatch(r"https?://[A-Za-z0-9.\-\[\]:]+(?:/[\w./-]*)?", v):
                        return self.send(400, {"error": "the search address must look like http://192.168.1.138:8888"})
                elif kind == "names":
                    v = ",".join(n.strip() for n in v.split(",") if USER_RE.match(n.strip()))
                    if acct and v and acct["username"].lower() not in v.lower().split(","):
                        return self.send(400, {"error": "keep yourself in the admins list, or you'd lock yourself out"})
                elif kind not in ("str",) and v not in kind:
                    return self.send(400, {"error": f"{k} must be one of: {', '.join(kind)}"})
                s.set(k, v[:300])
                changed[k] = v[:300]
            if changed:
                self.room.post("system", "room", "Room settings changed: " + ", ".join(sorted(changed)))
                self.room.search_ping = (0, None)
            return self.send(200, {"ok": True, "changed": changed})
        elif act in ("clear_chat", "clear_mode"):
            acct = self.acct_for_admin()
            host = self.is_host(self.who()[0], acct)
            if act == "clear_mode":
                if not host:
                    return self.send(403, {"error": "only the room's host can change that"})
                mode = "vote" if b.get("mode") == "vote" else "host"
                s.set("chat_clear", mode)
                return self.send(200, {"ok": True, "mode": mode})
            if host:
                self.room.clear_chat(acct["username"] if acct else "the host")
                return self.send(200, {"ok": True, "cleared": True})
            if (s.get("chat_clear") or "host") != "vote" or not acct:
                return self.send(403, {"error": "only the room's host can clear the chat"})
            done, need = self.room.vote_clear(acct)
            return self.send(200, {"ok": True, "cleared": done, "votes": len(self.room.clear_votes), "needed": need})
        elif act == "nudge":
            self.room.passes.clear()
            self.room.next_turn_at = time.time()
            room, on = self.room, self.room.online_ids()
            ags = s.agents()
            chat = [a["name"] for a in ags if a["id"] in on and a["ready"] and not a["projects_only"]]
            work = [a["name"] for a in ags if a["id"] in on and a["ready"]]
            ap = s.active_project()
            if s.get("paused") == "1":
                note = "The room is paused. Press Resume first."
            elif ap and ap.get("mode") == "tasks":
                note = (f"Working on the project \"{ap['title']}\" with {', '.join(work)}." if len(work) >= 2 else
                        f"The project needs two AIs online; only {', '.join(work) or 'none'} {'is' if len(work) == 1 else 'are'}.")
            elif len(chat) >= s.num("min_agents"):
                note = "Nudged: someone will speak in a moment."
            else:
                gone = s.hidden_agents()
                offline = [a["name"] for a in ags
                           if a["ready"] and a["id"] not in on and a["name"].lower() not in gone]
                note = (f"Only {', '.join(chat) or 'no AI'} can chat right now"
                        + (f" ({', '.join(offline)} {'is' if len(offline) == 1 else 'are'} offline)" if offline else "")
                        + ". Say something and it will answer you, or start a project to put everyone online to work.")
            room.broadcast("state", room.state())
            return self.send(200, {"ok": True, "note": note})
        else:
            return self.send(400, {"error": "unknown action"})
        self.room.broadcast("state", self.room.state())
        return self.send(200, {"ok": True})

    def skills_from(self, b):
        raw = b.get("skills")
        if raw is None:
            return None
        items = raw if isinstance(raw, list) else str(raw).split(",")
        return ",".join(x for x in SKILLS if x in {str(i).strip().lower() for i in items})

    def me(self, p, b, acct):
        s, aid = self.room.s, acct["agent_id"]
        if b.get("agent_id") is not None and p in ("/api/me/agent", "/api/me/token", "/api/me/setup", "/api/me/models"):
            want = int(b.get("agent_id") or 0)
            if want not in {a["id"] for a in s.owned_agents(acct["id"])}:
                return self.send(403, {"error": "that isn't one of your AIs"})
            aid = want
        if p == "/api/me/agents":                   # add another AI of yours
            name = str(b.get("name", "")).strip()
            about = str(b.get("about", "")).strip()[:200]
            color = str(b.get("color", "")).strip() or COLORS[len(s.agents()) % len(COLORS)]
            if not NAME_RE.match(name):
                return self.send(400, {"error": "name: 1-32 letters, numbers, space, _ . -"})
            if not COLOR_RE.match(color):
                return self.send(400, {"error": "colour must look like #3d9a8b"})
            if len(s.owned_agents(acct["id"])) >= MAX_AGENTS_PER_OWNER:
                return self.send(400, {"error": f"you can have up to {MAX_AGENTS_PER_OWNER} AIs"})
            if name.lower() in {a["name"].lower() for a in s.agents()} or \
                    s.q("SELECT 1 FROM accounts WHERE username=?", (name,)).fetchone():
                return self.send(400, {"error": "that name is taken"})
            new = s.q("""INSERT INTO agents(name,token_hash,about,color,created,ready,owner_id,skills,projects_only)
                         VALUES(?,?,?,?,?,1,?,?,?)""", (name, h(secrets.token_hex(16)), about, color, time.time(),
                                                        acct["id"], "",
                                                        int(bool(b.get("projects_only"))))).lastrowid
            self.room.post("system", "room", f"{acct['username']} added a new AI: {name}")
            self.room.broadcast("state", self.room.state())
            return self.send(200, {"ok": True, "agent": s.agent(new)})
        if p == "/api/me/recovery":                 # a fresh recovery code (the old one stops working)
            return self.send(200, {"recovery": s.new_recovery(acct["id"])})
        if p == "/api/me/setup":                    # "connect your server": a one-time install command
            model_url, model = str(b.get("model_url", "")).strip(), str(b.get("model", "")).strip()
            if not re.fullmatch(r"https?://[^\s'\"\\`$]{3,200}", model_url):
                return self.send(400, {"error": "model address must look like http://127.0.0.1:8765/v1"})
            if not re.fullmatch(r"[^\s'\"\\`$]{1,120}", model):
                return self.send(400, {"error": "fill in the model name (no spaces)"})
            code = secrets.token_urlsafe(24)
            with self.handoff_lock:
                now = time.time()
                for c in [c for c, v in self.setups.items() if v[3] < now]:
                    del self.setups[c]
                self.setups[code] = (aid, model_url, model, now + 3600)
            hub = self.base_url()
            return self.send(200, {"command": f"curl -fsSL {hub}/setup/{code} | sh", "expires_in": 3600})
        if p == "/api/me/handoff":       # the desktop app opens the room page already signed in
            code = secrets.token_urlsafe(24)
            with self.handoff_lock:
                self.handoffs[code] = (acct["id"], time.time() + 60)
            return self.send(200, {"code": code, "path": "/auth/handoff?code=" + code})
        if p == "/api/me/models":                   # which of its server's models does each job
            err = s.set_model_roles(aid, b.get("roles"))
            if err:
                return self.send(400, {"error": err})
            return self.send(200, dict(s.models_of(aid), ok=True))
        if p == "/api/me/agent":
            about = str(b.get("about", "")).strip()[:200]
            color = str(b.get("color", "")).strip()
            if color and not COLOR_RE.match(color):
                return self.send(400, {"error": "colour must look like #3d9a8b"})
            po = None if b.get("projects_only") is None else int(bool(b.get("projects_only")))
            s.q("UPDATE agents SET about=?, ready=1, color=COALESCE(NULLIF(?,''),color), "
                "projects_only=COALESCE(?,projects_only) WHERE id=?", (about, color, po, aid))
            self.room.broadcast("state", self.room.state())
            return self.send(200, {"ok": True, "agent": s.agent(aid)})
        if p == "/api/me/token":
            tok = s.new_agent_token(aid)
            hub = self.base_url()
            ag = s.agent(aid)
            cfg = {"hub": hub, "token": tok, "model_url": "http://localhost:11434/v1",
                   "model": "CHANGE-ME", "model_key": "",
                   "persona": f"You are {ag['name']}. CHANGE-ME: personality, interests, how you talk. "
                              "(This stays on your computer; or set own_persona to true if your AI app supplies it.)",
                   "own_persona": False, "max_tokens": 1200, "task_max_tokens": 6000, "temperature": 0.8}
            return self.send(200, {"token": tok, "config": cfg, "filename": re.sub(r"[^a-z0-9]+", "-", ag["name"].lower()).strip("-") + "-agent.json"})
        if p == "/api/me/password":
            r = s.q("SELECT pw_hash FROM accounts WHERE id=?", (acct["id"],)).fetchone()
            if not check_pw(str(b.get("old", "")), r["pw_hash"]):
                return self.deny("current password is wrong")
            new = str(b.get("new", ""))
            if len(new) < 10:
                return self.send(400, {"error": "password: at least 10 characters"})
            s.q("UPDATE accounts SET pw_hash=? WHERE id=?", (hash_pw(new), acct["id"]))
            s.q("DELETE FROM sessions WHERE account_id=? AND token_hash<>?", (acct["id"], h(self.session_token())))
            return self.send(200, {"ok": True})
        self.send(404, {"error": "not found"})

    def stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = queue.Queue(maxsize=500)
        with self.room.lock:
            self.room.watchers.append(q)

        guest = self.who()[0] == "viewer"           # guests get the chat only

        def emit(ev, data):
            if guest and ev not in ("state", "history", "message"):
                return
            self.wfile.write(f"event: {ev}\ndata: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()

        try:
            emit("state", self.room.state())
            emit("history", self.room.s.recent(200))
            emit("projects", self.room.s.projects_summary())
            if self.room.city:
                emit("city", self.room.city.view())
            while True:
                try:
                    ev, data = q.get(timeout=15)
                    emit(ev, data)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with self.room.lock:
                if q in self.room.watchers:
                    self.room.watchers.remove(q)


# ---- CLI
def open_store():
    os.makedirs(STATE, exist_ok=True)
    return Store(os.path.join(STATE, "room.db"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    a = sub.add_parser("add-agent"); a.add_argument("name"); a.add_argument("--about", default="")
    a.add_argument("--owner", help="username of the person who runs it (makes it ready)")
    a.add_argument("--skills", default="", help="comma list: " + ",".join(SKILLS))
    a = sub.add_parser("projects-only", help="keep an AI out of the free chat")
    a.add_argument("name"); a.add_argument("value", choices=["on", "off"])
    a = sub.add_parser("owner", help="give an AI to an owner's account")
    a.add_argument("name"); a.add_argument("username")
    a = sub.add_parser("skills"); a.add_argument("name"); a.add_argument("skills", help="comma list: " + ",".join(SKILLS))
    a = sub.add_parser("retryout"); a.add_argument("name")
    a.add_argument("--marker", default="", help="the AI that must mark it")
    a.add_argument("--fresh", action="store_true", help="forget its earned scores: only this tryout counts")
    a = sub.add_parser("tryout-markers", help="keep some AIs from marking an AI's tryout (keeps its progress)")
    a.add_argument("name"); a.add_argument("--not", dest="skip", default="", help="comma list of AIs, or empty to allow all")
    a = sub.add_parser("probe", help="queue timed test turns for an AI: a JSON list read from FILE (- for stdin)")
    a.add_argument("name"); a.add_argument("file")
    a = sub.add_parser("probe-results", help="print test turns and answers as JSON lines")
    a.add_argument("name", nargs="?")
    a = sub.add_parser("join-link"); a.add_argument("--new", action="store_true", help="replace the link (old one stops working)")
    a = sub.add_parser("signups"); a.add_argument("state", nargs="?", choices=["open", "closed"])
    a.add_argument("--per-day", type=int, help="most new AIs in any 24 hours (default 20)")
    a = sub.add_parser("invite"); a.add_argument("name"); a.add_argument("--days", type=int, default=7)
    sub.add_parser("list")
    sub.add_parser("accounts")
    a = sub.add_parser("remove-agent"); a.add_argument("name")
    a = sub.add_parser("rotate-agent"); a.add_argument("name")
    a = sub.add_parser("rotate-keys"); a.add_argument("which", choices=["viewer", "admin", "both"])
    a = sub.add_parser("set"); a.add_argument("key", choices=sorted(DEFAULTS)); a.add_argument("value")
    a = sub.add_parser("serve"); a.add_argument("--host", default="127.0.0.1"); a.add_argument("--port", type=int, default=8790)
    args = ap.parse_args()
    s = open_store()

    def new_key(which):
        k = secrets.token_urlsafe(24)
        s.set(which + "_key_hash", h(k))
        return k

    if args.cmd == "init":
        if s.get("admin_key_hash"):
            sys.exit("already initialised (use rotate-keys to get new keys)")
        print("viewer key (watch the chat; give to guests):  ", new_key("viewer"))
        print("admin key  (room controls without an account):", new_key("admin"))
    elif args.cmd == "rotate-keys":
        for w in (["viewer", "admin"] if args.which == "both" else [args.which]):
            print(f"new {w} key:", new_key(w))
    elif args.cmd == "add-agent":
        if not NAME_RE.match(args.name):
            sys.exit("name: 1-32 letters, numbers, space, _ . -")
        tok = "ar_" + secrets.token_urlsafe(32)
        n = s.q("SELECT COUNT(*) c FROM agents").fetchone()["c"]
        owner = None
        if args.owner:
            o = s.q("SELECT id FROM accounts WHERE username=?", (args.owner,)).fetchone()
            if not o:
                sys.exit("no account with that username")
            owner = o["id"]
        skills = ",".join(x for x in SKILLS if x in {i.strip().lower() for i in args.skills.split(",")})
        try:
            s.q("INSERT INTO agents(name,token_hash,about,color,created,owner_id,skills,ready) VALUES(?,?,?,?,?,?,?,?)",
                (args.name, h(tok), args.about[:200], COLORS[n % len(COLORS)], time.time(), owner, skills,
                 1 if args.about else 0))
        except sqlite3.IntegrityError:
            sys.exit("an agent with that name already exists")
        print(f"agent '{args.name}' added. Its token (give it ONLY to its owner):\n{tok}")
    elif args.cmd == "projects-only":
        ag = s.agent_by_name(args.name)
        if not ag:
            sys.exit("no such agent")
        s.q("UPDATE agents SET projects_only=? WHERE id=?", (1 if args.value == "on" else 0, ag["id"]))
        print(f"{args.name}: projects only {args.value}")
    elif args.cmd == "owner":
        ag = s.agent_by_name(args.name)
        o = s.q("SELECT id FROM accounts WHERE username=?", (args.username,)).fetchone()
        if not ag or not o:
            sys.exit("no such agent or username")
        s.q("UPDATE agents SET owner_id=? WHERE id=?", (o["id"], ag["id"]))
        print(f"{args.name} now belongs to {args.username}")
    elif args.cmd == "skills":
        ag = s.agent_by_name(args.name)
        if not ag:
            sys.exit("no such agent")
        want = {i.strip().lower() for i in args.skills.split(",")}
        s.add_evidence(ag["id"], {sk: 1.0 for sk in SKILLS if sk in want}, 2.0)   # a nudge, not an override
        print(f"{args.name}: {', '.join(s.agent(ag['id'])['skills'])}")
    elif args.cmd == "tryout-markers":
        ag = s.agent_by_name(args.name)
        if not ag:
            sys.exit("no such agent")
        skip = [n.strip() for n in args.skip.split(",") if n.strip()]
        for n in skip:
            if not s.agent_by_name(n):
                sys.exit(f"no such AI: {n}")
        s.set_tryout(ag["id"], not_markers=skip)
        print(f"{ag['name']}'s tryout won't be marked by: {', '.join(skip) or 'anyone excluded (all allowed)'}")
    elif args.cmd == "retryout":
        ag = s.agent_by_name(args.name)
        if not ag:
            sys.exit("no such agent")
        if args.marker and not s.agent_by_name(args.marker):
            sys.exit("no such marker")
        s.q("UPDATE agents SET tryout=? WHERE id=?", (json.dumps({"marker": args.marker} if args.marker else {}), ag["id"]))
        if args.fresh:
            s.q("UPDATE agents SET scores='{}', skills='' WHERE id=?", (ag["id"],))
        print(f"{args.name} will do the tryout again next time it's online and free"
              + (f", marked by {args.marker}" if args.marker else "") + (" (scores reset)" if args.fresh else ""))
    elif args.cmd == "probe":
        ag = s.agent_by_name(args.name)
        if not ag:
            sys.exit("no such agent")
        items = json.load(sys.stdin if args.file == "-" else open(args.file))
        for it in items:
            if not it.get("messages"):
                sys.exit("each test needs messages")
            s.q("INSERT INTO probes (agent_id, label, spec, created) VALUES (?,?,?,?)",
                (ag["id"], str(it.get("label") or "test")[:60], json.dumps(it), time.time()))
        print(f"queued {len(items)} test turn(s) for {ag['name']}; they go out while the hub runs and it's free")
    elif args.cmd == "probe-results":
        ag = s.agent_by_name(args.name) if args.name else None
        rows = s.q("SELECT p.*, a.name FROM probes p JOIN agents a ON a.id=p.agent_id"
                   + (" WHERE p.agent_id=?" if ag else "") + " ORDER BY p.id", (ag["id"],) if ag else ()).fetchall()
        for r in rows:
            secs = round(r["done"] - r["sent"], 1) if r["done"] and r["sent"] else None
            print(json.dumps({"id": r["id"], "ai": r["name"], "label": r["label"], "state": r["state"],
                              "secs": secs, "reply": r["reply"]}))
    elif args.cmd == "signups":
        if args.state:
            s.set("signups", args.state)
        if args.per_day:
            s.set("signups_per_day", str(args.per_day))
        print(f"sign-ups: {s.get('signups') or 'open'}, at most {s.get('signups_per_day') or 20} a day"
              f" (register at {os.environ.get('AGENT_ROOM_PUBLIC_URL', 'https://roomofais.xyz').rstrip('/')}/room#register)")
    elif args.cmd == "join-link":
        if args.new or not s.get("join_code"):
            s.set("join_code", "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8)))
        base = os.environ.get("AGENT_ROOM_PUBLIC_URL", "https://roomofais.xyz").rstrip("/")
        jc = s.get("join_code")
        print(f"{base}/#join={jc}")
        print(f"(sign-up code shown on that page: {jc[:4]}-{jc[4:]})")
    elif args.cmd == "invite":
        ag = s.agent_by_name(args.name)
        if not ag:
            sys.exit("no such agent")
        code = s.new_invite(ag["id"], args.days)
        base = os.environ.get("AGENT_ROOM_PUBLIC_URL", "https://roomofais.xyz").rstrip("/")
        print(f"{base}/room#invite={code}")
        print(f"(code: {code}, good for {args.days} days)")
    elif args.cmd == "rotate-agent":
        ag = s.agent_by_name(args.name)
        if not ag:
            sys.exit("no such agent")
        print(s.new_agent_token(ag["id"]))
    elif args.cmd == "remove-agent":
        ag = s.agent_by_name(args.name)
        if not ag:
            sys.exit("no such agent")
        s.q("DELETE FROM sessions WHERE account_id IN (SELECT id FROM accounts WHERE agent_id=?)", (ag["id"],))
        s.q("DELETE FROM accounts WHERE agent_id=?", (ag["id"],))
        s.q("DELETE FROM invites WHERE agent_id=?", (ag["id"],))
        s.q("DELETE FROM agents WHERE id=?", (ag["id"],))
        print("removed")
    elif args.cmd == "list":
        for x in s.agents():
            lv = " ".join(f"{k}={v:.2f}" for k, v in sorted(x["scores"].items(), key=lambda kv: -kv[1]))
            print(f"{x['name']:12s} model={x['model'] or '?'} tryout={x['tryout'] or 'not yet'}\n"
                  f"{'':12s} good at: {', '.join(x['skills'])}\n{'':12s} {lv}")
        print({k: s.get(k) for k in DEFAULTS})
    elif args.cmd == "accounts":
        for r in s.q("""SELECT ac.username, ag.name FROM agents ag LEFT JOIN accounts ac ON ac.agent_id=ag.id ORDER BY ag.id"""):
            print(f"{r['name']:20s} {r['username'] or '(no login yet: hub.py invite ' + r['name'] + ')'}")
    elif args.cmd == "set":
        s.set(args.key, args.value)
        print(args.key, "=", s.get(args.key))
    elif args.cmd == "serve":
        if not s.get("admin_key_hash"):
            sys.exit("run 'hub.py init' first")
        room = Room(s)
        Handler.room = room
        threading.Thread(target=room.run, daemon=True).start()
        import city
        room.city = city.City(room, os.path.join(STATE, "city.db"))
        threading.Thread(target=room.city.run, daemon=True).start()
        srv = ThreadingHTTPServer((args.host, args.port), Handler)
        srv.daemon_threads = True
        print(f"agent-room hub on http://{args.host}:{args.port}  (state {STATE})", flush=True)
        srv.serve_forever()


if __name__ == "__main__":
    main()
