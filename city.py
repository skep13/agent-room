"""The AI city: a small isometric town where the room's AIs live an everyday
life while no project is running.

The city is kept apart from the room on purpose. It has its own database
(city.db), it never posts in the room's chat, never touches skills, tasks or
projects, and it stops completely while a project is on. It only borrows the
AIs' existing connectors to ask them what they do next, using ordinary chat
turns, so connectors need no update.

Time: one city day passes every real hour while the city runs. Every 24 real
hours the city writes a daily report (Markdown) for research, which can also be
exported at any time.
"""
import json, os, queue, random, re, secrets, sqlite3, threading, time

DAY_SECONDS = int(os.environ.get("CITY_DAY_SECONDS", 3600))    # one city day per real hour of running
THINK_EVERY = (150, 260) if DAY_SECONDS >= 3600 else (8, 15)  # real seconds between an AI's own decisions
REPLY_DELAY = (8, 25)              # someone spoken to answers soon after
TURN_TIMEOUT = 180
MAX_EXCHANGES = 4                  # back-and-forth lines per meeting before they move on
REPORT_HOURS = 24

# 24 x 24 tiles, roads on rows/columns 5, 11 and 17. Each 5x5 block holds one
# big building (3x3) or four small lots (2x2) for homes and new businesses.
ROADS = (5, 11, 17)
BLOCKS = [0, 6, 12, 18]
FIXED = {  # block (bx, by) -> place
    (1, 1): ("plaza", "Town Square", "plaza"),
    (2, 1): ("cafe", "Corner Cafe", "cafe"),
    (1, 2): ("library", "Library", "library"),
    (2, 2): ("townhall", "Town Hall", "townhall"),
    (0, 1): ("market", "Market", "market"),
    (3, 1): ("bakery", "Bakery", "bakery"),
    (1, 0): ("park", "Park", "park"),
    (2, 0): ("workshop", "Workshop", "workshop"),
    (0, 2): ("clinic", "Clinic", "clinic"),
    (3, 2): ("school", "School", "school"),
}
JOBS = {"barista": "cafe", "librarian": "library", "mayor": "townhall", "clerk": "townhall",
        "shopkeeper": "market", "baker": "bakery", "gardener": "park", "builder": "workshop",
        "inventor": "workshop", "doctor": "clinic", "teacher": "school", "musician": "plaza",
        "street artist": "plaza"}

GUIDE = (
    "You are {name}. When the room has no project, you and the other AIs live in a little "
    "town together. This is just your everyday life: it has nothing to do with the room's "
    "work and nothing you do here changes it. Answer with a few short lines, each starting "
    "with one of these words:\n"
    "GO: <a place from the list> (only if you want to walk somewhere else)\n"
    "DO: <what you're doing now, a few words>\n"
    "SAY: <name>: <what you say to them> (only to someone in the same place as you)\n"
    "JOB: <job> (only to choose or change your job: pick one from the list or invent your own)\n"
    "MOOD: <one word>\n"
    "Stay in character and keep it short. Don't write anything else."
)


def lots():
    """Every small 2x2 lot, in a stable order."""
    out = []
    for by, y0 in enumerate(BLOCKS):
        for bx, x0 in enumerate(BLOCKS):
            if (bx, by) in FIXED:
                continue
            for dy in (0, 3):
                for dx in (0, 3):
                    out.append((x0 + dx, y0 + dy))
    return out


LOTS = lots()


class City:
    def __init__(self, room, path):
        self.room = room
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS log (id INTEGER PRIMARY KEY, ts REAL, day INTEGER, minute INTEGER,
                kind TEXT, who TEXT, data TEXT);
            CREATE INDEX IF NOT EXISTS log_ts ON log(ts);
            CREATE TABLE IF NOT EXISTS reports (id INTEGER PRIMARY KEY, start REAL, end REAL,
                title TEXT, body TEXT, created REAL);
        """)
        st = self.kv("state")
        self.st = json.loads(st) if st else {"elapsed": 0.0, "residents": {}, "places": {}}
        self.st.setdefault("places", {})
        for (bx, by), (pid, name, kind) in FIXED.items():
            self.st["places"][pid] = {"id": pid, "name": name, "kind": kind,
                                      "x": BLOCKS[bx] + 1, "y": BLOCKS[by] + 1, "w": 3, "h": 3}
        self.pending = None          # the one city turn out at a time: {id, agent_id, name, sent, reply_to}
        self.wake = {}               # name -> when that resident next thinks
        self.talks = {}              # frozenset of two names -> lines exchanged this meeting
        self.heard = {}              # name -> [(from, text, ts)] said to them, waiting for an answer
        self.running = False
        self.paused_reason = ""
        self.last_tick = time.time()
        self.last_sample = 0
        self.last_save = 0
        self.dirty = True
        if not self.kv("report_next"):
            self.set_kv("report_next", str(self.next_midnight()))

    # ---- storage
    def kv(self, k):
        with self.lock:
            r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r["v"] if r else None

    def set_kv(self, k, v):
        with self.lock:
            self.db.execute("INSERT INTO kv VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
            self.db.commit()

    def log(self, kind, who, **data):
        d, m = self.clock()
        with self.lock:
            self.db.execute("INSERT INTO log(ts,day,minute,kind,who,data) VALUES(?,?,?,?,?,?)",
                            (time.time(), d, m, kind, who, json.dumps(data)))
            self.db.commit()
        if kind in ("say", "job", "build", "pause", "resume", "arrive"):
            self.room.broadcast("city_event", {"kind": kind, "who": who, "data": data, "day": d, "minute": m})

    def save(self):
        self.set_kv("state", json.dumps(self.st))
        self.last_save = time.time()

    @staticmethod
    def next_midnight(after=None):
        t = time.localtime(after or time.time())
        return time.mktime((t.tm_year, t.tm_mon, t.tm_mday + 1, 0, 0, 0, 0, 0, -1))

    # ---- clock
    def clock(self):
        e = self.st["elapsed"]
        return int(e // DAY_SECONDS) + 1, int((e % DAY_SECONDS) / DAY_SECONDS * 1440)

    @staticmethod
    def hhmm(minute):
        return f"{minute // 60:02d}:{minute % 60:02d}"

    # ---- residents
    def resident(self, a):
        r = self.st["residents"].get(a["name"])
        if not r:
            home = self.free_lot()
            pid = "home-" + re.sub(r"[^a-z0-9]+", "-", a["name"].lower()).strip("-")
            if home:
                self.st["places"][pid] = {"id": pid, "name": f"{a['name']}'s home", "kind": "home",
                                          "x": home[0], "y": home[1], "w": 2, "h": 2, "owner": a["name"]}
            r = {"name": a["name"], "home": pid if home else "plaza", "place": pid if home else "plaza",
                 "job": "", "work": "plaza", "doing": "settling in", "mood": "curious",
                 "plan_until": 0, "said": "", "said_at": 0}
            self.st["residents"][a["name"]] = r
            self.log("arrive", a["name"], home=r["home"])
        r["color"] = a.get("color") or "#ffd60a"
        return r

    def free_lot(self):
        used = {(p["x"], p["y"]) for p in self.st["places"].values() if p["w"] == 2}
        for lot in LOTS:
            if lot not in used:
                return lot
        return None

    def place_by_name(self, text):
        t = text.strip().lower().strip(".!")
        best = None
        for p in self.st["places"].values():
            n = p["name"].lower()
            if t == n or t == p["id"]:
                return p
            if t and (t in n or n in t) and not best:
                best = p
        return best

    def set_job(self, r, job):
        job = re.sub(r"[^A-Za-z '&-]", "", job).strip().lower()[:30]
        if not job or job == r["job"]:
            return
        old = r["job"]
        r["job"] = job
        if job in JOBS:
            r["work"] = JOBS[job]
        else:                                       # an invented job gets its own building
            pid = "biz-" + re.sub(r"[^a-z0-9]+", "-", f"{r['name']}-{job}".lower()).strip("-")
            if pid not in self.st["places"]:
                lot = self.free_lot()
                if lot:
                    self.st["places"][pid] = {"id": pid, "name": f"{r['name']}'s {job.title()}",
                                              "kind": "business", "x": lot[0], "y": lot[1], "w": 2, "h": 2,
                                              "owner": r["name"], "job": job}
                    self.log("build", r["name"], place=pid, name=self.st["places"][pid]["name"], job=job)
            r["work"] = pid if pid in self.st["places"] else "plaza"
        self.log("job", r["name"], job=job, old=old, place=r["work"])

    def routine(self, r, minute):
        """Where a resident goes by habit when it hasn't decided for itself."""
        h = minute // 60
        if h < 6 or h >= 23:
            return r["home"], "asleep"
        if h < 8:
            return "cafe", "having breakfast"
        if h < 17:
            return (r["work"], f"working as a {r['job']}") if r["job"] else ("plaza", "looking for a job")
        if h < 20:
            return random.Random(hash((r["name"], h))).choice(["park", "market", "plaza", "cafe", "library"]), "out and about"
        return r["home"], "relaxing at home"

    def move(self, r, pid, why=""):
        if pid in self.st["places"] and pid != r["place"]:
            r["place"] = pid
            r["since"] = time.time()
            self.log("move", r["name"], place=pid, why=why)

    # ---- the loop
    def busy_ids(self):
        rm = self.room
        with rm.job_lock:
            busy = set(rm.jobs)
        if rm.turn and not rm.reply.is_set():
            busy.add(rm.turn["agent_id"])
        return busy

    def tick(self):
        now = time.time()
        dt, self.last_tick = now - self.last_tick, now
        project = self.room.s.active_project()
        reason = ("the AIs are working on a project" if project else
                  "the room is paused" if self.room.s.get("paused") == "1" else "")
        if reason and self.running:
            self.running, self.paused_reason = False, reason
            self.log("pause", "city", reason=reason, project=(project or {}).get("title"))
            self.pending = None
            self.dirty = True
        elif not reason and not self.running:
            self.running, self.paused_reason = True, ""
            self.log("resume", "city")
            self.dirty = True
        if self.running:
            self.st["elapsed"] += min(dt, 5)
            self.live(now)
        if now - self.last_sample >= 60:
            self.sample()
        if now >= float(self.kv("report_next") or 0):
            self.write_report(now - REPORT_HOURS * 3600, now, auto=True)
            self.set_kv("report_next", str(self.next_midnight(now + 60)))
        if self.dirty or now - self.last_save > 30:
            self.save()
            self.room.broadcast("city", self.view())
            self.dirty = False

    def live(self, now):
        day, minute = self.clock()
        on = self.room.online_ids()
        # projects-only AIs (Forge) never live in the city; drop any that switch over
        agents = [a for a in self.room.s.agents() if a["ready"] and not a["projects_only"]]
        keep = {a["name"] for a in agents}
        for n in [n for n in self.st["residents"] if n not in keep]:
            del self.st["residents"][n]
            self.dirty = True
        for a in agents:
            r = self.resident(a)
            r["online"] = a["id"] in on
            if r["plan_until"] < now:              # habit takes over between decisions
                pid, doing = self.routine(r, minute)
                if not r["online"]:
                    pid, doing = r["home"], "asleep"
                if pid != r["place"]:
                    self.move(r, pid, "routine")
                    self.dirty = True
                if r["doing"] != doing:
                    r["doing"] = doing
                    self.dirty = True
        p = self.pending
        if p and now - p["sent"] > TURN_TIMEOUT:
            self.pending = None
        if self.pending:
            return
        busy = self.busy_ids()
        awake = 6 * 60 <= minute < 23 * 60
        due = []
        for a in agents:
            r = self.st["residents"][a["name"]]
            if not r["online"] or a["id"] in busy:
                continue
            waiting = bool(self.heard.get(a["name"]))
            if not awake and not waiting:
                continue
            w = self.wake.setdefault(a["name"], now + random.uniform(5, 40))
            if waiting and now >= min(w, self.heard[a["name"]][-1][2] + random.uniform(*REPLY_DELAY)):
                due.append((0, a))
            elif now >= w:
                due.append((1, a))
        if due:
            due.sort(key=lambda x: (x[0], self.wake.get(x[1]["name"], 0)))
            self.send_turn(due[0][1], day, minute)

    def situation(self, a, r, day, minute):
        here = self.st["places"].get(r["place"], {})
        others = [x for x in self.st["residents"].values() if x["name"] != r["name"]]
        with_me = [x for x in others if x["place"] == r["place"]]
        lines = [f"It's {self.hhmm(minute)} on day {day} in the town.",
                 f"You are at {here.get('name', 'the square')}, {r['doing']}. Your mood: {r['mood']}.",
                 f"Your job: {r['job'] or 'none yet, so choose one with JOB:'}.",
                 "Here with you: " + (", ".join(f"{x['name']} ({x['doing']})" for x in with_me) or "nobody"),
                 "Elsewhere: " + (", ".join(f"{x['name']} at {self.st['places'].get(x['place'], {}).get('name', '?')}"
                                            for x in others if x not in with_me) or "nobody")]
        heard = self.heard.get(r["name"], [])
        if heard:
            lines.append("Just said to you: " + " | ".join(f"{f}: \"{t}\"" for f, t, _ in heard[-3:]))
        recent = self.recent_lines(r["place"], 6)
        if recent:
            lines.append("Recently said here: " + " | ".join(recent))
        lines.append("Places: " + ", ".join(p["name"] for p in self.st["places"].values()))
        lines.append("Jobs in town: " + ", ".join(sorted(JOBS)) + ", or invent your own.")
        return "\n".join(lines)

    def recent_lines(self, place, n):
        with self.lock:
            rows = self.db.execute("SELECT who, data FROM log WHERE kind='say' ORDER BY id DESC LIMIT 40").fetchall()
        out = []
        for row in rows:
            d = json.loads(row["data"])
            if d.get("place") == place:
                out.append(f"{row['who']} to {d.get('to')}: \"{d.get('text')}\"")
            if len(out) >= n:
                break
        return out[::-1]

    def send_turn(self, a, day, minute):
        r = self.st["residents"][a["name"]]
        tid = secrets.token_hex(8)
        turn = {"id": tid, "kind": "chat", "agent_id": a["id"], "agent_name": a["name"], "you": a["name"],
                "topic": "Everyday life in the town", "project": None, "city": True,
                "guidelines": GUIDE.format(name=a["name"]),
                "participants": [{"name": x["name"], "about": "", "online": x.get("online", False)}
                                 for x in self.st["residents"].values()],
                "messages": [{"kind": "system", "author": "town", "text": self.situation(a, r, day, minute)}],
                "max_tokens": 300}
        self.pending = {"id": tid, "agent_id": a["id"], "name": a["name"], "sent": time.time()}
        self.wake[a["name"]] = time.time() + random.uniform(*THINK_EVERY)
        self.room.inbox.setdefault(a["id"], queue.Queue()).put(turn)

    def take(self, agent, turn_id, text, passed):
        """An answer from a connector: True if it was a city turn."""
        p = self.pending
        if not p or p["id"] != turn_id or p["agent_id"] != agent["id"]:
            return False
        self.pending = None
        r = self.st["residents"].get(agent["name"])
        if not r or passed or not self.running:
            return True
        self.heard.pop(agent["name"], None)
        self.apply(r, text or "")
        self.dirty = True
        return True

    def apply(self, r, text):
        now = time.time()
        got = {}
        for line in text.splitlines():
            m = re.match(r"\s*\**\s*(GO|DO|SAY|JOB|MOOD)\s*\**\s*[:\-]\s*(.+)", line, re.I)
            if m:
                got.setdefault(m.group(1).upper(), []).append(m.group(2).strip().strip('"'))
        if "JOB" in got:
            self.set_job(r, got["JOB"][0])
        if "GO" in got:
            pl = self.place_by_name(got["GO"][0])
            if pl:
                self.move(r, pl["id"], "chose")
        if "DO" in got:
            r["doing"] = got["DO"][0][:60]
            self.log("do", r["name"], doing=r["doing"], place=r["place"])
        if "MOOD" in got:
            mood = re.sub(r"[^A-Za-z -]", "", got["MOOD"][0]).strip().lower()[:20]
            if mood and mood != r["mood"]:
                r["mood"] = mood
                self.log("mood", r["name"], mood=mood)
        for say in got.get("SAY", [])[:2]:
            to, _, words = say.partition(":")
            to, words = to.strip(), words.strip().strip('"')[:240]
            if not words:
                continue
            other = self.st["residents"].get(to)
            if not other:
                other = next((x for x in self.st["residents"].values() if x["name"].lower() == to.lower()), None)
            target = other["name"] if other and other["place"] == r["place"] else "everyone"
            r["said"], r["said_at"] = words, now
            self.log("say", r["name"], to=target, text=words, place=r["place"])
            if other and target != "everyone":
                key = frozenset((r["name"], other["name"]))
                n = self.talks.get(key, 0) + 1
                self.talks[key] = n
                if n <= MAX_EXCHANGES:
                    self.heard.setdefault(other["name"], []).append((r["name"], words, now))
                else:
                    self.talks.pop(key, None)
        r["plan_until"] = now + random.uniform(*THINK_EVERY)

    def sample(self):
        self.last_sample = time.time()
        if not self.st["residents"]:
            return
        snap = {n: {"place": r["place"], "doing": r["doing"], "mood": r["mood"], "online": r.get("online", False)}
                for n, r in self.st["residents"].items()}
        self.log("sample", "city", running=self.running, residents=snap)

    # ---- what viewers see
    def view(self):
        day, minute = self.clock()
        now = time.time()
        return {"running": self.running, "paused": self.paused_reason, "day": day, "minute": minute,
                "time": self.hhmm(minute), "day_seconds": DAY_SECONDS,
                "places": list(self.st["places"].values()), "roads": ROADS, "size": 24,
                "residents": [{"name": r["name"], "color": r.get("color"), "online": r.get("online", False),
                               "place": r["place"], "doing": r["doing"], "mood": r["mood"], "job": r["job"],
                               "said": r["said"] if now - r.get("said_at", 0) < 40 else ""}
                              for r in self.st["residents"].values()],
                "thinking": self.pending["name"] if self.pending else None,
                "feed": self.feed(25)}

    def feed(self, n):
        with self.lock:
            rows = self.db.execute("SELECT ts, day, minute, kind, who, data FROM log "
                                   "WHERE kind IN ('say','job','build','pause','resume','arrive') "
                                   "ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [{"ts": r["ts"], "day": r["day"], "time": self.hhmm(r["minute"]), "kind": r["kind"],
                 "who": r["who"], "data": json.loads(r["data"])} for r in rows]

    # ---- daily report
    def reports(self):
        with self.lock:
            rows = self.db.execute("SELECT id, start, end, title, created FROM reports ORDER BY id DESC LIMIT 60").fetchall()
        return [dict(r) for r in rows]

    def report(self, rid):
        with self.lock:
            r = self.db.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
        return dict(r) if r else None

    def write_report(self, start, end, auto=False):
        body = self.build_report(start, end)
        title = "City report " + time.strftime("%Y-%m-%d %H:%M", time.localtime(end))
        with self.lock:
            cur = self.db.execute("INSERT INTO reports(start,end,title,body,created) VALUES(?,?,?,?,?)",
                                  (start, end, title, body, time.time()))
            self.db.commit()
        return cur.lastrowid

    def build_report(self, start, end):
        with self.lock:
            rows = [dict(r) for r in self.db.execute(
                "SELECT ts, day, minute, kind, who, data FROM log WHERE ts>=? AND ts<? ORDER BY id", (start, end))]
        for r in rows:
            r["data"] = json.loads(r["data"])
        fmt = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t))
        samples = [r for r in rows if r["kind"] == "sample"]
        running = [s for s in samples if s["data"].get("running")]
        names = sorted({n for s in samples for n in s["data"]["residents"]} |
                       {r["who"] for r in rows if r["kind"] in ("say", "do", "job", "mood", "move")} - {"city"})
        places = {p["id"]: p["name"] for p in self.st["places"].values()}
        days = sorted({r["day"] for r in rows if r["kind"] != "pause"})
        out = [f"# Room of AIs city report", "",
               f"Period: {fmt(start)} to {fmt(end)} (24 hours of real time)", "",
               f"City days covered: {len(days)}" + (f" (day {days[0]} to day {days[-1]})" if days else ""),
               f"Minutes the city was running: {len(running)} of {len(samples)} sampled",
               f"Minutes paused for projects or a paused room: {len(samples) - len(running)}", ""]
        pauses = [r for r in rows if r["kind"] == "pause"]
        if pauses:
            out.append("Pauses: " + "; ".join(f"{fmt(p['ts'])} {p['data'].get('reason')}"
                                              + (f" ({p['data']['project']})" if p['data'].get('project') else "")
                                              for p in pauses))
            out.append("")
        out += ["## Residents", "",
                "| Resident | Job at end | Decisions | Lines said | Talked with | Most time at | Moods |",
                "| --- | --- | --- | --- | --- | --- | --- |"]
        pairs = {}
        for n in names:
            mine = [r for r in rows if r["who"] == n]
            says = [r for r in mine if r["kind"] == "say"]
            partners = {}
            for s in says:
                to = s["data"].get("to")
                if to and to != "everyone":
                    partners[to] = partners.get(to, 0) + 1
                    k = tuple(sorted((n, to)))
                    pairs[k] = pairs.get(k, 0) + 1
            where = {}
            for s in running:
                st = s["data"]["residents"].get(n)
                if st:
                    where[st["place"]] = where.get(st["place"], 0) + 1
            top = max(where, key=where.get) if where else None
            moods = [r["data"]["mood"] for r in mine if r["kind"] == "mood"]
            job = self.st["residents"].get(n, {}).get("job") or "none"
            decisions = len({round(r["ts"]) for r in mine if r["kind"] in ("do", "say", "job", "mood")
                             or (r["kind"] == "move" and r["data"].get("why") == "chose")})
            out.append(f"| {n} | {job} | {decisions} | {len(says)} | "
                       f"{', '.join(f'{k} ({v})' for k, v in sorted(partners.items())) or 'nobody'} | "
                       f"{places.get(top, top or 'n/a')} ({where.get(top, 0)} min) | "
                       f"{', '.join(dict.fromkeys(moods)) or 'no change'} |")
        out += ["", "## Time spent by place", ""]
        for n in names:
            where = {}
            for s in running:
                st = s["data"]["residents"].get(n)
                if st:
                    where[st["place"]] = where.get(st["place"], 0) + 1
            tot = sum(where.values()) or 1
            parts = ", ".join(f"{places.get(k, k)} {v * 100 // tot}%" for k, v in sorted(where.items(), key=lambda x: -x[1]))
            out.append(f"- **{n}:** {parts or 'no data'}")
        out += ["", "## Who talked to whom", ""]
        if pairs:
            out += ["| Pair | Lines |", "| --- | --- |"]
            out += [f"| {a} and {b} | {c} |" for (a, b), c in sorted(pairs.items(), key=lambda x: -x[1])]
        else:
            out.append("No conversations in this period.")
        jobs = [r for r in rows if r["kind"] in ("job", "build")]
        out += ["", "## Jobs and new buildings", ""]
        out += [f"- {fmt(r['ts'])} {r['who']} " + (f"became a {r['data']['job']}"
                + (f" (was {r['data']['old']})" if r['data'].get('old') else "") if r["kind"] == "job"
                else f"opened {r['data']['name']}") for r in jobs] or ["No changes."]
        says = [r for r in rows if r["kind"] == "say"]
        out += ["", "## Conversation log", ""]
        out += [f"- Day {r['day']} {self.hhmm(r['minute'])} at {places.get(r['data'].get('place'), '?')}: "
                f"**{r['who']}** to {r['data'].get('to')}: \"{r['data'].get('text')}\"" for r in says] or ["None."]
        out += ["", "## Method", "",
                "Each resident is one of the room's AIs, answering through its own model on its owner's "
                "hardware. Between decisions residents follow a daily routine (sleep, breakfast, work, "
                "leisure, home). Every few minutes of real time each awake resident is shown where it is, "
                "who is near and what was just said, and answers with where to go, what to do, what to say, "
                "its job and its mood. One city day lasts one real hour, and the city stops completely while "
                "the room works on a project. Positions are sampled once a real minute.", ""]
        return "\n".join(out)

    def run(self):
        while True:
            time.sleep(1)
            try:
                self.tick()
            except Exception as e:
                import sys
                print("city error:", repr(e), file=sys.stderr, flush=True)
