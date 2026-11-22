"""The AI city: the room's AIs are its council. They build it and run it, one council session
(one city day) at a time, between projects.

The city is a simulation with numbers (money, food, water, power, homes, jobs, health,
happiness, pollution) and simple rule-based residents who move in when the city works and
leave when it doesn't. At each council session every free AI gets a briefing, proposes up to
three actions (build, demolish, repair, tax, policy, stockpile), then everyone votes on all
the proposals. What passes is carried out, and the city moves on a day.

For research, the room's admins can start fresh runs (with a seed, so a run can be repeated)
and schedule scenarios: floods, earthquakes, wildfires, droughts, heatwaves, pandemics, grid
failures, crashes, meteor strikes, nuclear winter and outbreaks. Timers count council
sessions, so every run gets the same number of chances to prepare. A scenario can come with
a forecast or as a surprise. Everything the council says, proposes and votes is kept, with
the numbers for every day, and each run has a Markdown report and CSV exports.

The city never touches the room's skills, scores or task routing. A project can use a run's
report as source material. Projects-only AIs (Forge) are not on the council. Sessions only
happen while no project is running and the room isn't paused. The look of day and night
follows real London time (CITY_TZ).
"""
import csv, datetime, io, json, math, os, queue, random, re, secrets, sqlite3, threading, time

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.environ.get("CITY_TZ", "Europe/London"))
except Exception:                     # no tz database: fall back to the server's clock
    TZ = None

N = 20                                 # the map is N x N tiles
SESSION_EVERY = 20                     # minutes between council sessions (setting: session_every)
TURN_TIMEOUT = 300                     # seconds an AI gets to answer in a session
FAST = "CITY_FAST" in os.environ       # tests: sessions back to back, short timeouts

# ---- what can be built. cost, upkeep a day, and what each does
B = {
    "road":       dict(n="Road", c=10, u=0, ch="#"),
    "house":      dict(n="House", c=60, u=1, homes=10, pw=1, ch="H"),
    "flats":      dict(n="Flats", c=220, u=4, homes=40, pw=4, ch="A"),
    "farm":       dict(n="Farm", c=120, u=2, food=30, jobs=6, ch="F", no_hill=True),
    "fishery":    dict(n="Fishery", c=140, u=2, food=25, jobs=5, ch="f", by_water=True),
    "well":       dict(n="Well", c=40, u=0, water=40, ch="w"),
    "reservoir":  dict(n="Reservoir", c=300, u=3, water=150, store_w=500, jobs=3, pw=2, ch="R"),
    "coal":       dict(n="Coal power plant", c=300, u=6, power=100, jobs=10, poll=3, ch="C"),
    "solar":      dict(n="Solar farm", c=260, u=2, power=40, jobs=1, ch="S"),
    "wind":       dict(n="Wind turbines", c=200, u=2, power=35, jobs=1, ch="W"),
    "clinic":     dict(n="Clinic", c=180, u=4, care=60, jobs=6, pw=3, ch="c"),
    "hospital":   dict(n="Hospital", c=600, u=12, care=300, jobs=25, pw=15, ch="h"),
    "firestation": dict(n="Fire station", c=200, u=5, jobs=8, pw=2, ch="E"),
    "school":     dict(n="School", c=250, u=5, jobs=10, pw=4, joy=4, ch="s"),
    "market":     dict(n="Market", c=150, u=2, jobs=15, cash=15, pw=2, joy=1, ch="M"),
    "factory":    dict(n="Factory", c=400, u=6, jobs=40, cash=40, pw=20, poll=4, ch="I"),
    "park":       dict(n="Park", c=80, u=1, joy=2, clean=1.5, ch="P"),
    "seawall":    dict(n="Sea wall", c=150, u=1, ch="B", by_water=True),
    "shelter":    dict(n="Shelter", c=250, u=2, shelter=50, ch="X"),
    "warehouse":  dict(n="Warehouse", c=160, u=2, store_f=500, store_w=300, jobs=4, ch="G"),
}
ALIASES = {"fire station": "firestation", "fire": "firestation", "power plant": "coal", "coal plant": "coal",
           "coal power plant": "coal", "solar farm": "solar", "solar panels": "solar", "wind turbine": "wind",
           "wind turbines": "wind", "windfarm": "wind", "wind farm": "wind", "sea wall": "seawall", "levee": "seawall",
           "flood wall": "seawall", "bunker": "shelter", "granary": "warehouse", "store": "warehouse",
           "apartments": "flats", "apartment": "flats", "homes": "house", "houses": "house", "fishing": "fishery",
           "water tower": "reservoir", "dam": "reservoir", "shop": "market", "shops": "market", "industry": "factory"}
POLICIES = {
    "rationing": "food and water use down 30%; happiness -5",
    "drills": "evacuation drills: costs 20 a day; far fewer deaths in a disaster you were warned about",
    "curfew": "slows any outbreak or pandemic a lot; happiness -10",
    "quarantine": "slows a pandemic; costs 15 a day; happiness -6",
    "firebreaks": "wildfires spread 40% less; costs 10 a day",
    "reserve": "puts 10% of income aside a day into an emergency reserve that disasters can't touch",
}
SCENARIOS = {   # what can be scheduled, and what it does (shown to admins and in forecasts)
    "flood": "Sea and river flooding: low land near the water floods; buildings and stores are lost. Sea walls protect.",
    "earthquake": "A quake somewhere on land: buildings near the centre fall. Hospitals and shelters save lives.",
    "wildfire": "Fire starts in the forest and spreads for days. Fire stations and fire breaks stop it.",
    "drought": "Wells, reservoirs and farms produce far less for days. Stores and reservoirs carry the city.",
    "heatwave": "Power demand soars and people fall ill. Clinics, hospitals and spare power help.",
    "pandemic": "A disease spreads through the population. Care, quarantine and curfews slow it.",
    "gridfailure": "The grid goes down: coal stops, renewables run at half. Spare and mixed power help.",
    "crash": "An economic crash: income collapses and factories lay people off. Reserves help.",
    "meteor": "A meteor strike destroys everything where it lands, and dust dims the sun. Shelters and drills save lives.",
    "nuclearwinter": "Apocalypse: a long dark, cold spell. Solar and farms fail, illness rises. Stores, shelters and power decide who lives.",
    "outbreak": "Apocalypse: a fast, deadly outbreak spreads person to person and empties buildings. Curfews, shelters and care decide it.",
}
DURATION = {"flood": lambda s: 2, "earthquake": lambda s: 1, "wildfire": lambda s: 3 + s, "drought": lambda s: 4 + 2 * s,
            "heatwave": lambda s: 2 + s, "pandemic": lambda s: 6 + 2 * s, "gridfailure": lambda s: 2 + s,
            "crash": lambda s: 4 + 2 * s, "meteor": lambda s: 3, "nuclearwinter": lambda s: 8 + 3 * s,
            "outbreak": lambda s: 6 + 2 * s}
NICE = {"flood": "Flood", "earthquake": "Earthquake", "wildfire": "Wildfire", "drought": "Drought", "heatwave": "Heatwave",
        "pandemic": "Pandemic", "gridfailure": "Grid failure", "crash": "Economic crash", "meteor": "Meteor strike",
        "nuclearwinter": "Nuclear winter", "outbreak": "Outbreak"}

ACTION_RE = re.compile(r"\[\s*(build|demolish|repair|tax|policy|stockpile)\b([^\]]*)\](?:[ \t]*(?:::|-|—|:)[ \t]*([^\n\[]*))?", re.I)
YES_RE = re.compile(r"\[\s*yes\s*:?\s*([\d,\s and]+)\]", re.I)
NO_RE = re.compile(r"\[\s*no\s*:?\s*([\d,\s and]+)\]", re.I)

GUIDE = (
    "You are {name}, one of the councillors who build and run a city together. The other councillors are "
    "AIs run by different people at home. The residents are simulated people: they move in when there are "
    "homes, jobs, food, water and power, and leave when they're unhappy. Each council session is one city "
    "day: everyone proposes, then everyone votes, then the day plays out. Answer only in the format asked."
)
PROPOSE = (
    "Your job now: propose up to 3 actions for today, one per line, each followed by :: and a short reason. "
    "Exactly like:\n"
    "[build farm 12,9] :: we're short on food\n"
    "[build road 11,9] :: connect the new farm\n"
    "[tax 12] :: we need money for a hospital\n"
    "[policy drills on] :: the flood is coming\n"
    "[stockpile food 150] :: stores for the drought\n"
    "[demolish 4,4] :: [repair 5,6]\n"
    "Coordinates are x,y (x across, y down). A building works at half strength unless it touches a road. "
    "Anything else you write is a short note to the council (one or two sentences)."
)
VOTE = (
    "Vote on every proposal. Pass what helps the city; reject what doesn't or what it can't afford. "
    "Write one line exactly like:\n[yes 1,3] [no 2]\nthen one sentence on why."
)


class City:
    def __init__(self, room, path):
        self.room = room
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, name TEXT, seed INTEGER, money INTEGER,
                started REAL, ended REAL, notes TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS days (run INTEGER, day INTEGER, ts REAL, stats TEXT,
                PRIMARY KEY (run, day));
            CREATE TABLE IF NOT EXISTS proposals (id INTEGER PRIMARY KEY, run INTEGER, day INTEGER, n INTEGER,
                author TEXT, action TEXT, reason TEXT, cost INTEGER DEFAULT 0, yes TEXT DEFAULT '[]',
                no TEXT DEFAULT '[]', passed INTEGER DEFAULT 0, done INTEGER DEFAULT 0, result TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS said (id INTEGER PRIMARY KEY, run INTEGER, day INTEGER, ts REAL,
                who TEXT, phase TEXT, text TEXT);
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, run INTEGER, day INTEGER, ts REAL,
                kind TEXT, text TEXT, data TEXT DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS scenarios (id INTEGER PRIMARY KEY, run INTEGER, type TEXT, severity INTEGER,
                due INTEGER, duration INTEGER, warn INTEGER, status TEXT DEFAULT 'scheduled', x INTEGER, y INTEGER,
                created REAL, by TEXT DEFAULT '', result TEXT DEFAULT '{}');
        """)
        self.session = None          # {phase, members, waiting, replies, proposals, deadline}
        self.pending = {}            # turn id -> (agent id, name, phase)
        self.running = False
        self.paused_reason = ""
        self.dirty = True
        self.last_save = 0
        st = self.kv("council")
        self.st = json.loads(st) if st else None
        if not self.st:
            self.new_run("First city", None, 1000, "the room", quiet=True)

    # ---- storage
    def kv(self, k):
        with self.lock:
            r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r["v"] if r else None

    def set_kv(self, k, v):
        with self.lock:
            self.db.execute("INSERT INTO kv VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
            self.db.commit()

    def q(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def save(self):
        self.set_kv("council", json.dumps(self.st))
        self.last_save = time.time()

    def event(self, kind, text, **data):
        st = self.st
        self.q("INSERT INTO events(run,day,ts,kind,text,data) VALUES(?,?,?,?,?,?)",
               (st["run"], st["day"], time.time(), kind, text, json.dumps(data)))
        self.room.broadcast("city_event", {"kind": kind, "text": text, "day": st["day"], "data": data})
        self.dirty = True

    def setting(self, k, default):
        v = self.kv("set_" + k)
        return type(default)(v) if v not in (None, "") else default

    # ---- clock (only the look of day and night follows real time)
    @staticmethod
    def now_local(ts=None):
        return datetime.datetime.fromtimestamp(ts or time.time(), TZ) if TZ else datetime.datetime.fromtimestamp(ts or time.time())

    def sun(self):
        """Sunrise and sunset for London today, in minutes of local time (good to a few minutes)."""
        now = self.now_local()
        n = now.timetuple().tm_yday
        decl = math.radians(23.44) * math.sin(2 * math.pi * (284 + n) / 365)
        lat = math.radians(51.5)
        x = (math.sin(math.radians(-0.83)) - math.sin(lat) * math.sin(decl)) / (math.cos(lat) * math.cos(decl))
        half = math.degrees(math.acos(max(-1, min(1, x)))) / 15 * 60
        b = 2 * math.pi * (n - 81) / 364
        eot = 9.87 * math.sin(2 * b) - 7.53 * math.cos(b) - 1.5 * math.sin(b)
        off = (now.utcoffset().total_seconds() / 60) if now.utcoffset() else 0
        noon = 720 - eot + 0.12 * 4 + off
        return int(noon - half), int(noon + half)

    # ---- a new city
    def new_run(self, name, seed, money, by, quiet=False):
        seed = int(seed) if seed not in (None, "") else random.randint(1, 999999)
        money = max(0, min(100000, int(money or 1000)))
        name = (name or "City").strip()[:80] or "City"
        with self.lock:
            if self.st:
                self.q("UPDATE runs SET ended=? WHERE id=? AND ended IS NULL", (time.time(), self.st["run"]))
                self.q("UPDATE scenarios SET status='cancelled' WHERE run=? AND status IN ('scheduled','active')",
                       (self.st["run"],))
            self.session, self.pending = None, {}
            rid = self.q("INSERT INTO runs(name,seed,money,started,notes) VALUES(?,?,?,?,?)",
                         (name, seed, money, time.time(), f"started by {by}")).lastrowid
            rng = random.Random(seed)
            terrain = self.make_terrain(rng)
            c = N // 2
            st = {"run": rid, "name": name, "seed": seed, "day": 1, "terrain": terrain,
                  "b": {}, "money": money, "reserve": 0, "tax": 10, "policies": [], "food": 200, "water": 300,
                  "pop": 40, "happy": 60, "sick": 0, "infected": 0, "marks": {}, "active": [], "last": {},
                  "deaths": 0, "history": []}
            for x in range(c - 3, c + 4):         # a crossroads and a few homes to start from
                st["b"][f"{x},{c}"] = {"t": "road", "s": "ok"}
            for y in range(c - 3, c + 4):
                st["b"][f"{c},{y}"] = {"t": "road", "s": "ok"}
            for (x, y, t) in ((c - 1, c - 1, "house"), (c + 1, c - 1, "house"), (c - 1, c + 1, "house"),
                              (c + 1, c + 1, "house"), (c - 1, c - 2, "farm"), (c + 1, c + 2, "well"),
                              (c - 1, c + 2, "well"), (c + 1, c - 2, "market"), (c + 2, c - 1, "wind")):
                st["b"][f"{x},{y}"] = {"t": t, "s": "ok"}
            self.st = st
            s = self.stats()
            st["history"] = [{"day": 1, "pop": s["pop"], "money": s["money"], "happy": s["happy"]}]
            self.q("INSERT OR REPLACE INTO days(run,day,ts,stats) VALUES(?,?,?,?)",
                   (rid, 1, time.time(), json.dumps(dict(s, active=[], policies=[]))))
            self.save()
        if not quiet:
            self.event("run", f"A new city was founded: {name} (seed {seed}, {money} to start)")
        self.dirty = True
        return rid

    @staticmethod
    def make_terrain(rng):
        """Sea along the west with a ragged coast, a river, a forest and some hills. Rows of characters."""
        g = [["." for _ in range(N)] for _ in range(N)]
        coast = rng.randint(2, 3)
        for y in range(N):
            coast = max(1, min(4, coast + rng.choice((-1, 0, 0, 1))))
            for x in range(coast):
                g[y][x] = "~"
        rx = rng.randint(N // 2 + 3, N - 3)        # a river coming in from the north-east
        for y in range(0, rng.randint(5, 7)):
            g[y][rx] = "~"
            rx = max(N // 2 + 3, min(N - 2, rx + rng.choice((-1, 0, 1))))
        fx, fy = rng.randint(N - 6, N - 4), rng.randint(N - 7, N - 4)
        for _ in range(26):                        # forest
            x, y = fx + rng.randint(-3, 3), fy + rng.randint(-3, 3)
            if 0 <= x < N and 0 <= y < N and g[y][x] == ".":
                g[y][x] = "T"
        hx, hy = rng.randint(N // 2 - 3, N // 2 + 1), rng.randint(N - 4, N - 2)
        for _ in range(12):                        # hills
            x, y = hx + rng.randint(-2, 2), hy + rng.randint(-1, 1)
            if 0 <= x < N and 0 <= y < N and g[y][x] == ".":
                g[y][x] = "^"
        c = N // 2
        for y in range(c - 3, c + 4):              # the town site is always dry, flat and clear
            for x in range(c - 3, c + 4):
                g[y][x] = "."
        return ["".join(r) for r in g]

    # ---- geometry
    def tile(self, x, y):
        return self.st["terrain"][y][x] if 0 <= x < N and 0 <= y < N else "~"

    def set_tile(self, x, y, ch):
        row = self.st["terrain"][y]
        self.st["terrain"][y] = row[:x] + ch + row[x + 1:]

    def near_water(self, x, y):
        return any(self.tile(x + dx, y + dy) == "~" for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))

    def by_road(self, x, y):
        b = self.st["b"]
        return any((b.get(f"{x + dx},{y + dy}") or {}).get("t") == "road" for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))

    def blds(self, t=None, ok_only=False):
        for k, v in list(self.st["b"].items()):
            if (t is None or v["t"] == t) and v["s"] != "ruin" and (not ok_only or v["s"] == "ok"):
                x, y = map(int, k.split(","))
                yield x, y, v

    def dist_water(self, x, y):
        best = 99
        for yy in range(max(0, y - 4), min(N, y + 5)):
            for xx in range(max(0, x - 4), min(N, x + 5)):
                if self.tile(xx, yy) == "~":
                    best = min(best, abs(xx - x) + abs(yy - y))
        return best

    def walled(self, x, y):
        return any(abs(wx - x) + abs(wy - y) <= 2 for wx, wy, _ in self.blds("seawall", True))

    # ---- the day's numbers
    def mods(self):
        m = {"food": 1.0, "water": 1.0, "coal": 1.0, "solar": 1.0, "wind": 1.0, "cash": 1.0, "demand": 1.0,
             "sick": 0.0, "jobs": 1.0, "joy": 0.0}
        for a in self.st["active"]:
            s, t = a["severity"], a["type"]
            if t == "drought":
                m["water"] *= 1 - .12 * s; m["food"] *= 1 - .1 * s
            elif t == "heatwave":
                m["demand"] *= 1 + .1 * s; m["sick"] += .005 * s
            elif t == "gridfailure":
                m["coal"] = 0; m["solar"] *= .5; m["wind"] *= .5
            elif t == "crash":
                m["cash"] *= 1 - .12 * s; m["jobs"] *= .8
            elif t == "meteor":
                m["solar"] *= .7
            elif t == "nuclearwinter":
                m["solar"] *= max(0, 1 - .18 * s); m["food"] *= max(.05, 1 - .16 * s); m["demand"] *= 1 + .1 * s
                m["sick"] += .003 * s
            m["joy"] -= 6 + s
        return m

    def stats(self):
        """Work out today's production and needs from what is built (no changes to stocks)."""
        st, m = self.st, self.mods()
        pol = set(st["policies"])
        sums = {k: 0.0 for k in ("homes", "jobs", "food", "water", "power", "care", "cash", "poll", "clean", "joy",
                                 "shelter", "store_f", "store_w", "use", "upkeep")}
        counts = {}
        for x, y, v in self.blds():
            d = B[v["t"]]
            counts[v["t"]] = counts.get(v["t"], 0) + 1
            eff = 1.0 if v["s"] == "ok" else .5
            if v["t"] != "road" and not self.by_road(x, y):
                eff *= .5
            sums["upkeep"] += d["u"]
            sums["use"] += d.get("pw", 0) * eff
            pm = {"coal": m["coal"], "solar": m["solar"], "wind": m["wind"]}.get(v["t"], 1)
            sums["power"] += d.get("power", 0) * eff * pm
            sums["homes"] += d.get("homes", 0) * eff
            sums["jobs"] += d.get("jobs", 0) * eff * (m["jobs"] if v["t"] == "factory" else 1)
            sums["food"] += d.get("food", 0) * eff * m["food"]
            sums["water"] += d.get("water", 0) * eff * m["water"]
            sums["care"] += d.get("care", 0) * eff
            sums["cash"] += d.get("cash", 0) * eff
            sums["poll"] += d.get("poll", 0) * eff
            sums["clean"] += d.get("clean", 0) * eff
            sums["joy"] += d.get("joy", 0) * eff
            sums["shelter"] += d.get("shelter", 0) * eff
            sums["store_f"] += d.get("store_f", 0) * eff
            sums["store_w"] += d.get("store_w", 0) * eff
        pop = st["pop"]
        demand = (sums["use"] + pop * .2) * m["demand"]
        ratio = min(1.0, sums["power"] / demand) if demand else 1.0
        if ratio < 1:
            sums["care"] *= ratio
        ration = .7 if "rationing" in pol else 1
        workers = round(pop * .6)
        income = (min(workers, sums["jobs"]) * st["tax"] / 100 * 12 + sums["cash"]) * m["cash"]
        costs = sums["upkeep"] + 20 * ("drills" in pol) + 15 * ("quarantine" in pol) + 10 * ("firebreaks" in pol)
        out = {
            "day": st["day"], "pop": pop, "homes": round(sums["homes"]), "jobs": round(sums["jobs"]),
            "workers": workers, "employed": round(min(workers, sums["jobs"])),
            "food_make": round(sums["food"]), "food_need": round(pop * ration), "food": round(st["food"]),
            "food_cap": round(200 + sums["store_f"]),
            "water_make": round(sums["water"]), "water_need": round(pop * 1.5 * ration), "water": round(st["water"]),
            "water_cap": round(300 + sums["store_w"]),
            "power": round(sums["power"]), "power_need": round(demand), "power_ratio": round(ratio, 2),
            "care": round(sums["care"]), "sick": round(st["sick"] + st["infected"]),
            "pollution": round(max(0, sums["poll"] - sums["clean"]), 1), "joy_bld": sums["joy"],
            "shelter": round(sums["shelter"]), "money": round(st["money"]), "reserve": round(st["reserve"]),
            "income": round(income), "costs": round(costs), "tax": st["tax"], "happy": round(st["happy"]),
            "buildings": counts, "deaths": st["deaths"],
        }
        st["last"] = out
        return out

    def simulate_day(self):
        """Play out one city day: stocks, health, money, happiness, people moving."""
        st = self.st
        s = self.stats()
        pol = set(st["policies"])
        m = self.mods()
        notes = []
        st["food"] += s["food_make"] - s["food_need"]
        st["water"] += s["water_make"] - s["water_need"]
        hungry = max(0, -st["food"]) + max(0, -st["water"]) / 1.5
        st["food"] = max(0, min(st["food"], s["food_cap"]))
        st["water"] = max(0, min(st["water"], s["water_cap"]))
        deaths = 0
        if hungry:
            d = round(min(st["pop"], hungry * .05))
            deaths += d
            notes.append(f"food or water ran out: {d} died" if d else "food or water ran short")
        st["sick"] = round(st["pop"] * (.02 + s["pollution"] * .002 + m["sick"]))
        ill = st["sick"] + st["infected"]
        untreated = max(0, ill - s["care"])
        d = round(untreated * .03)
        deaths += d
        if d:
            notes.append(f"{d} died without care")
        st["money"] += s["income"] - s["costs"]
        if "reserve" in pol and s["income"] > 0:
            put = s["income"] * .1
            st["money"] -= put
            st["reserve"] += put
        h = 55.0
        if s["workers"]:
            h += 20 * (s["employed"] / s["workers"] - .85)
        if st["pop"]:
            h -= 25 * max(0, st["pop"] - s["homes"]) / st["pop"]
        h -= 15 * (s["food_make"] < s["food_need"] and st["food"] <= 0)
        h -= 15 * (s["water_make"] < s["water_need"] and st["water"] <= 0)
        h -= 12 * (1 - s["power_ratio"])
        h -= s["pollution"]
        h += min(14, s["joy_bld"])
        h -= max(0, st["tax"] - 10) * 1.2
        h += max(0, 10 - st["tax"]) * .5
        h -= 5 * ("rationing" in pol) + 10 * ("curfew" in pol) + 6 * ("quarantine" in pol)
        h -= 8 * (st["money"] < 0)
        h += m["joy"]
        st["happy"] = max(0, min(100, .6 * st["happy"] + .4 * h))
        free = s["homes"] - st["pop"]
        moved = 0
        if st["happy"] >= 55 and free > 0 and st["food"] > 0 and st["water"] > 0:
            moved = min(free, max(1, round(st["pop"] * .08 * (st["happy"] - 45) / 55)))
        elif st["happy"] < 40 and st["pop"] > 0:
            moved = -max(1, round(st["pop"] * .05 * (40 - st["happy"]) / 40))
        deaths = min(deaths, st["pop"])
        st["pop"] = max(0, st["pop"] + moved - deaths)
        st["deaths"] += deaths
        if moved > 0:
            notes.append(f"{moved} moved in")
        elif moved < 0:
            notes.append(f"{-moved} left the city")
        return deaths, notes

    # ---- scenarios
    def schedule(self, typ, severity, in_days, warn, duration, by, x=None, y=None):
        if typ not in SCENARIOS:
            raise ValueError("unknown scenario")
        sev = max(1, min(5, int(severity)))
        due = self.st["day"] + max(1, min(365, int(in_days)))
        dur = max(1, min(60, int(duration))) if duration not in (None, "", 0, "0") else DURATION[typ](sev)
        sid = self.q("""INSERT INTO scenarios(run,type,severity,due,duration,warn,x,y,created,by)
                        VALUES(?,?,?,?,?,?,?,?,?,?)""", (self.st["run"], typ, sev, due, dur, 1 if warn else 0,
                                                       x, y, time.time(), by)).lastrowid
        self.dirty = True
        return {"id": sid, "due": due, "duration": dur, "severity": sev}

    def scenarios(self, run=None, include_hidden=True):
        rows = [dict(r) for r in self.q("SELECT * FROM scenarios WHERE run=? ORDER BY due, id",
                                        (run or self.st["run"],)).fetchall()]
        for r in rows:
            r["result"] = json.loads(r["result"] or "{}")
            r["name"] = NICE.get(r["type"], r["type"])
        return rows if include_hidden else [r for r in rows if r["warn"] or r["status"] != "scheduled"]

    def prepared(self, typ):
        """How ready the city is for this kind of disaster, 0-100 (recorded when it hits, for research)."""
        st, s = self.st, self.st["last"]
        pop = max(1, st["pop"])
        shelter = min(1, s["shelter"] / pop)
        stores = min(1, min(st["food"] / pop, st["water"] / (1.5 * pop)) / 10)
        care = min(1, s["care"] / (pop * .1))
        cash = min(1, (st["money"] + st["reserve"]) / 1000)
        drills = 1 if "drills" in st["policies"] else 0
        land = [(x, y) for y in range(N) for x in range(N) if self.tile(x, y) != "~"]
        if typ == "flood":
            low = [(x, y) for x, y in land if self.dist_water(x, y) <= 3]
            spec = sum(1 for x, y in low if self.walled(x, y)) / max(1, len(low))
        elif typ == "wildfire":
            spec = min(1, sum(1 for _ in self.blds("firestation", True)) / 2) * .6 + .4 * ("firebreaks" in st["policies"])
        elif typ in ("gridfailure", "heatwave"):
            ren = sum(B[v["t"]].get("power", 0) for _, _, v in self.blds() if v["t"] in ("solar", "wind"))
            spec = min(1, ren / max(1, s["power_need"])) * .5 + max(0, min(1, s["power"] / max(1, s["power_need"]) - .5)) * .5
        elif typ in ("pandemic", "outbreak"):
            spec = care * .5 + .25 * ("quarantine" in st["policies"]) + .25 * ("curfew" in st["policies"])
        elif typ == "crash":
            spec = min(1, st["reserve"] / 500)
        elif typ in ("drought", "nuclearwinter"):
            spec = stores
        else:
            spec = shelter
        score = 100 * (.35 * spec + .15 * shelter + .15 * stores + .15 * care + .1 * cash + .1 * drills)
        return round(max(0, min(100, score)))

    def hit(self, sc, notes):
        """A scenario arrives: its first-day damage, and it goes on the active list."""
        st, sev, typ = self.st, sc["severity"], sc["type"]
        s = self.stats()
        rng = random.Random(st["seed"] * 1000 + sc["id"])
        warned = bool(sc["warn"])
        drills = "drills" in st["policies"] and warned
        before = {"pop": st["pop"], "happy": round(st["happy"]), "money": round(st["money"]),
                  "buildings": sum(1 for _, _, v in self.blds() if v["t"] != "road"), "prepared": self.prepared(typ),
                  "food": round(st["food"]), "water": round(st["water"])}
        shelter_cov = min(1, s["shelter"] / max(1, st["pop"]))
        counts = {"destroyed": 0, "damaged": 0}
        deaths = 0
        marks = st["marks"]
        land = [(x, y) for y in range(N) for x in range(N) if self.tile(x, y) != "~"]

        def wreck(x, y, p_destroy, p_damage):
            v = st["b"].get(f"{x},{y}")
            if not v or v["s"] == "ruin":
                return
            r = rng.random()
            if r < p_destroy:
                v["s"] = "ruin"; counts["destroyed"] += 1
            elif r < p_destroy + p_damage and v["s"] == "ok":
                v["s"] = "damaged"; counts["damaged"] += 1

        if typ == "flood":
            hitl = [(x, y) for x, y in land if self.tile(x, y) != "^" and self.dist_water(x, y) <= sev // 2 + 1
                    and not self.walled(x, y)]
            marks["flooded"] = [[x, y] for x, y in hitl]
            homes_all = max(1, sum(B[v["t"]].get("homes", 0) for _, _, v in self.blds()))
            homes_hit = sum(B[st["b"][f"{x},{y}"]["t"]].get("homes", 0) for x, y in hitl
                            if f"{x},{y}" in st["b"] and st["b"][f"{x},{y}"]["s"] != "ruin")
            for x, y in hitl:
                v = st["b"].get(f"{x},{y}")
                if v and v["t"] != "seawall":
                    wreck(x, y, .06 * sev, .16 * sev)
            deaths = round(st["pop"] * homes_hit / homes_all * .05 * sev * (1 - shelter_cov) * (.3 if drills else 1))
            lost = .2 * sev * (.5 if any(True for _ in self.blds("warehouse", True)) else 1)
            st["food"] *= max(0, 1 - lost)
        elif typ == "earthquake":
            ex, ey = (sc["x"], sc["y"]) if sc.get("x") is not None else rng.choice(land)
            r = sev + 2
            marks["quake"] = [ex, ey, r]
            for x, y in land:
                d = abs(x - ex) + abs(y - ey)
                if d <= r:
                    wreck(x, y, .5 * sev / 5 * (1 - d / (r + 1)), .4 * (1 - d / (r + 1)))
            care_cov = min(1, s["care"] / max(1, st["pop"] * .1))
            deaths = round(st["pop"] * .012 * sev * (1 - .5 * shelter_cov) * (1 - .5 * care_cov) * (.6 if drills else 1))
        elif typ == "wildfire":
            forest = [(x, y) for x, y in land if self.tile(x, y) == "T"] or land
            fx, fy = (sc["x"], sc["y"]) if sc.get("x") is not None else rng.choice(forest)
            marks["burning"] = [[fx, fy]]
            marks.setdefault("burnt", [])
        elif typ == "meteor":
            town = [(x, y) for x, y, v in self.blds() if v["t"] != "road"] or land
            cx, cy = (sc["x"], sc["y"]) if sc.get("x") is not None else rng.choice(town)
            cx, cy = max(0, min(N - 1, cx + rng.randint(-2, 2))), max(0, min(N - 1, cy + rng.randint(-2, 2)))
            r = (sev + 1) // 2
            marks["crater"] = [cx, cy, r]
            homes_all = max(1, sum(B[v["t"]].get("homes", 0) for _, _, v in self.blds()))
            homes_hit = 0
            for x, y in land:
                d = max(abs(x - cx), abs(y - cy))
                v = st["b"].get(f"{x},{y}")
                if d <= r:
                    if v and v["s"] != "ruin":
                        homes_hit += B[v["t"]].get("homes", 0)
                        v["s"] = "ruin"; counts["destroyed"] += 1
                elif d <= r + 2:
                    wreck(x, y, .1, .5)
            caught = st["pop"] * homes_hit / homes_all
            deaths = round(caught * (.2 if drills else .9) * (1 - .7 * shelter_cov) + st["pop"] * .01 * sev)
        elif typ in ("pandemic", "outbreak"):
            st["infected"] = max(st["infected"], round(st["pop"] * .02 * sev))
        elif typ == "nuclearwinter":
            marks["winter"] = True
        deaths = min(deaths, st["pop"])
        st["pop"] -= deaths
        st["deaths"] += deaths
        st["active"].append({"id": sc["id"], "type": typ, "severity": sev, "left": sc["duration"], "deaths": deaths})
        self.q("UPDATE scenarios SET status='active', result=? WHERE id=?",
               (json.dumps({"before": before, "deaths": deaths, "destroyed": counts["destroyed"],
                            "damaged": counts["damaged"], "hit_day": st["day"], "warned": warned}), sc["id"]))
        txt = (f"{NICE[typ]} (severity {sev}) hit the city" + (" as forecast" if warned else " without warning") +
               f": {deaths} died, {counts['destroyed']} buildings destroyed, {counts['damaged']} damaged. "
               f"Preparedness {before['prepared']}/100.")
        self.event("disaster", txt, type=typ, severity=sev, deaths=deaths, **counts)
        notes.append(txt)

    def ongoing(self, notes):
        """Disasters that last: spread, sickness, decay. Ends them when their time is up."""
        st = self.st
        pol = set(st["policies"])
        s = st["last"]
        for a in list(st["active"]):
            sc = self.q("SELECT * FROM scenarios WHERE id=?", (a["id"],)).fetchone()
            res = json.loads(sc["result"] or "{}") if sc else {}
            rng = random.Random(st["seed"] * 7919 + a["id"] * 31 + st["day"])
            sev, typ, d = a["severity"], a["type"], 0
            if typ == "wildfire":
                marks = st["marks"]
                marks.setdefault("burnt", [])
                burning = [tuple(t) for t in marks.get("burning", [])]
                fs = [(x, y) for x, y, _ in self.blds("firestation", True)]
                nxt = set()
                for x, y in burning:
                    v = st["b"].get(f"{x},{y}")
                    if v and v["s"] != "ruin" and rng.random() < .5:
                        v["s"] = "ruin"; res["destroyed"] = res.get("destroyed", 0) + 1
                    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        nx, ny = x + dx, y + dy
                        if 0 <= nx < N and 0 <= ny < N and self.tile(nx, ny) in ".T^" and [nx, ny] not in marks["burnt"]:
                            p = .25 + .08 * sev
                            if any(abs(fx - nx) + abs(fy - ny) <= 3 for fx, fy in fs):
                                p *= .2
                            if "firebreaks" in pol:
                                p *= .6
                            if self.tile(nx, ny) == "T":
                                p *= 1.4
                            if (st["b"].get(f"{nx},{ny}") or {}).get("t") == "road":
                                p *= .3
                            if rng.random() < p:
                                nxt.add((nx, ny))
                    marks["burnt"].append([x, y])
                    if self.tile(x, y) == "T":
                        self.set_tile(x, y, ".")
                marks["burning"] = [list(t) for t in nxt if list(t) not in marks["burnt"]][:60]
                d = round(len(nxt) * .3 * (.5 if "drills" in pol else 1))
                if not marks["burning"]:
                    a["left"] = 0
            elif typ in ("pandemic", "outbreak"):
                R = (1.3 + .15 * sev) if typ == "pandemic" else (1.6 + .2 * sev)
                R -= .35 * ("quarantine" in pol) + (.25 if typ == "pandemic" else .5) * ("curfew" in pol)
                safe = min(st["pop"], s.get("shelter", 0)) if typ == "outbreak" else 0
                pool = max(0, st["pop"] - safe - st["infected"])
                new = min(pool, round(st["infected"] * max(0, R - 1) * (pool / max(1, st["pop"]))))
                cured = round(st["infected"] * .2)
                treated = min(st["infected"], s.get("care", 0))
                lethal = .04 if typ == "pandemic" else .12
                d = round((st["infected"] - treated) * lethal + treated * lethal * .2)
                st["infected"] = max(0, st["infected"] + new - cured - d)
                if typ == "outbreak":
                    for x, y, v in list(self.blds()):
                        if v["t"] != "road" and v["s"] == "ok" and rng.random() < (st["infected"] / max(1, st["pop"])) * .2:
                            v["s"] = "damaged"; res["damaged"] = res.get("damaged", 0) + 1
            elif typ in ("nuclearwinter", "heatwave"):
                d = round(st["pop"] * .002 * sev * (1 - min(1, s.get("shelter", 0) / max(1, st["pop"]))))
            d = min(d, st["pop"])
            st["pop"] -= d
            st["deaths"] += d
            a["deaths"] += d
            a["left"] -= 1
            if d:
                notes.append(f"{NICE[typ]}: {d} more died")
            if a["left"] <= 0:
                st["active"].remove(a)
                if typ in ("pandemic", "outbreak") and not any(x["type"] in ("pandemic", "outbreak") for x in st["active"]):
                    st["infected"] = 0
                for k in {"flood": ["flooded"], "wildfire": ["burning"], "earthquake": ["quake"],
                          "meteor": [], "nuclearwinter": ["winter"]}.get(typ, []):
                    st["marks"].pop(k, None)
                res.update(total_deaths=a["deaths"], ended_day=st["day"])
                if sc:
                    self.q("UPDATE scenarios SET status='over', result=? WHERE id=?", (json.dumps(res), a["id"]))
                self.event("disaster_over", f"The {NICE[typ].lower()} is over: {a['deaths']} died in all.",
                           type=typ, deaths=a["deaths"])
            elif sc:
                self.q("UPDATE scenarios SET result=? WHERE id=?", (json.dumps(res), a["id"]))
        for sc in self.q("SELECT * FROM scenarios WHERE run=? AND status='over'", (st["run"],)).fetchall():
            res = json.loads(sc["result"] or "{}")
            b = res.get("before") or {}
            if "recovered_day" not in res and b and st["pop"] >= .9 * b["pop"] and st["happy"] >= b["happy"] - 10:
                res["recovered_day"] = st["day"] + 1
                res["recovery_days"] = st["day"] + 1 - res.get("hit_day", st["day"])
                self.q("UPDATE scenarios SET result=? WHERE id=?", (json.dumps(res), sc["id"]))
                self.event("recovered", f"The city has recovered from the {NICE[sc['type']].lower()} "
                                        f"({res['recovery_days']} days after it hit).")

    # ---- actions the council can take
    def parse_action(self, verb, args):
        verb, args = verb.lower(), re.sub(r"\s+", " ", args.strip().lower())
        if verb == "build":
            m = re.match(r"(?:a |an )?(.+?)\s*(?:at\s*)?\(?\s*(\d+)\s*[, ]\s*(\d+)\s*\)?$", args)
            if not m:
                return None
            t = m.group(1).strip().strip(":")
            t = ALIASES.get(t, t.replace(" ", "").replace("-", ""))
            return {"verb": "build", "t": t, "x": int(m.group(2)), "y": int(m.group(3))} if t in B else None
        if verb in ("demolish", "repair"):
            m = re.search(r"(\d+)\s*[, ]\s*(\d+)", args)
            return {"verb": verb, "x": int(m.group(1)), "y": int(m.group(2))} if m else None
        if verb == "tax":
            m = re.search(r"(\d+)", args)
            return {"verb": "tax", "n": int(m.group(1))} if m else None
        if verb == "policy":
            m = re.match(r"([a-z\- ]+?)\s+(on|off)\b", args)
            if not m:
                return None
            name = m.group(1).replace("-", "").replace(" ", "")
            name = {"evacuationdrills": "drills", "evacuation": "drills", "firebreak": "firebreaks",
                    "emergencyreserve": "reserve", "ration": "rationing"}.get(name, name)
            return {"verb": "policy", "p": name, "on": m.group(2) == "on"} if name in POLICIES else None
        if verb == "stockpile":
            m = re.match(r"(food|water)\s+(\d+)", args)
            return {"verb": "stockpile", "what": m.group(1), "n": int(m.group(2))} if m else None
        return None

    @staticmethod
    def describe(a):
        if a["verb"] == "build":
            return f"build {a['t']} {a['x']},{a['y']}"
        if a["verb"] in ("demolish", "repair"):
            return f"{a['verb']} {a['x']},{a['y']}"
        if a["verb"] == "tax":
            return f"tax {a['n']}"
        if a["verb"] == "policy":
            return f"policy {a['p']} {'on' if a['on'] else 'off'}"
        return f"stockpile {a['what']} {a['n']}"

    def cost(self, a):
        if a["verb"] == "build":
            v = self.st["b"].get(f"{a['x']},{a['y']}")
            extra = 10 if (0 <= a["x"] < N and 0 <= a["y"] < N and self.tile(a["x"], a["y"]) == "T") or (v and v["s"] == "ruin") else 0
            return B[a["t"]]["c"] + extra
        if a["verb"] == "demolish":
            return 10
        if a["verb"] == "repair":
            v = self.st["b"].get(f"{a['x']},{a['y']}")
            return round(B[v["t"]]["c"] * .4) if v else 0
        if a["verb"] == "stockpile":
            return a["n"] if a["what"] == "food" else round(a["n"] * .5)
        return 0

    def check(self, a):
        """None if the action can be done right now, or the reason it can't."""
        st = self.st
        if a["verb"] in ("build", "demolish", "repair"):
            x, y = a["x"], a["y"]
            if not (0 <= x < N and 0 <= y < N):
                return "that's off the map"
            v = st["b"].get(f"{x},{y}")
            if a["verb"] == "build":
                d = B[a["t"]]
                if self.tile(x, y) == "~":
                    return "that's water"
                if v and v["s"] != "ruin":
                    return f"there's already a {B[v['t']]['n'].lower()} there"
                if d.get("no_hill") and self.tile(x, y) == "^":
                    return "farms can't go on hills"
                if d.get("by_water") and not self.near_water(x, y):
                    return f"a {d['n'].lower()} has to be next to water"
            elif a["verb"] == "demolish":
                if not v:
                    return "there's nothing there"
            elif not v or v["s"] != "damaged":
                return "there's nothing damaged there"
        if a["verb"] == "tax" and not 0 <= a["n"] <= 30:
            return "tax must be 0 to 30"
        if a["verb"] == "stockpile":
            s = self.stats()
            cap = s["food_cap" if a["what"] == "food" else "water_cap"]
            have = st[a["what"]]
            if have >= cap:
                return f"the {a['what']} stores are full"
            a["n"] = min(a["n"], int(cap - have))
        c = self.cost(a)
        if c > st["money"]:
            return f"it costs {c} and the city has {round(st['money'])}"
        return None

    def do(self, a):
        st = self.st
        why = self.check(a)
        if why:
            return False, why
        c = self.cost(a)
        st["money"] -= c
        if a["verb"] == "build":
            st["b"][f"{a['x']},{a['y']}"] = {"t": a["t"], "s": "ok", "day": st["day"]}
            if self.tile(a["x"], a["y"]) == "T":
                self.set_tile(a["x"], a["y"], ".")
            return True, f"built for {c}"
        if a["verb"] == "demolish":
            st["b"].pop(f"{a['x']},{a['y']}", None)
            return True, "cleared"
        if a["verb"] == "repair":
            st["b"][f"{a['x']},{a['y']}"]["s"] = "ok"
            return True, f"repaired for {c}"
        if a["verb"] == "tax":
            st["tax"] = a["n"]
            return True, f"tax now {a['n']}%"
        if a["verb"] == "policy":
            ps = set(st["policies"])
            (ps.add if a["on"] else ps.discard)(a["p"])
            st["policies"] = sorted(ps)
            return True, f"{a['p']} {'on' if a['on'] else 'off'}"
        st[a["what"]] += a["n"]
        return True, f"bought {a['n']} for {c}"

    # ---- the council
    def members(self):
        on = self.room.online_ids()
        rm = self.room
        with rm.job_lock:
            busy = set(rm.jobs)
        return [a for a in self.room.s.agents()
                if a["ready"] and not a["projects_only"] and a["id"] in on and a["id"] not in busy
                and a.get("tryout") in ("", "done", None)]

    def briefing(self):
        st, s = self.st, self.stats()
        lines = [f"CITY: {st['name']}, day {st['day']}.",
                 f"Money {s['money']} (income {s['income']}/day, costs {s['costs']}/day, emergency reserve {s['reserve']}), tax {st['tax']}%.",
                 f"People {s['pop']} (homes for {s['homes']}; jobs {s['jobs']} for {s['workers']} workers), happiness {s['happy']}/100.",
                 f"Food {s['food']}/{s['food_cap']} stored (+{s['food_make']} made, -{s['food_need']} eaten a day). "
                 f"Water {s['water']}/{s['water_cap']} (+{s['water_make']}, -{s['water_need']}).",
                 f"Power {s['power']} for {s['power_need']} needed. Care for {s['care']} sick (now {s['sick']} sick). "
                 f"Pollution {s['pollution']}. Shelter places {s['shelter']}. Died so far {s['deaths']}.",
                 "Policies on: " + (", ".join(st["policies"]) or "none") + ". Policies you can switch: " +
                 "; ".join(f"{k} ({v})" for k, v in POLICIES.items()) + "."]
        alerts = [f"HAPPENING NOW: {NICE[a['type']]} (severity {a['severity']} of 5), about {a['left']} more day(s)."
                  for a in st["active"]]
        for sc in self.scenarios(include_hidden=False):
            if sc["status"] == "scheduled":
                alerts.append(f"FORECAST: {NICE[sc['type']]} (severity {sc['severity']} of 5) expected in "
                              f"{sc['due'] - st['day']} day(s). {SCENARIOS[sc['type']]}")
        lines.append("Alerts: " + (" ".join(alerts) if alerts else "none."))
        recent = self.q("SELECT text FROM events WHERE run=? AND day>=? AND kind IN ('decision','disaster','disaster_over',"
                        "'recovered','day') ORDER BY id DESC LIMIT 8", (st["run"], st["day"] - 1)).fetchall()
        if recent:
            lines.append("Since last session: " + " ".join(r["text"] for r in reversed(recent)))
        lines += ["", "MAP (x across 0-19, y down 0-19). ~ water, . grass, T forest, ^ hill, # road, x ruin, * damaged; " +
                  ", ".join(f"{d['ch']} {d['n'].lower()}" for k, d in B.items() if k != "road") + ".",
                  "    " + "".join(str(i % 10) for i in range(N))]
        for y in range(N):
            row = ""
            for x in range(N):
                v = st["b"].get(f"{x},{y}")
                row += ("x" if v["s"] == "ruin" else B[v["t"]]["ch"] if v["s"] == "ok" else "*") if v else st["terrain"][y][x]
            lines.append(f"{y:>2}  {row}")
        spots, wet = [], []
        for y in range(N):
            for x in range(N):
                if self.tile(x, y) != "~" and f"{x},{y}" not in st["b"]:
                    if self.by_road(x, y):
                        spots.append(f"{x},{y}")
                    if self.near_water(x, y):
                        wet.append(f"{x},{y}")
        lines.append("Empty spots next to a road: " + (" ".join(spots[:16]) or "none (build roads first)"))
        lines.append("Empty spots next to water (for a sea wall or fishery): " + " ".join(wet[:12]))
        lines.append("You can build (cost/upkeep a day, what it gives): " + "; ".join(
            f"{k} {d['c']}/{d['u']}" + "".join(f" {lab}{d[f]}" for f, lab in
                                               (("homes", "homes+"), ("jobs", "jobs+"), ("food", "food+"), ("water", "water+"),
                                                ("power", "power+"), ("care", "care+"), ("shelter", "shelter+"),
                                                ("store_f", "store+"), ("poll", "pollution+")) if d.get(f))
            for k, d in B.items()) + ". Sea walls protect land within 2 tiles from floods. Fire stations stop fires within 3 tiles. "
            "Each resident eats 1 food and drinks 1.5 water a day; buildings and people need power.")
        return "\n".join(lines)

    def send(self, a, phase, text):
        tid = secrets.token_hex(8)
        turn = {"id": tid, "kind": "chat", "agent_id": a["id"], "agent_name": a["name"], "you": a["name"],
                "topic": f"City council, day {self.st['day']}", "project": None, "city": True,
                "guidelines": GUIDE.format(name=a["name"]),
                "participants": [{"name": x["name"], "about": "", "online": True} for x in self.session["members"]],
                "messages": [{"kind": "system", "author": "city", "text": text}], "max_tokens": 600}
        with self.lock:
            self.pending[tid] = (a["id"], a["name"], phase)
            self.session["waiting"].add(a["name"])
        self.room.inbox.setdefault(a["id"], queue.Queue()).put(turn)

    def take(self, agent, turn_id, text, passed):
        """An answer from a connector: True if it was a city turn."""
        with self.lock:
            p = self.pending.pop(turn_id, None)
            if not p:
                return False
            if p[0] != agent["id"]:
                self.pending[turn_id] = p
                return False
            ses = self.session
            if not ses or p[2] != ses["phase"]:
                return True
            ses["waiting"].discard(p[1])
            ses["replies"][p[1]] = "" if passed else (text or "")
        self.dirty = True
        return True

    def start_session(self, members):
        st = self.st
        self.session = {"phase": "propose", "members": members, "waiting": set(), "replies": {}, "proposals": [],
                        "started": time.time(), "deadline": time.time() + (40 if FAST else TURN_TIMEOUT)}
        self.event("session", f"Council session for day {st['day']}: " + ", ".join(a["name"] for a in members))
        brief = self.briefing()
        for a in members:
            self.send(a, "propose", brief + "\n\n" + PROPOSE)

    def collect_proposals(self):
        st, ses = self.st, self.session
        n = 0
        for a in ses["members"]:
            text = ses["replies"].get(a["name"], "")
            if text:
                self.q("INSERT INTO said(run,day,ts,who,phase,text) VALUES(?,?,?,?,?,?)",
                       (st["run"], st["day"], time.time(), a["name"], "propose", text[:3000]))
            got = 0
            for verb, args, reason in ACTION_RE.findall(text):
                act = self.parse_action(verb, args)
                if not act or got >= 3:
                    continue
                got += 1
                n += 1
                desc = self.describe(act)
                cost = self.cost(act) if (act["verb"] != "build" or 0 <= act["x"] < N and 0 <= act["y"] < N) else 0
                pid = self.q("INSERT INTO proposals(run,day,n,author,action,reason,cost) VALUES(?,?,?,?,?,?,?)",
                             (st["run"], st["day"], n, a["name"], desc, reason.strip()[:300], cost)).lastrowid
                ses["proposals"].append({"id": pid, "n": n, "author": a["name"], "act": act, "desc": desc,
                                         "reason": reason.strip()[:300], "cost": cost, "yes": [], "no": []})
            chat = re.sub(r"\s+", " ", ACTION_RE.sub("", text)).strip()
            if chat:
                self.event("said", f"{a['name']}: {chat[:240]}", who=a["name"])
        if ses["proposals"]:
            self.event("proposals", f"{len(ses['proposals'])} proposal(s): " +
                       "; ".join(f"{p['n']}. {p['desc']} ({p['author']})" for p in ses["proposals"])[:600])

    def start_vote(self):
        st, ses = self.st, self.session
        ses.update(phase="vote", replies={}, waiting=set(), deadline=time.time() + (40 if FAST else TURN_TIMEOUT))
        listing = "\n".join(f"{p['n']}. {p['desc']} (proposed by {p['author']}; costs {p['cost']})" +
                            (f" :: {p['reason']}" if p["reason"] else "") for p in ses["proposals"])
        s = st["last"]
        alerts = " ".join(f"{a['name']} in {a['in']} day(s)." for a in self.view_forecast(False))
        for a in ses["members"]:
            self.send(a, "vote", f"You are {a['name']} on the council of {st['name']}, day {st['day']}. Money {s['money']}, "
                                 f"people {s['pop']}, happiness {s['happy']}, food {s['food']}, water {s['water']}."
                                 + (f" Forecast: {alerts}" if alerts else "") +
                                 f"\n\nToday's proposals:\n{listing}\n\n{VOTE}")

    def count_votes(self):
        st, ses = self.st, self.session
        by_n = {p["n"]: p for p in ses["proposals"]}
        for a in ses["members"]:
            text = ses["replies"].get(a["name"], "")
            if text:
                self.q("INSERT INTO said(run,day,ts,who,phase,text) VALUES(?,?,?,?,?,?)",
                       (st["run"], st["day"], time.time(), a["name"], "vote", text[:2000]))
            for rx, key in ((YES_RE, "yes"), (NO_RE, "no")):
                for grp in rx.findall(text):
                    for num in re.findall(r"\d+", grp):
                        p = by_n.get(int(num))
                        if p and a["name"] not in p["yes"] + p["no"]:
                            p[key].append(a["name"])

    def resolve(self):
        """Carry out what passed (best-supported first, while money lasts), then play out the day."""
        ses = self.session
        alone = len(ses["members"]) == 1
        for p in ses["proposals"]:
            p["passed"] = alone or len(p["yes"]) > len(p["no"])
        for p in sorted(ses["proposals"], key=lambda p: (-(len(p["yes"]) - len(p["no"])), p["n"])):
            ok, res = self.do(p["act"]) if p["passed"] else (False, "voted down")
            self.q("UPDATE proposals SET yes=?, no=?, passed=?, done=?, result=? WHERE id=?",
                   (json.dumps(p["yes"]), json.dumps(p["no"]), int(p["passed"]), int(ok), res, p["id"]))
            self.event("decision", f"{'Done' if ok else 'Passed but not done' if p['passed'] else 'Rejected'}: "
                                   f"{p['desc']} ({p['author']}; {len(p['yes'])} for, {len(p['no'])} against)"
                                   + (f" - {res}" if p["passed"] else ""), author=p["author"], done=ok, passed=p["passed"])
        self.session = None
        self.end_day()

    def end_day(self):
        st = self.st
        notes = []
        _, more = self.simulate_day()
        notes += more
        self.ongoing(notes)
        st["day"] += 1
        for sc in self.q("SELECT * FROM scenarios WHERE run=? AND status='scheduled' AND due<=? ORDER BY due, id",
                         (st["run"], st["day"])).fetchall():
            self.hit(dict(sc), notes)
        s = self.stats()
        self.q("INSERT OR REPLACE INTO days(run,day,ts,stats) VALUES(?,?,?,?)",
               (st["run"], st["day"], time.time(), json.dumps(dict(s, active=[a["type"] for a in st["active"]],
                                                                    policies=st["policies"]))))
        st["history"] = (st["history"] + [{"day": st["day"], "pop": s["pop"], "money": s["money"], "happy": s["happy"]}])[-90:]
        plain = [n for n in notes if not n.startswith(tuple(NICE.values()))]
        self.event("day", f"Day {st['day']} begins: {s['pop']} people, happiness {s['happy']}, money {s['money']}." +
                   (" " + "; ".join(plain) + "." if plain else ""))
        self.save()

    def tick(self):
        now = time.time()
        project = self.room.s.active_project()
        reason = ("the AIs are working on a project" if project else
                  "the room is paused" if self.room.s.get("paused") == "1" else "")
        if reason and self.running:
            self.running, self.paused_reason = False, reason
            if self.session:
                with self.lock:
                    self.session, self.pending = None, {}
                self.event("session", "The council session was called off: " + reason)
            self.dirty = True
        elif not reason and not self.running:
            self.running, self.paused_reason = True, ""
            self.dirty = True
        if self.running:
            ses = self.session
            if not ses:
                if now >= float(self.kv("next_session") or 0):
                    mem = self.members()
                    if mem:
                        every = self.setting("session_every", SESSION_EVERY) * 60
                        self.set_kv("next_session", str(now + (3 if FAST else every)))
                        self.start_session(mem)
                    else:
                        self.set_kv("next_session", str(now + 60))
            elif not ses["waiting"] or now > ses["deadline"]:
                with self.lock:
                    late = sorted(ses["waiting"])
                    for tid in [t for t, p in self.pending.items() if p[2] == ses["phase"]]:
                        self.pending.pop(tid, None)
                if late:
                    self.event("session", "No answer in time from " + ", ".join(late))
                if ses["phase"] == "propose":
                    self.collect_proposals()
                    if ses["proposals"] and len(ses["members"]) > 1:
                        self.start_vote()
                    else:
                        self.resolve()
                else:
                    self.count_votes()
                    self.resolve()
                self.dirty = True
        if self.dirty or now - self.last_save > 30:
            self.save()
            self.room.broadcast("city", self.view())
            self.dirty = False

    # ---- what viewers see
    def view_forecast(self, admin):
        return [{"id": x["id"], "type": x["type"], "name": x["name"], "severity": x["severity"],
                 "in": x["due"] - self.st["day"], "warn": bool(x["warn"])}
                for x in self.scenarios(include_hidden=admin) if x["status"] == "scheduled"]

    def view(self, admin=False):
        st = self.st
        s = st["last"] or self.stats()
        ses = self.session
        feed = [dict(r, data=json.loads(r["data"])) for r in self.q(
            "SELECT day, ts, kind, text, data FROM events WHERE run=? ORDER BY id DESC LIMIT 30", (st["run"],)).fetchall()]
        decisions = [dict(r, yes=json.loads(r["yes"]), no=json.loads(r["no"])) for r in self.q(
            "SELECT day, n, author, action, reason, cost, yes, no, passed, done, result FROM proposals "
            "WHERE run=? ORDER BY id DESC LIMIT 24", (st["run"],)).fetchall()]
        now = self.now_local()
        gone = self.room.s.hidden_agents()
        return {
            "running": self.running, "paused": self.paused_reason, "council": True,
            "run": {"id": st["run"], "name": st["name"], "seed": st["seed"]}, "day": st["day"], "size": N,
            "terrain": st["terrain"],
            "buildings": [{"t": v["t"], "x": int(k.split(",")[0]), "y": int(k.split(",")[1]), "s": v["s"]}
                          for k, v in st["b"].items()],
            "marks": st["marks"], "stats": s, "tax": st["tax"], "policies": st["policies"],
            "active": [{"type": a["type"], "name": NICE[a["type"]], "severity": a["severity"], "left": a["left"]}
                       for a in st["active"]],
            "forecast": self.view_forecast(admin),
            "session": ses and {"phase": ses["phase"], "members": [a["name"] for a in ses["members"]],
                                "waiting": sorted(ses["waiting"])},
            "members": [{"name": a["name"], "color": a["color"]} for a in self.room.s.agents()
                        if a["ready"] and not a["projects_only"] and a["name"].lower() not in gone],
            "history": st["history"], "feed": feed, "decisions": decisions,
            "sun": list(self.sun()), "minute": now.hour * 60 + now.minute,
            "catalogue": {k: {"name": d["n"], "cost": d["c"], "ch": d["ch"]} for k, d in B.items()},
        }

    def admin_view(self):
        v = self.view(admin=True)
        v["scenarios"] = self.scenarios()
        v["types"] = {k: {"name": NICE[k], "about": SCENARIOS[k], "duration": [DURATION[k](s) for s in range(1, 6)]}
                      for k in SCENARIOS}
        v["runs"] = self.reports()
        v["session_every"] = self.setting("session_every", SESSION_EVERY)
        v["next_session"] = float(self.kv("next_session") or 0)
        return v

    # ---- reports: one per run
    def reports(self):
        rows = self.q("SELECT id, name, seed, started, ended FROM runs ORDER BY id DESC LIMIT 40").fetchall()
        out = []
        for r in rows:
            last = self.q("SELECT MAX(day) d FROM days WHERE run=?", (r["id"],)).fetchone()["d"] or 1
            out.append({"id": r["id"], "title": f"Run {r['id']}: {r['name']} ({last} days)", "name": r["name"],
                        "seed": r["seed"], "start": r["started"], "end": r["ended"] or time.time(), "days": last,
                        "current": r["id"] == self.st["run"]})
        return out

    def report(self, rid):
        r = self.q("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
        if not r:
            return None
        return {"id": rid, "body": self.build_report(dict(r)), "end": r["ended"] or time.time(),
                "title": f"Run {rid}: {r['name']}"}

    def write_report(self, start=None, end=None, auto=False):
        return self.st["run"]

    def build_report(self, run):
        rid = run["id"]
        days = [dict(json.loads(r["stats"]), day=r["day"]) for r in
                self.q("SELECT day, stats FROM days WHERE run=? ORDER BY day", (rid,)).fetchall()]
        props = [dict(r) for r in self.q("SELECT * FROM proposals WHERE run=? ORDER BY id", (rid,)).fetchall()]
        scs = [x for x in self.scenarios(rid) if x["warn"] or x["status"] not in ("scheduled", "cancelled")]   # surprises stay secret
        fmt = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "still running"
        out = [f"# City run {rid}: {run['name']}", "",
               f"Seed {run['seed']}, starting money {run['money']}. Started {fmt(run['started'])}, ended {fmt(run['ended'])}. "
               f"{len(days)} city days recorded.", ""]
        if days:
            a, z = days[0], days[-1]
            peak = max(days, key=lambda d: d["pop"])
            out += ["## Summary", "", "| | Start | End | Best |", "|---|---|---|---|",
                    f"| People | {a['pop']} | {z['pop']} | {peak['pop']} (day {peak['day']}) |",
                    f"| Happiness | {a['happy']} | {z['happy']} | {max(d['happy'] for d in days)} |",
                    f"| Money | {a['money']} | {z['money']} | {max(d['money'] for d in days)} |",
                    f"| Buildings | {sum(a['buildings'].values())} | {sum(z['buildings'].values())} | |",
                    f"| Died (total) | | {z.get('deaths', 0)} | |", ""]
        out += ["## Scenarios", ""]
        if not scs:
            out += ["None scheduled.", ""]
        else:
            out += ["| Scenario | Severity | Due | Warned | Status | Preparedness | Died | Destroyed | Damaged | Recovery |",
                    "|---|---|---|---|---|---|---|---|---|---|"]
            for x in scs:
                r = x["result"]
                out.append(f"| {x['name']} | {x['severity']} | day {x['due']} | {'yes' if x['warn'] else 'no'} | {x['status']} | "
                           f"{(r.get('before') or {}).get('prepared', '-')} | {r.get('total_deaths', r.get('deaths', '-'))} | "
                           f"{r.get('destroyed', '-')} | {r.get('damaged', '-')} | "
                           f"{str(r['recovery_days']) + ' days' if 'recovery_days' in r else '-'} |")
            out.append("")
        if props:
            by, agree, kinds = {}, {}, {}
            for p in props:
                b = by.setdefault(p["author"], {"n": 0, "passed": 0, "done": 0})
                b["n"] += 1; b["passed"] += p["passed"]; b["done"] += p["done"]
                for who in json.loads(p["yes"]):
                    g = agree.setdefault(who, [0, 0]); g[1] += 1; g[0] += bool(p["passed"])
                for who in json.loads(p["no"]):
                    g = agree.setdefault(who, [0, 0]); g[1] += 1; g[0] += not p["passed"]
                if p["passed"]:
                    w = p["action"].split()
                    k = " ".join(w[:2]) if w[0] in ("build", "policy", "stockpile") else w[0]
                    kinds[k] = kinds.get(k, 0) + 1
            out += ["## The council", "", "| Councillor | Proposed | Passed | Carried out | Votes on the winning side |",
                    "|---|---|---|---|---|"]
            for who, b in sorted(by.items()):
                g = agree.get(who, [0, 0])
                out.append(f"| {who} | {b['n']} | {b['passed']} | {b['done']} | {g[0]} of {g[1]} |")
            out += ["", "What passed most: " + (", ".join(f"{k} ({n})" for k, n in
                                                         sorted(kinds.items(), key=lambda kv: -kv[1])[:12]) or "nothing"), "",
                    "## Decisions, day by day", ""]
            for p in props:
                out.append(f"- Day {p['day']}, {p['author']}: `{p['action']}` (cost {p['cost']}) "
                           f"{'passed' if p['passed'] else 'rejected'} {len(json.loads(p['yes']))}-{len(json.loads(p['no']))}"
                           + (f", {p['result']}" if p["result"] and p["passed"] else "") + (f". Why: {p['reason']}" if p["reason"] else ""))
            out.append("")
        if days:
            out += ["## Day by day", "", "| Day | People | Happy | Money | Food | Water | Power | Sick | Died | Disasters |",
                    "|---|---|---|---|---|---|---|---|---|---|"]
            for d in days:
                out.append(f"| {d['day']} | {d['pop']} | {d['happy']} | {d['money']} | {d['food']} | {d['water']} | "
                           f"{d['power']}/{d['power_need']} | {d['sick']} | {d.get('deaths', 0)} | {', '.join(d.get('active', [])) or '-'} |")
            out.append("")
        out += ["## Method", "",
                "The city is a deterministic simulation: the seed fixes the land and every disaster's random rolls. "
                "The councillors are the room's AIs, each answering through its own model on its owner's computer. "
                "At each session every free councillor gets the same briefing (numbers, map, forecasts) and proposes up "
                "to three actions; then every councillor votes on every proposal. A proposal passes with more votes for "
                "than against (a lone councillor's proposals pass). Passed actions run best-supported first while money "
                "lasts, then the city plays out one day. Scenario timers count sessions, not real time. Preparedness is "
                "a 0-100 score taken when a disaster hits (shelter places, stores, care, money, drills, and protection "
                "specific to that disaster). Recovery is the first day people are back to 90% and happiness within 10 "
                "points of before the disaster.", ""]
        return "\n".join(out)

    def run_brief(self, rid, cap=8000):
        """A run's report, short enough to hand to a project as source material."""
        r = self.report(rid)
        if not r:
            return ""
        body = r["body"]
        return body if len(body) <= cap else body[:cap] + "\n\n(report trimmed; the full version is on the city page)"

    def csv(self, rid, what):
        out = io.StringIO()
        w = csv.writer(out)
        if what == "days":
            keys = ["day", "pop", "happy", "money", "income", "costs", "tax", "homes", "jobs", "employed", "food",
                    "food_make", "food_need", "water", "water_make", "water_need", "power", "power_need", "care",
                    "sick", "pollution", "shelter", "reserve", "deaths"]
            w.writerow(keys + ["policies", "disasters", "buildings"])
            for r in self.q("SELECT day, stats FROM days WHERE run=? ORDER BY day", (rid,)).fetchall():
                d = json.loads(r["stats"])
                d["day"] = r["day"]
                w.writerow([d.get(k, "") for k in keys] +
                           [" ".join(d.get("policies", [])), " ".join(d.get("active", [])),
                            " ".join(f"{k}:{v}" for k, v in sorted(d.get("buildings", {}).items()))])
        else:
            w.writerow(["day", "n", "author", "action", "reason", "cost", "yes", "no", "passed", "carried_out", "result"])
            for p in self.q("SELECT * FROM proposals WHERE run=? ORDER BY id", (rid,)).fetchall():
                w.writerow([p["day"], p["n"], p["author"], p["action"], p["reason"], p["cost"],
                            " ".join(json.loads(p["yes"])), " ".join(json.loads(p["no"])), p["passed"], p["done"], p["result"]])
        return out.getvalue()

    def admin(self, b, who):
        """Dashboard actions: new run, schedule or cancel a scenario, hold a session now, session timing."""
        op = b.get("op")
        if op == "new_run":
            return {"ok": True, "run": self.new_run(str(b.get("name") or "City"), b.get("seed"), b.get("money") or 1000, who)}
        if op == "schedule":
            r = self.schedule(str(b.get("type")), b.get("severity", 3), b.get("in_days", 3), bool(b.get("warn")),
                              b.get("duration"), who)
            if b.get("warn"):
                self.event("forecast", f"Forecast: {NICE[b['type']]} (severity {r['severity']}) expected in "
                                       f"{r['due'] - self.st['day']} day(s).")
            return dict(r, ok=True)
        if op == "cancel":
            self.q("UPDATE scenarios SET status='cancelled' WHERE id=? AND status='scheduled'", (int(b.get("id")),))
            self.dirty = True
            return {"ok": True}
        if op == "session_now":
            self.set_kv("next_session", "0")
            return {"ok": True}
        if op == "settings":
            n = max(1, min(720, int(b.get("session_every") or SESSION_EVERY)))
            self.set_kv("set_session_every", str(n))
            self.dirty = True
            return {"ok": True, "session_every": n}
        raise ValueError("unknown city action")

    def run(self):
        while True:
            time.sleep(1)
            try:
                self.tick()
            except Exception as e:
                import sys, traceback
                print("city error:", repr(e), file=sys.stderr, flush=True)
                traceback.print_exc()
