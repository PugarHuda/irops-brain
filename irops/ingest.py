"""Build the IROPS graph from public US DOT on-time data.

Real (public): every Southwest flight, its tail number, scheduled times and what
actually happened (delays, cancellations, delay causes) for 21-23 Dec 2022.
Synthetic (seeded, deterministic): cockpit crews, reserve crews and connecting
passenger bookings, generated on top of the real schedule. No private data.

    uv run python -m irops.ingest            # loads graph "irops_WN"
"""
import csv, io, random, sys, zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from irops.db import graph

BTS_ZIP = "data/raw/bts_2022_12.zip"
AIRPORTS = "data/raw/airports.dat"
DAYS = ("2022-12-21", "2022-12-22", "2022-12-23")
EPOCH = datetime(2022, 12, 21, tzinfo=timezone.utc)  # all times = minutes since this instant (UTC)
MIN_TURN = 35        # minutes an aircraft needs on the ground
MIN_CREW_CONN = 30   # minutes a crew needs to change aircraft
MIN_PAX_CONN = 35    # minimum connection time for passengers
CONN_PENALTY = 45    # rebooking cost of one extra connection, in minutes
SEATS = 175         # 737-800; ponytail: one cabin size, per-tail seat maps if it matters
# ponytail: plausible synthetic crew bases, not Southwest's real roster.
BASES = ["ATL", "BWI", "DAL", "DEN", "HOU", "LAS", "MCO", "MDW", "OAK", "PHX", "BNA", "LAX"]
SEED = 22


def minutes(dt):
    return int((dt - EPOCH).total_seconds() // 60)


def fnum(s):
    return float(s) if s not in ("", None) else 0.0


def load_airports(codes):
    na = chr(92) + "N"
    out = {}
    for r in csv.reader(open(AIRPORTS, encoding="utf-8")):
        if r[4] in codes and r[11] != na:
            out[r[4]] = {"code": r[4], "name": r[1], "city": r[2],
                         "lat": float(r[6]), "lon": float(r[7]), "tz": r[11]}
    return out


def read_flights(carrier="WN"):
    z = zipfile.ZipFile(BTS_ZIP)
    f = io.TextIOWrapper(z.open(next(n for n in z.namelist() if n.endswith(".csv"))), "latin-1")
    rows = [r for r in csv.DictReader(f) if r["Reporting_Airline"] == carrier and r["FlightDate"] in DAYS]
    airports = load_airports({r["Origin"] for r in rows} | {r["Dest"] for r in rows})
    flights, seen = [], set()
    for r in rows:
        day = datetime.strptime(r["FlightDate"], "%Y-%m-%d")
        def local(hhmm, code):
            hhmm = hhmm.zfill(4)
            h, m = (0, 0) if hhmm == "2400" else (int(hhmm[:2]), int(hhmm[2:]))
            dt = day.replace(hour=h, minute=m, tzinfo=ZoneInfo(airports[code]["tz"]))
            return dt + timedelta(days=1) if hhmm == "2400" else dt
        dep = minutes(local(r["CRSDepTime"], r["Origin"]))
        arr = minutes(local(r["CRSArrTime"], r["Dest"]))
        while arr <= dep:
            arr += 1440
        fid = f'{r["FlightDate"]}-WN{r["Flight_Number_Reporting_Airline"]}-{r["Origin"]}'
        if fid in seen:
            fid += "-" + r["Tail_Number"]
        seen.add(fid)
        flights.append({
            "id": fid, "fno": "WN" + r["Flight_Number_Reporting_Airline"], "date": r["FlightDate"],
            "origin": r["Origin"], "dest": r["Dest"], "tail": r["Tail_Number"],
            "dep": dep, "arr": arr, "block": arr - dep, "seats": SEATS,
            # ground truth, never shown to the agents; used by eval/backtest.py
            "act_dep_delay": fnum(r["DepDelay"]), "act_arr_delay": fnum(r["ArrDelay"]),
            "act_cancelled": r["Cancelled"] == "1.00", "act_cancel_code": r["CancellationCode"],
            "act_late_aircraft": fnum(r["LateAircraftDelay"]), "act_weather": fnum(r["WeatherDelay"]),
            "act_nas": fnum(r["NASDelay"]), "act_carrier": fnum(r["CarrierDelay"]),
        })
    return flights, airports


def aircraft_legs(flights):
    by_tail = defaultdict(list)
    for f in flights:
        if f["tail"]:
            by_tail[f["tail"]].append(f)
    legs = []
    for tail, fs in by_tail.items():
        fs.sort(key=lambda f: f["dep"])
        for a, b in zip(fs, fs[1:]):
            gap = b["dep"] - a["arr"]
            if a["dest"] == b["origin"] and 0 <= gap < 16 * 60:
                legs.append({"a": a["id"], "b": b["id"], "turn": gap, "slack": max(0, gap - MIN_TURN), "tail": tail})
    return legs


def fdp_limit(start_local_hour, segments):
    """14 CFR 117.13 Table B (unaugmented), simplified to whole hours."""
    table = [(0, 9), (4, 10), (5, 12), (6, 13), (7, 14), (12, 13), (13, 12), (17, 12), (22, 11), (23, 10)]
    limit = next(h for start, h in reversed(table) if start_local_hour >= start)
    return int((limit - 0.5 * max(0, segments - 4)) * 60)


def build_crews(flights, airports, rng):
    """Greedy, legal-by-construction cockpit pairings over the real schedule.

    ponytail: greedy cover, not an optimiser; crews stay with their aircraft
    ~65% of the time, which is what makes delays jump between tails.
    """
    tz = {c: ZoneInfo(a["tz"]) for c, a in airports.items()}
    crews, assign, by_loc = [], [], defaultdict(list)
    last_crew_of_tail = {}
    for f in sorted(flights, key=lambda f: f["dep"]):
        def legal(c):
            if c["loc"] != f["origin"] or c["free_at"] > f["dep"]:
                return False
            gap = f["dep"] - c["last_arr"]
            if c["duty_start"] is None or gap >= 10 * 60:
                return True  # new duty period after legal rest (117.25)
            if gap > 4 * 60:
                return False  # too long to be the same duty, too short to be rest
            return f["arr"] + 15 - c["duty_start"] <= fdp_limit(c["start_hour"], c["legs"] + 1)
        pick = None
        prev = last_crew_of_tail.get(f["tail"])
        if prev and legal(prev) and rng.random() < 0.65:
            pick = prev
        else:
            cands = [c for c in by_loc[f["origin"]] if legal(c)]
            if cands:
                pick = min(cands, key=lambda c: c["free_at"])
        if pick is None:
            pick = {"id": f"C{len(crews) + 1:04d}", "base": f["origin"] if f["origin"] in BASES else rng.choice(BASES),
                    "loc": f["origin"], "free_at": -10**6, "duty_start": None, "last_arr": -10**6,
                    "legs": 0, "start_hour": 0, "reserve": False}
            crews.append(pick)
            by_loc[f["origin"]].append(pick)
        if pick["duty_start"] is None or f["dep"] - pick["last_arr"] > 4 * 60:
            pick["duty_start"] = f["dep"] - 45  # report 45 min before first departure
            pick["start_hour"] = (EPOCH + timedelta(minutes=pick["duty_start"])).astimezone(tz[f["origin"]]).hour
            pick["legs"] = 0
        by_loc[pick["loc"]].remove(pick)
        pick["legs"] += 1
        pick["loc"], pick["last_arr"] = f["dest"], f["arr"]
        pick["free_at"] = f["arr"] + MIN_CREW_CONN
        by_loc[f["dest"]].append(pick)
        assign.append({"crew": pick["id"], "flight": f["id"], "duty_start": pick["duty_start"],
                       "fdp_limit": fdp_limit(pick["start_hour"], pick["legs"])})
        last_crew_of_tail[f["tail"]] = pick
    for c in crews:  # drop working state, keep identity
        c.pop("free_at"), c.pop("last_arr"), c.pop("legs"), c.pop("start_hour"), c.pop("duty_start"), c.pop("loc")
    # crew connections: consecutive flights of the same crew
    seq = defaultdict(list)
    fmap = {f["id"]: f for f in flights}
    for a in assign:
        seq[a["crew"]].append(a["flight"])
    crew_next = []
    for crew, fids in seq.items():
        for a, b in zip(fids, fids[1:]):
            gap = fmap[b]["dep"] - fmap[a]["arr"]
            if gap <= 4 * 60:  # same duty period; across rest the delay is absorbed by the rest buffer
                crew_next.append({"a": a, "b": b, "crew": crew, "slack": max(0, gap - MIN_CREW_CONN)})
    # reserves: ~6% of the line crews per base per day, on call 05:00-17:00 local
    reserves = []
    for base in BASES:
        n = max(2, sum(c["base"] == base for c in crews) * 6 // 100)
        for d, day in enumerate(DAYS):
            for i in range(n):
                start = datetime.strptime(day, "%Y-%m-%d").replace(hour=5, tzinfo=tz[base])
                reserves.append({"id": f"R-{base}-{d}{i:02d}", "base": base, "reserve": True,
                                 "on_call_from": minutes(start), "on_call_to": minutes(start) + 12 * 60})
    return crews + reserves, assign, crew_next


def build_bookings(flights, rng):
    """Connecting itineraries through the real network (local pax kept as a count)."""
    dep_by_ap = defaultdict(list)
    for f in flights:
        dep_by_ap[f["origin"]].append(f)
    for fs in dep_by_ap.values():
        fs.sort(key=lambda f: f["dep"])
    bookings, segs = [], []
    load = {f["id"]: 0 for f in flights}
    for a in flights:
        outs = [d for d in dep_by_ap[a["dest"]]
                if MIN_PAX_CONN + 10 <= d["dep"] - a["arr"] <= 180 and d["dest"] != a["origin"]]
        for d in rng.sample(outs, min(len(outs), rng.randint(1, 4))):
            pax = rng.randint(2, 12)
            if load[a["id"]] + pax > 70 or load[d["id"]] + pax > 70:
                continue
            bid = f"B{len(bookings) + 1:05d}"
            bookings.append({"id": bid, "pax": pax, "origin": a["origin"], "dest": d["dest"], "planned_arr": d["arr"]})
            segs += [{"b": bid, "f": a["id"], "seq": 1}, {"b": bid, "f": d["id"], "seq": 2}]
            load[a["id"]] += pax
            load[d["id"]] += pax
    for f in flights:  # holiday load factor ~85%
        f["pax_local"] = max(0, int(f["seats"] * rng.uniform(0.75, 0.92)) - load[f["id"]])
        f["pax"] = f["pax_local"] + load[f["id"]]
    return bookings, segs


RULES = [
    {"id": "14CFR117.13", "title": "Flight duty period: unaugmented operations",
     "text": "Maximum FDP depends on scheduled report time (acclimated) and number of segments (Table B).",
     "url": "https://www.ecfr.gov/current/title-14/chapter-I/subchapter-G/part-117/section-117.13"},
    {"id": "14CFR117.25", "title": "Rest period",
     "text": "Before beginning any reserve or flight duty period a flightcrew member must be given at least 10 consecutive hours of rest immediately before the FDP, with at least 8 hours of uninterrupted sleep opportunity.",
     "url": "https://www.ecfr.gov/current/title-14/chapter-I/subchapter-G/part-117/section-117.25"},
    {"id": "14CFR117.21", "title": "Reserve status",
     "text": "Short-call reserve availability period may not exceed 14 hours; FDP plus reserve availability limits apply.",
     "url": "https://www.ecfr.gov/current/title-14/chapter-I/subchapter-G/part-117/section-117.21"},
    {"id": "DOT-REFUND-2024", "title": "Automatic refunds for cancelled flights",
     "text": "Passengers are entitled to a refund if their flight is cancelled or significantly changed and they do not accept rebooking.",
     "url": "https://www.transportation.gov/briefing-room/biden-harris-administration-announces-final-rule-requiring-automatic-refunds-airline"},
]


def batched(query, rows, g, size=2000):
    for i in range(0, len(rows), size):
        g.query(query, {"rows": rows[i:i + size]}, timeout=120000)


def load(name="irops_WN"):
    rng = random.Random(SEED)
    flights, airports = read_flights()
    legs = aircraft_legs(flights)
    crews, assign, crew_next = build_crews(flights, airports, rng)
    bookings, segs = build_bookings(flights, rng)
    fmap = {f["id"]: f for f in flights}
    # passenger-feasible connections at the same airport, for rebooking (time-expanded network)
    by_ap = defaultdict(list)
    for f in flights:
        by_ap[f["origin"]].append(f)
    conns = []
    for a in flights:
        for d in by_ap[a["dest"]]:
            gap = d["dep"] - a["arr"]
            if MIN_PAX_CONN <= gap <= 6 * 60 and d["dest"] != a["origin"]:
                # weight = extra arrival time + 45 min per connection, so fewer stops win ties
                conns.append({"a": a["id"], "b": d["id"], "w": d["arr"] - a["arr"] + CONN_PENALTY})
    routes = defaultdict(list)
    for f in flights:
        routes[(f["origin"], f["dest"])].append(f["block"])
    routes = [{"o": o, "d": d, "block": sum(v) // len(v), "daily": len(v) // len(DAYS) or 1} for (o, d), v in routes.items()]

    g = graph(name)
    try:
        g.delete()
    except Exception:
        pass
    g = graph(name)
    for label, prop in [("Airport", "code"), ("Flight", "id"), ("Aircraft", "tail"), ("Crew", "id"),
                        ("Booking", "id"), ("Rule", "id"), ("Agent", "name")]:
        g.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
    g.query("CREATE INDEX FOR (n:Flight) ON (n.dep)")
    batched("UNWIND $rows AS r MERGE (a:Airport {code: r.code}) SET a += r", list(airports.values()), g)
    # :Active = not cancelled. Graph algorithms filter by label, so cancelling a flight removes it.
    batched("UNWIND $rows AS r MERGE (f:Flight {id: r.id}) SET f += r, f:Active, f.status = 'scheduled', f.delay = 0", flights, g)
    batched("""UNWIND $rows AS r MATCH (f:Flight {id: r.id}), (o:Airport {code: r.origin}), (d:Airport {code: r.dest})
               MERGE (f)-[:DEPARTS_FROM]->(o) MERGE (f)-[:ARRIVES_AT {w: 0}]->(d)""", flights, g)
    batched("""UNWIND $rows AS r MATCH (f:Flight {id: r.id}) WHERE r.tail <> ''
               MERGE (t:Aircraft {tail: r.tail}) MERGE (t)-[:OPERATES]->(f)""", flights, g)
    batched("UNWIND $rows AS r MATCH (a:Flight {id: r.a}), (b:Flight {id: r.b}) MERGE (a)-[e:NEXT_LEG]->(b) SET e.turn = r.turn, e.slack = r.slack, e.tail = r.tail", legs, g)
    batched("UNWIND $rows AS r MERGE (c:Crew {id: r.id}) SET c += r", crews, g)
    batched("""UNWIND $rows AS r MATCH (c:Crew {id: r.id}), (b:Airport {code: r.base}) MERGE (c)-[:BASED_AT]->(b)""", crews, g)
    batched("""UNWIND $rows AS r MATCH (c:Crew {id: r.crew}), (f:Flight {id: r.flight})
               MERGE (c)-[e:ASSIGNED]->(f) SET e.duty_start = r.duty_start, e.fdp_limit = r.fdp_limit""", assign, g)
    batched("UNWIND $rows AS r MATCH (a:Flight {id: r.a}), (b:Flight {id: r.b}) MERGE (a)-[e:CREW_NEXT {crew: r.crew}]->(b) SET e.slack = r.slack", crew_next, g)
    batched("UNWIND $rows AS r MERGE (b:Booking {id: r.id}) SET b += r, b.status = 'confirmed'", bookings, g)
    batched("UNWIND $rows AS r MATCH (b:Booking {id: r.b}), (f:Flight {id: r.f}) MERGE (b)-[:SEGMENT {seq: r.seq}]->(f)", segs, g, 4000)
    # aggregate passenger connections from bookings, in the graph
    g.query("""MATCH (b:Booking)-[:SEGMENT {seq: 1}]->(a:Flight), (b)-[:SEGMENT {seq: 2}]->(d:Flight)
               WITH a, d, sum(b.pax) AS pax
               MERGE (a)-[e:PAX_CONNECT]->(d) SET e.pax = pax, e.slack = d.dep - a.arr - $mct""", {"mct": MIN_PAX_CONN}, timeout=120000)
    batched("UNWIND $rows AS r MATCH (a:Flight {id: r.a}), (b:Flight {id: r.b}) CREATE (a)-[:CONN {w: r.w}]->(b)", conns, g, 5000)
    batched("""UNWIND $rows AS r MATCH (o:Airport {code: r.o}), (d:Airport {code: r.d})
               MERGE (o)-[e:ROUTE]->(d) SET e.block = r.block, e.daily = r.daily""", routes, g)
    batched("UNWIND $rows AS r MERGE (x:Rule {id: r.id}) SET x += r", RULES, g)
    for name_ in ("ops", "crew", "pax"):
        g.query("MERGE (:Agent {name: $n})", {"n": name_})
    counts = g.query("MATCH (n) RETURN labels(n)[0] AS l, count(*) ORDER BY l", timeout=120000).result_set
    rels = g.query("MATCH ()-[r]->() RETURN type(r), count(*) ORDER BY type(r)", timeout=120000).result_set
    print("nodes", counts, "\nrels", rels)
    return counts, rels


if __name__ == "__main__":
    load(*(sys.argv[1:2]))
