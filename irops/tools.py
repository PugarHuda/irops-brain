"""The fixed tool catalogue. Humans wrote every Cypher query; agents only pick a
tool and fill in arguments, and every argument is resolved against the graph
before anything runs.

Each tool returns a dict:
  outcome  answered | from_memory | not_in_graph | unknown_entity | rejected
  rows     plain data the answer must be built from
  paths    the graph paths behind the rows (the explanation, not a reconstruction)
  cypher   the queries that ran
"""
import json
import re
from datetime import timedelta
from zoneinfo import ZoneInfo

from falkordb.edge import Edge
from falkordb.node import Node
from falkordb.path import Path

from irops.db import graph
from irops.ingest import EPOCH, MIN_PAX_CONN, carrier_of, fdp_limit

TIMEOUT_MS = 8000
TURN_PAD = 15  # FDP ends 15 min after block-in
SIGNIFICANT_DELAY = 180  # 14 CFR 260.6: a domestic arrival 3h+ late is a significant change


def hhmm(m, tz="UTC"):
    return (EPOCH + timedelta(minutes=int(m))).astimezone(ZoneInfo(tz)).strftime("%a %d %b %H:%M")


def key(n):
    p = n.properties
    return p.get("id") or p.get("code") or p.get("tail") or p.get("name") or str(n.id)


def ser_path(p: Path):
    nodes = [{"key": key(n), "label": n.labels[0],
              **{k: n.properties[k] for k in ("fno", "origin", "dest", "dep", "arr", "delay", "status", "code", "base")
                 if k in n.properties}} for n in p.nodes()]
    ids = {n.id: key(n) for n in p.nodes()}
    edges = [{"type": e.relation, "src": ids.get(e.src_node), "dst": ids.get(e.dest_node), **e.properties}
             for e in p.edges()]
    return {"nodes": nodes, "edges": edges}


def plain(v):
    if isinstance(v, Path):
        return ser_path(v)
    if isinstance(v, (Node, Edge)):
        return v.properties
    if isinstance(v, list):
        return [plain(x) for x in v]
    return v


class Tools:
    """One instance per tenant graph (one airline = one isolated FalkorDB graph)."""

    def __init__(self, tenant="irops_WN"):
        self.tenant = tenant
        self.carrier = carrier_of(tenant)
        self.g = graph(tenant)
        self.trace = []  # every query this toolbox ran, for the UI

    # -- plumbing -----------------------------------------------------------
    def _run(self, q, params=None, write=False):
        q = " ".join(q.split())
        self.trace.append(q)
        fn = self.g.query if write else self.g.ro_query  # ro_query: the server refuses writes
        res = fn(q, params or {}, timeout=TIMEOUT_MS)
        cols = [h[1] if isinstance(h, (list, tuple)) else h for h in res.header]
        return [dict(zip(cols, (plain(v) for v in row))) for row in res.result_set]

    def _result(self, outcome, rows=None, paths=None, start=0, **extra):
        return {"outcome": outcome, "rows": rows or [], "paths": paths or [],
                "cypher": self.trace[start:], **extra}

    def now(self):
        r = self._run("MATCH (c:Clock {id: 'sim'}) RETURN c.now AS now")
        return r[0]["now"] if r else 0

    def set_clock(self, minutes_since_epoch):
        self._run("MERGE (c:Clock {id: 'sim'}) SET c.now = $t", {"t": int(minutes_since_epoch)}, write=True)

    # -- resolution: never trust a name the model typed ---------------------
    def resolve_flight(self, ref):
        """Accepts a full flight id, or a flight number like 'WN2262' optionally with a date and/or origin."""
        start = len(self.trace)
        rows = self._run("MATCH (f:Flight {id: $ref}) RETURN f.id AS id, f.fno AS fno, f.origin AS origin, "
                         "f.dest AS dest, f.dep AS dep, f.status AS status", {"ref": ref})
        if not rows:
            u = ref.upper()
            m = re.search(rf"(?:{self.carrier})?\s*(\d{{1,4}})\b", re.sub(r"2022-12-\d\d", " ", u))
            date = re.search(r"2022-12-\d\d", ref)
            ap = re.findall(r"\b([A-Z]{3})\b", u.replace(self.carrier, " "))
            if m:
                rows = self._run("""MATCH (f:Flight {fno: $fno})
                    WHERE ($date IS NULL OR f.date = $date) AND ($ap IS NULL OR f.origin = $ap)
                    RETURN f.id AS id, f.fno AS fno, f.origin AS origin, f.dest AS dest, f.dep AS dep, f.status AS status
                    ORDER BY f.dep LIMIT 6""",
                                 {"fno": self.carrier + m.group(1), "date": date.group(0) if date else None,
                                  "ap": ap[0] if ap else None})
        return self._result("answered" if rows else "unknown_entity", rows, start=start)

    def _one_flight(self, ref):
        r = self.resolve_flight(ref)
        return r["rows"][0]["id"] if len(r["rows"]) == 1 else None, r

    def resolve_airport(self, name):
        """An airport code from a code or a name/city ('Denver', 'Midway'), via the full-text index."""
        start = len(self.trace)
        rows = self._run("MATCH (a:Airport {code: $c}) RETURN a.code AS code, a.name AS name, a.city AS city",
                         {"c": (name or "").strip().upper()})
        terms = " ".join(w + "*" for w in re.findall(r"[A-Za-z]{3,}", name or ""))  # prefix match per word
        if not rows and terms:
            rows = self._run("""CALL db.idx.fulltext.queryNodes('Airport', $q) YIELD node, score
                RETURN node.code AS code, node.name AS name, node.city AS city, score ORDER BY score DESC LIMIT 5""",
                             {"q": terms})
        return self._result("answered" if rows else "unknown_entity", rows, start=start)

    def _airport(self, code):
        return bool(self._run("MATCH (a:Airport {code: $c}) RETURN a.code AS c", {"c": (code or "").upper()}))

    # -- T01: act on connected data ----------------------------------------
    def propagate_delay(self, roots, max_hops=8):
        """roots: [{"flight": id-or-ref, "delay": minutes}]. Follows aircraft rotations
        (NEXT_LEG) and crew connections (CREW_NEXT), consuming slack at each hop."""
        start = len(self.trace)
        resolved, unknown = [], []
        for r in roots:
            fid, _ = self._one_flight(str(r["flight"]))
            (resolved if fid else unknown).append({"id": fid or r["flight"], "delay": int(r["delay"])})
        if not resolved:
            return self._result("unknown_entity", start=start, unknown=unknown)
        hops = max(1, min(int(max_hops), 10))
        down = self._run(f"""
            UNWIND $roots AS r
            MATCH (f:Flight {{id: r.id}})
            MATCH p = (f)-[:NEXT_LEG|CREW_NEXT*1..{hops}]->(x:Flight:Active)
            WITH x, p, reduce(d = r.delay, e IN relationships(p) |
                 CASE WHEN d - e.slack > 0 THEN d - e.slack ELSE 0 END) AS d
            WHERE d > 0
            WITH x, d, p ORDER BY d DESC
            WITH x, collect(p)[0] AS p, max(d) AS d
            RETURN x.id AS id, x.fno AS fno, x.origin AS origin, x.dest AS dest, x.dep AS dep,
                   x.pax AS pax, d AS delay, p AS path
            ORDER BY x.dep""", {"roots": resolved})
        impact = {r["id"]: r["delay"] for r in resolved}
        for row in down:
            impact[row["id"]] = max(row["delay"], impact.get(row["id"], 0))
        imp = [{"id": k, "delay": v} for k, v in impact.items()]
        # who misses a connection, given both flights' projected delays
        mis = self._run("""
            UNWIND $imp AS i
            MATCH (a:Flight {id: i.id})-[c:PAX_CONNECT]->(d:Flight:Active)
            WITH a, c, d, i.delay - coalesce($impact[d.id], 0) AS lost
            WHERE lost > c.slack
            RETURN a.id AS inbound, d.id AS outbound, d.origin AS at, c.pax AS pax, lost - c.slack AS short_by
            ORDER BY pax DESC""", {"imp": imp, "impact": impact})
        # crews pushed past their flight-duty-period limit (14 CFR 117.13)
        fdp = self._run("""
            UNWIND $imp AS i
            MATCH (c:Crew)-[a:ASSIGNED]->(f:Flight:Active {id: i.id}) WHERE a.relieved_by IS NULL
            WITH c, f, a, f.arr + i.delay + $pad - a.duty_start - a.fdp_limit AS over
            WHERE over > 0
            MATCH (r:Rule {id: '14CFR117.13'})
            RETURN c.id AS crew, f.id AS flight, f.origin AS at, over AS over_by, r.id AS rule, r.url AS rule_url
            ORDER BY f.dep""", {"imp": imp, "pad": TURN_PAD})
        # passengers owed a refund if they ask (14 CFR 260.6): last leg 3h+ late, or a leg cancelled
        owed = self._run("""
            UNWIND $imp AS i
            MATCH (f:Flight {id: i.id})<-[s:SEGMENT]-(b:Booking) WHERE i.delay >= $sig AND b.status <> 'refunded'
            OPTIONAL MATCH (b)-[n:SEGMENT]->() WHERE n.seq > s.seq
            WITH b, count(n) AS later WHERE later = 0
            RETURN collect(DISTINCT b.id) AS late""", {"imp": imp, "sig": SIGNIFICANT_DELAY})[0]["late"]
        owed += self._run("""MATCH (f:Flight) WHERE NOT f:Active MATCH (f)<-[:SEGMENT]-(b:Booking) WHERE b.status <> 'refunded'
            RETURN collect(DISTINCT b.id) AS c""")[0]["c"]
        owed_pax = self._run("UNWIND $ids AS i MATCH (b:Booking {id: i}) RETURN coalesce(sum(b.pax), 0) AS n",
                             {"ids": sorted(set(owed))})[0]["n"]
        paths = [r.pop("path") for r in down]
        summary = {"roots": len(resolved), "downstream_flights": len(down),
                   "delay_minutes": sum(r["delay"] for r in down),
                   "pax_delayed": sum(r["pax"] for r in down),
                   "pax_misconnecting": int(sum(m["pax"] for m in mis)), "crews_illegal": len({b["crew"] for b in fdp}),
                   "crew_legs_illegal": len(fdp),
                   "pax_refund_eligible": int(owed_pax)}
        return self._result("answered" if down or mis or fdp else "not_in_graph", down, paths, start,
                            misconnects=mis, crew_fdp_breaches=fdp, summary=summary, unknown=unknown)

    def crew_duty(self, crew_id):
        """A crew's assigned legs with projected times against its FDP limit."""
        start = len(self.trace)
        rows = self._run("""
            MATCH (c:Crew {id: $id})-[a:ASSIGNED]->(f:Flight) WHERE a.relieved_by IS NULL
            RETURN c.base AS base, f.id AS flight, f.origin AS origin, f.dest AS dest, f.dep AS dep,
                   f.arr AS arr, f.delay AS delay, f.status AS status, a.duty_start AS duty_start,
                   a.fdp_limit AS fdp_limit, f.arr + f.delay + $pad - a.duty_start AS fdp_used
            ORDER BY f.dep""", {"id": crew_id, "pad": TURN_PAD})
        if not rows:
            return self._result("unknown_entity", start=start)
        p = self._run("MATCH p = (:Crew {id: $id})-[a:ASSIGNED]->(:Flight) WHERE a.relieved_by IS NULL RETURN p",
                      {"id": crew_id})
        return self._result("answered", rows, [x["p"] for x in p], start)

    def crews_over_limit(self, airport=None):
        """Crews that the delays now in the graph push past 14 CFR 117.13, projected through rotations and crew
        connections like propagate_delay; with `airport`, only breaches on legs departing there."""
        start = len(self.trace)
        roots = self._run("MATCH (f:Flight:Active {status: 'delayed'}) RETURN f.id AS flight, f.delay AS delay")
        ap = (airport or "").upper()
        if ap and not self._airport(ap):
            return self._result("unknown_entity", start=start)
        if not roots:
            return self._result("not_in_graph", start=start, reason="no delays recorded in the graph yet")
        res = self.propagate_delay(roots)
        rows = [b for b in res.get("crew_fdp_breaches", []) if not ap or b["at"] == ap]
        # the evidence: each crew's assignment to the leg it cannot legally finish
        paths = [x["p"] for x in self._run("""UNWIND $b AS b MATCH p = (:Crew {id: b.crew})-[a:ASSIGNED]->(:Flight {id: b.flight})
            WHERE a.relieved_by IS NULL RETURN p""", {"b": [{"crew": b["crew"], "flight": b["flight"]} for b in rows[:30]]})]
        return self._result("answered" if rows else "not_in_graph", rows, paths, start,
                            crews=len({b["crew"] for b in rows}), roots=len(roots))

    def find_reserve(self, airport, need_at=None, minutes_needed=360):
        """Free reserve crews on call at `airport`; if none, the nearest base with a free
        reserve, routed by A* over the route network (haversine heuristic)."""
        start = len(self.trace)
        airport = (airport or "").upper()
        if not self._airport(airport):
            return self._result("unknown_entity", start=start)
        t = int(need_at if need_at is not None else self.now())
        free = """(r:Crew {reserve: true})-[:BASED_AT]->(b:Airport)
                  WHERE r.on_call_from <= $t AND $t + $need <= r.on_call_to + 120 AND NOT (r)-[:CLAIMED_FOR]->()"""
        local = self._run(f"""MATCH p = {free} AND b.code = $ap
            RETURN r.id AS reserve, b.code AS base, r.on_call_to AS on_call_to, 0 AS deadhead_minutes, p AS path
            ORDER BY r.id LIMIT 5""", {"t": t, "need": int(minutes_needed), "ap": airport})
        if local:
            return self._result("answered", local, [r.pop("path") for r in local], start)
        rows = self._run(f"""
            MATCH {free} AND b.code <> $ap
            WITH b, collect(r.id)[0] AS reserve
            MATCH (a:Airport {{code: $ap}})
            CALL algo.AStar({{sourceNode: b, targetNode: a, relTypes: ['ROUTE'], weightProp: 'block',
                             latitudeProperty: 'lat', longitudeProperty: 'lon',
                             heuristicScale: 0.0000666}})
            YIELD path, pathWeight
            RETURN reserve, b.code AS base, pathWeight + 30 * (length(path) - 1) AS deadhead_minutes, path
            ORDER BY deadhead_minutes LIMIT 3""", {"t": t, "need": int(minutes_needed), "ap": airport})
        paths = [r.pop("path") for r in rows]
        return self._result("answered" if rows else "not_in_graph", rows, paths, start)

    def critical_flights(self, date="2022-12-22", top=10):
        """Load-bearing flights: betweenness over the aircraft + crew + passenger
        connection network. Delay one of these and the ripple is widest."""
        # ponytail: 64 samples; 256 crashes the 100 MB FalkorDB Cloud free tier. Raise on a bigger instance.
        start = len(self.trace)
        rows = self._run("""
            CALL algo.betweenness({nodeLabels: ['Active'], relationshipTypes: ['NEXT_LEG', 'CREW_NEXT', 'PAX_CONNECT'],
                                   samplingSize: 64, samplingSeed: 7})
            YIELD node, score
            WITH node, score WHERE node.date = $date AND node.dep >= $now
            RETURN node.id AS id, node.origin AS origin, node.dest AS dest, node.dep AS dep, node.pax AS pax,
                   round(score) AS betweenness
            ORDER BY score DESC LIMIT $top""", {"date": date, "top": int(top), "now": self.now()})
        return self._result("answered" if rows else "not_in_graph", rows, start=start)

    def network_health(self):
        """Weakly connected components of airports over flights still flying.
        Airports outside the main component are cut off from the network."""
        start = len(self.trace)
        comps = self._run("""
            CALL algo.WCC({nodeLabels: ['Airport', 'Active'], relationshipTypes: ['DEPARTS_FROM', 'ARRIVES_AT']})
            YIELD node, componentId
            WITH componentId, collect(node) AS ns
            WITH componentId, [n IN ns WHERE n:Airport | n.code] AS airports, size(ns) AS size
            RETURN componentId, airports, size ORDER BY size DESC""")
        stats = self._run("""MATCH (f:Flight) WHERE f.dep >= $now
            RETURN f.date AS date, count(*) AS scheduled, sum(CASE WHEN f:Active THEN 0 ELSE 1 END) AS cancelled,
                   sum(CASE WHEN f.delay > 0 THEN 1 ELSE 0 END) AS delayed ORDER BY date""", {"now": self.now()})
        cut_off = [a for c in comps[1:] for a in c["airports"]]
        return self._result("answered", stats, start=start, components=len(comps), cut_off_airports=cut_off)

    def rebook_options(self, booking_id, k=3):
        """k-shortest feasible itineraries for a disrupted booking, via algo.SPpaths over the
        time-expanded connection network. Seats, cancellations and projected delays are checked."""
        start = len(self.trace)
        b = self._run("""
            MATCH (b:Booking {id: $id})-[s:SEGMENT]->(f:Flight)
            RETURN b.pax AS pax, b.origin AS origin, b.dest AS dest, b.planned_arr AS planned_arr,
                   s.seq AS seq, f.id AS flight, f.origin AS f_origin, f.dep AS dep, f.arr AS arr, f.delay AS delay,
                   f:Active AS active ORDER BY s.seq""", {"id": booking_id})
        if not b:
            return self._result("unknown_entity", start=start)
        pax, dest = b[0]["pax"], b[0]["dest"]
        first, last = b[0], b[-1]
        if not first["active"]:          # never left origin
            at, ready = first["origin"], max(self.now(), first["dep"])
        else:                            # stuck at the connection point
            at, ready = last["f_origin"], first["arr"] + first["delay"] + MIN_PAX_CONN
        rows = self._run("""
            MATCH (s:Flight:Active)-[:DEPARTS_FROM]->(:Airport {code: $at})
            WHERE s.dep + s.delay >= $ready AND s.seats - s.pax >= $pax AND NOT s.id IN $current
            WITH s ORDER BY CASE WHEN s.dest = $dest THEN 0 ELSE 1 END, s.dep LIMIT 16
            MATCH (t:Airport {code: $dest})
            CALL algo.SPpaths({sourceNode: s, targetNode: t, relTypes: ['CONN', 'ARRIVES_AT'],
                               weightProp: 'w', maxLen: 3, pathCount: 4})
            YIELD path, pathWeight
            WITH path, pathWeight, [n IN nodes(path) WHERE n:Flight] AS fl
            WHERE all(n IN fl WHERE n:Active AND n.seats - n.pax >= $pax)
              AND all(i IN range(0, size(fl) - 2) WHERE fl[i].arr + fl[i].delay + $mct <= fl[i + 1].dep + fl[i + 1].delay)
            WITH path, fl, fl[size(fl) - 1] AS l
            RETURN [n IN fl | n.id] AS flights, size(fl) - 1 AS connections,
                   l.arr + l.delay AS arrives, l.arr + l.delay - $planned AS late_by, path
            ORDER BY arrives, connections LIMIT $k""",
                         {"at": at, "dest": dest, "ready": int(ready), "pax": pax, "mct": MIN_PAX_CONN,
                          "planned": first["planned_arr"], "k": int(k),
                          "current": [x["flight"] for x in b]})
        for r in rows:
            r["arrives_local"] = hhmm(r["arrives"], self._tz(dest))
        paths = [r.pop("path") for r in rows]
        return self._result("answered" if rows else "not_in_graph", rows, paths, start,
                            booking={"id": booking_id, "pax": pax, "stranded_at": at, "dest": dest})

    def rescue_capacity(self, origin, dest, hours=24):
        """Most passengers the rest of the network can still move from origin to dest in the next `hours`:
        max flow over the route network, each route's capacity the free seats on its active flights still to
        depart. Connection times are not checked, so this is an upper bound: if the stranded passengers exceed
        it, no rebooking plan can carry them all and the rest are refunds. Rows are the last legs into dest (the
        solver's edge flows can include cycles back through origin, so only flow into the sink is reported)."""
        start = len(self.trace)
        o, d = (origin or "").upper(), (dest or "").upper()
        if o == d or not self._airport(o) or not self._airport(d):
            return self._result("unknown_entity", start=start)
        now, hours = self.now(), max(1, min(int(hours), 48))
        # ponytail: free seats are written onto ROUTE (algo.maxFlow reads an edge property). Idempotent and
        # derived from the flights, so concurrent callers write the same values.
        self._run("""MATCH (a:Airport)-[r:ROUTE]->(b:Airport)
            OPTIONAL MATCH (a)<-[:DEPARTS_FROM]-(f:Flight:Active)-[:ARRIVES_AT]->(b)
                WHERE f.dep + f.delay >= $now AND f.dep < $now + $h
            WITH r, sum(CASE WHEN f.seats > f.pax THEN f.seats - f.pax ELSE 0 END) AS free
            SET r.free = free""", {"now": now, "h": int(hours) * 60}, write=True)
        rows = self._run("""MATCH (s:Airport {code: $o}), (t:Airport {code: $d})
            CALL algo.maxFlow({sourceNodes: [s], targetNodes: [t], relationshipTypes: ['ROUTE'],
                               capacityProperty: 'free'})
            YIELD edges, edgeFlows, maxFlow
            UNWIND range(0, size(edges) - 1) AS i
            WITH maxFlow, startNode(edges[i]).code AS from, endNode(edges[i]).code AS to, edgeFlows[i] AS flow
            WHERE to = $d AND flow > 0
            RETURN maxFlow, from, to, flow ORDER BY flow DESC""", {"o": o, "d": d})
        ends = self._run("""MATCH (s:Airport {code: $o}), (t:Airport {code: $d})
            OPTIONAL MATCH (s)-[a:ROUTE]->() WITH t, sum(a.free) AS out_seats
            OPTIONAL MATCH ()-[b:ROUTE]->(t) RETURN out_seats, sum(b.free) AS in_seats""", {"o": o, "d": d})[0]
        total = int(rows[0]["maxFlow"]) if rows else 0
        for r in rows:  # named so a model reading the rows does not take one leg for the answer
            r.pop("maxFlow")
            r["last_leg_pax"] = int(r.pop("flow"))
        out_s, in_s = int(ends["out_seats"] or 0), int(ends["in_seats"] or 0)
        limit = "seats out of " + o if out_s <= in_s else "seats into " + d
        if total < min(out_s, in_s):
            limit = "the connecting routes in between"
        # each last leg that carries flow, as a one-edge path the evidence canvas can draw
        paths = [{"nodes": [{"key": r["from"], "label": "Airport", "code": r["from"]},
                            {"key": d, "label": "Airport", "code": d}],
                  "edges": [{"type": "ROUTE", "src": r["from"], "dst": d, "flow": r["last_leg_pax"]}]} for r in rows]
        return self._result("answered" if total else "not_in_graph", rows, paths, start=start, origin=o, dest=d,
                            answer=f"At most {total} passengers in total can still get from {o} to {d} in the next "
                                   f"{int(hours)}h; the rows split that total over the last legs into {d}.",
                            hours=int(hours), max_passengers=total, seats_leaving_origin=out_s, seats_arriving_dest=in_s, bottleneck=limit)

    def _tz(self, code):
        r = self._run("MATCH (a:Airport {code: $c}) RETURN a.tz AS tz", {"c": code})
        return r[0]["tz"] if r else "UTC"

    def disrupted_bookings(self, limit=20):
        """Bookings whose itinerary is broken: a cancelled leg, or a connection the
        projected delays make impossible."""
        start = len(self.trace)
        rows = self._run("""
            MATCH (b:Booking {status: 'confirmed'})-[s1:SEGMENT {seq: 1}]->(a:Flight), (b)-[:SEGMENT {seq: 2}]->(d:Flight)
            WHERE a.dep >= $now - 600 AND (NOT a:Active OR NOT d:Active OR a.arr + a.delay + $mct > d.dep + d.delay)
            RETURN b.id AS booking, b.pax AS pax, b.origin AS origin, b.dest AS dest, a.id AS first, d.id AS second,
                   CASE WHEN NOT a:Active THEN 'first leg cancelled' WHEN NOT d:Active THEN 'second leg cancelled'
                        ELSE 'misconnect' END AS reason
            ORDER BY a.dep LIMIT $limit""", {"now": self.now(), "mct": MIN_PAX_CONN, "limit": int(limit)})
        return self._result("answered" if rows else "not_in_graph", rows, start=start)

    # -- T02: memory and coordination (memory = nodes in the same graph) -----
    def recall(self, about):
        """Earlier decisions and lessons linked to a flight, crew, airport or booking."""
        start = len(self.trace)
        rows = self._run("""
            MATCH (d:Decision)-[:ABOUT]->(x) WHERE coalesce(x.id, x.code, x.tail) = $k
               OR (x:Airport AND x.code = $k)
            OPTIONAL MATCH (d)-[:CITES]->(r:Rule)
            RETURN d.id AS decision, d.agent AS agent, d.kind AS kind, d.summary AS summary, d.status AS status,
                   d.at AS at, collect(r.id) AS rules ORDER BY d.at DESC LIMIT 10""", {"k": about})
        lessons = self._run("""MATCH (l:Lesson)-[:ABOUT]->(x) WHERE coalesce(x.id, x.code) = $k
            RETURN l.text AS lesson, l.at AS at, l.session AS session, coalesce(l.seen, 1) AS times_seen
            ORDER BY l.at DESC LIMIT 5""", {"k": about})
        return self._result("from_memory" if rows or lessons else "not_in_graph", rows, start=start, lessons=lessons)

    def remember(self, agent, kind, summary, about, rules=(), evidence=None, status="proposed", session=None,
                 action=None):
        """Writes a Decision node linked to what it is about and the rules it relies on.
        `action` is the JSON of the operational write a human must approve. If a human already rejected this
        exact action (and nobody has reset since), it is stored as 'withheld': visible, not approvable."""
        start = len(self.trace)
        action = json.dumps(action) if action else None
        if action and status == "proposed":
            prior = self._run("""MATCH (d:Decision {status: 'rejected', action: $a}) WHERE d.cleared IS NULL
                RETURN d.id AS id ORDER BY d.at DESC LIMIT 1""", {"a": action})
            if prior:
                status = "withheld"
                summary += f" Withheld: a human rejected this exact action earlier (decision {prior[0]['id'][:8]})."
        rows = self._run("""
            MATCH (ag:Agent {name: $agent})
            CREATE (d:Decision {id: randomUUID(), agent: $agent, kind: $kind, summary: $summary,
                                status: $status, at: $at, session: $session, evidence: $evidence,
                                action: $action})
            CREATE (ag)-[:DECIDED]->(d)
            WITH d
            OPTIONAL MATCH (x) WHERE (x:Flight AND x.id IN $about) OR (x:Crew AND x.id IN $about)
                                  OR (x:Booking AND x.id IN $about) OR (x:Airport AND x.code IN $about)
            FOREACH (_ IN CASE WHEN x IS NULL THEN [] ELSE [1] END | MERGE (d)-[:ABOUT]->(x))
            WITH DISTINCT d
            OPTIONAL MATCH (r:Rule) WHERE r.id IN $rules
            FOREACH (_ IN CASE WHEN r IS NULL THEN [] ELSE [1] END | MERGE (d)-[:CITES]->(r))
            RETURN DISTINCT d.id AS decision""",
                         {"agent": agent, "kind": kind, "summary": summary, "status": status, "at": self.now(),
                          "session": session, "evidence": json.dumps(evidence) if evidence else "", "about": list(about),
                          "rules": list(rules), "action": action}, write=True)
        return self._result("answered" if rows else "rejected", rows, start=start)

    def handoff(self, frm, to, kind, about, payload, session):
        """Agent-to-agent work item, stored in the graph so any agent (or a restarted one) can pick it up."""
        start = len(self.trace)
        rows = self._run("""
            CREATE (h:Handoff {id: randomUUID(), from: $frm, to: $to, kind: $kind, payload: $payload,
                               status: 'open', at: $at, session: $s})
            WITH h MATCH (x:Flight) WHERE x.id IN $about MERGE (h)-[:ABOUT]->(x)
            RETURN DISTINCT h.id AS handoff""",
                         {"frm": frm, "to": to, "kind": kind, "payload": payload, "at": self.now(), "s": session,
                          "about": list(about)}, write=True)
        return self._result("answered", rows, start=start)

    def inbox(self, agent, session):
        start = len(self.trace)
        rows = self._run("""MATCH (h:Handoff {to: $a, status: 'open', session: $s})
            OPTIONAL MATCH (h)-[:ABOUT]->(f:Flight)
            WITH h, collect(f.id) AS flights
            RETURN h.id AS handoff, h.from AS sender, h.kind AS kind, h.payload AS payload, flights, h.at AS at
            ORDER BY at""", {"a": agent, "s": session})
        return self._result("answered" if rows else "not_in_graph", rows, start=start)

    def close_handoff(self, handoff_id):
        self._run("MATCH (h:Handoff {id: $id}) SET h.status = 'done'", {"id": handoff_id}, write=True)

    def decisions(self, session=None, status=None):
        return self._run("""MATCH (d:Decision) WHERE ($s IS NULL OR d.session = $s) AND ($st IS NULL OR d.status = $st)
            OPTIONAL MATCH (d)-[:CITES]->(r:Rule)
            OPTIONAL MATCH (d)-[:ABOUT]->(x)
            RETURN d.id AS id, d.agent AS agent, d.kind AS kind, d.summary AS summary, d.status AS status,
                   d.action AS action, d.evidence AS evidence, d.at AS at,
                   collect(DISTINCT r.id) AS rules, collect(DISTINCT coalesce(x.id, x.code)) AS about
            ORDER BY d.at, d.kind""", {"s": session, "st": status})

    def take_decision(self, decision_id):
        """Atomically move an open proposal to 'executing'; a second approver gets nothing."""
        r = self._run("""MATCH (d:Decision {id: $id, status: 'proposed'}) WHERE d.action IS NOT NULL
            SET d.status = 'executing' RETURN d.action AS action, d.summary AS summary""", {"id": decision_id}, write=True)
        return r[0] if r else None

    def set_decision(self, decision_id, status, outcome=""):
        self._run("MATCH (d:Decision {id: $id}) SET d.status = $st, d.outcome = $o",
                  {"id": decision_id, "st": status, "o": outcome}, write=True)

    def learn(self, text, about, session):
        start = len(self.trace)
        # the same lesson learned again is counted, not duplicated
        self._run("""MERGE (l:Lesson {text: $t}) ON CREATE SET l.first_at = $at
            SET l.at = $at, l.session = $s, l.seen = toInteger(coalesce(l.seen, 0)) + 1 WITH l
            MATCH (x) WHERE (x:Airport AND x.code IN $about) OR (x:Flight AND x.id IN $about) MERGE (l)-[:ABOUT]->(x)""",
                  {"t": text, "at": self.now(), "s": session, "about": list(about)}, write=True)
        return self._result("answered", start=start)

    def claim_reserve(self, agent, reserve_id, flight_ref):
        """Atomic check-and-claim: FalkorDB runs each write query alone, so two agents
        racing for the same reserve cannot both win."""
        start = len(self.trace)
        fid, _ = self._one_flight(flight_ref)
        if not fid:
            return self._result("unknown_entity", start=start)
        rows = self._run("""
            MATCH (r:Crew {id: $rid, reserve: true}), (f:Flight {id: $fid}), (a:Agent {name: $agent})
            WHERE NOT (r)-[:CLAIMED_FOR]->()
            CREATE (r)-[:CLAIMED_FOR {by: $agent, at: $at}]->(f), (a)-[:CLAIMED {at: $at}]->(r)
            RETURN r.id AS reserve, f.id AS flight""",
                         {"rid": reserve_id, "fid": fid, "agent": agent, "at": self.now()}, write=True)
        if rows:
            return self._result("answered", rows, start=start)
        holder = self._run("MATCH (r:Crew {id: $rid})-[c:CLAIMED_FOR]->(f) RETURN c.by AS by, f.id AS flight",
                           {"rid": reserve_id})
        return self._result("rejected", holder, start=start, reason="already claimed" if holder else "not a reserve")

    # -- writes that change the operation (gated by human approval upstream) -
    def set_delay(self, flight_ref, delay):
        start = len(self.trace)
        fid, _ = self._one_flight(flight_ref)
        if not fid:
            return self._result("unknown_entity", start=start)
        rows = self._run("MATCH (f:Flight {id: $id}) SET f.delay = $d, f.status = CASE WHEN $d > 0 THEN 'delayed' "
                         "ELSE f.status END RETURN f.id AS id, f.delay AS delay", {"id": fid, "d": int(delay)}, write=True)
        return self._result("answered", rows, start=start)

    def cancel_with_return(self, flight_ref, agent="ops", reason=""):
        """Cancel a flight and the aircraft's return leg, so the tail ends where it began
        (the standard IROPS containment move). Cancelled flights lose :Active, so every
        algorithm and query stops routing through them; reset_ops can restore them."""
        start = len(self.trace)
        fid, _ = self._one_flight(flight_ref)
        if not fid:
            return self._result("unknown_entity", start=start)
        pair = self._run("""
            MATCH (f:Flight {id: $id})
            OPTIONAL MATCH (f)-[:NEXT_LEG]->(g:Flight) WHERE g.dest = f.origin
            RETURN f.id AS out, g.id AS back""", {"id": fid})
        ids = [x for x in (pair[0]["out"], pair[0]["back"]) if x]
        self._run("""UNWIND $ids AS i MATCH (f:Flight {id: i})
            REMOVE f:Active SET f.status = 'cancelled', f.cancel_reason = $why""", {"ids": ids, "why": reason}, write=True)
        hit = self._run("""UNWIND $ids AS i MATCH (b:Booking)-[:SEGMENT]->(:Flight {id: i})
            RETURN count(DISTINCT b) AS bookings, toInteger(sum(b.pax)) AS pax""", {"ids": ids})
        return self._result("answered", [{"cancelled": ids, **hit[0]}], start=start)

    def assign_reserve(self, reserve_id, flight_id, replaces, legs=None):
        """Swap the timed-out crew for the claimed reserve. Requires the claim edge, so an
        agent cannot assign a reserve it does not hold."""
        start = len(self.trace)
        f = self._run("MATCH (f:Flight {id: $f}) RETURN f.origin AS o", {"f": flight_id})
        if not f:
            return self._result("unknown_entity", start=start)
        hour = int(hhmm(self.now(), self._tz(f[0]["o"]))[-5:-3])
        # `legs`: the timed-out crew's legs the reserve takes over, from flight_id on (as many as it may fly legally).
        # The relieved crew's inbound CREW_NEXT stops carrying delay: it is retyped to CREW_NEXT_RELIEVED
        # (filtering every variable-length path instead costs ~90x) and kept, so reset_ops can restore it.
        rows = self._run("""
            MATCH (r:Crew {id: $r})-[:CLAIMED_FOR]->(f:Flight {id: $f})
            WHERE NOT (r)-[:ASSIGNED]->(f)
            OPTIONAL MATCH (:Crew {id: $old})-[a:ASSIGNED]->(f)
            SET a.relieved_by = $r
            WITH DISTINCT r, f
            OPTIONAL MATCH (pv:Flight)-[cn:CREW_NEXT {crew: $old}]->(f)
            FOREACH (_ IN CASE WHEN cn IS NULL THEN [] ELSE [1] END |
                CREATE (pv)-[:CREW_NEXT_RELIEVED {crew: cn.crew, slack: cn.slack, relieved_by: $r}]->(f))
            DELETE cn
            WITH DISTINCT r, f
            CREATE (r)-[:ASSIGNED {duty_start: $now, fdp_limit: $limit, replaced: $old}]->(f)
            WITH r, f
            OPTIONAL MATCH (:Crew {id: $old})-[b:ASSIGNED]->(g:Flight) WHERE g.id IN $later AND b.relieved_by IS NULL
            FOREACH (_ IN CASE WHEN b IS NULL THEN [] ELSE [1] END |
                SET b.relieved_by = $r
                CREATE (r)-[:ASSIGNED {duty_start: $now, fdp_limit: $limit, replaced: $old}]->(g))
            RETURN r.id AS reserve, f.id AS flight, count(g) AS later_legs""",
                         {"r": reserve_id, "f": flight_id, "old": replaces, "now": self.now(),
                          "later": [x for x in (legs or [flight_id]) if x != flight_id],
                          "limit": fdp_limit(hour, len(legs or [flight_id]))}, write=True)
        return self._result("answered" if rows else "rejected", rows, start=start)

    def apply_rebook(self, booking_id, flights, agent="pax"):
        """Move a booking onto new flights; seats are re-checked inside the write."""
        start = len(self.trace)
        rows = self._run("""
            MATCH (b:Booking {id: $id})-[s0:SEGMENT]->(o0:Flight)
            WITH b, s0, o0 ORDER BY s0.seq
            WITH b, collect(o0.id) AS was
            SET b.orig = coalesce(b.orig, was)
            WITH b, [i IN $flights | i] AS want
            MATCH (f:Flight:Active) WHERE f.id IN want AND f.seats - f.pax >= b.pax
            WITH b, want, collect(f) AS fs WHERE size(fs) = size(want)
            OPTIONAL MATCH (b)-[old:SEGMENT]->(o:Flight)
            SET o.pax = o.pax - b.pax
            DELETE old
            WITH DISTINCT b, want, fs
            UNWIND range(0, size(want) - 1) AS i
            MATCH (f:Flight {id: want[i]})
            CREATE (b)-[:SEGMENT {seq: i + 1}]->(f)
            SET f.pax = f.pax + b.pax, b.status = 'rebooked'
            RETURN DISTINCT b.id AS booking""", {"id": booking_id, "flights": list(flights)}, write=True)
        return self._result("answered" if rows else "rejected", rows, start=start)

    def refund(self, booking_id):
        """Refund a booking nobody can re-route (14 CFR 260.6): its seats go back on sale. The itinerary is kept
        in b.orig, so reset_ops restores it."""
        start = len(self.trace)
        rows = self._run("""
            MATCH (b:Booking {id: $id})-[s:SEGMENT]->(f:Flight) WHERE b.status <> 'refunded'
            WITH b, s, f ORDER BY s.seq
            WITH b, collect(s) AS ss, collect(f) AS fs
            SET b.orig = coalesce(b.orig, [f IN fs | f.id]), b.status = 'refunded'
            FOREACH (f IN fs | SET f.pax = f.pax - b.pax)
            FOREACH (s IN ss | DELETE s)
            RETURN b.id AS booking, b.pax AS pax, size(fs) AS seats_released_on""", {"id": booking_id}, write=True)
        return self._result("answered" if rows else "rejected", rows, start=start)

    def live_conditions(self, airport=None):
        """Airports in this airline's network that the FAA or the weather constrain right now, with how much of
        the network runs through each (routes and scheduled departures per day) and which spoke airports
        depend on it for most of their service. With `airport`: that airport's current METAR and any FAA
        entry, constrained or not."""
        start = len(self.trace)
        feed = self._run("""MATCH (f:LiveFeed {id: 'feed'}) RETURN f.fetched AS fetched, f.faa_time AS faa_time,
            f.adsb_airborne AS aircraft_airborne, f.adsb_ground AS aircraft_on_ground, f.adsb_time AS adsb_time,
            f.errors AS feed_errors""")
        if not feed:
            return self._result("not_in_graph", start=start, reason="live feed never refreshed")
        ap = (airport or "").upper() or None
        if ap and not self._airport(ap):
            return self._result("unknown_entity", start=start)
        rows = self._run("""
            MATCH (a:Airport) WHERE ($ap IS NULL AND (a)<-[:AT]-(:Live)) OR a.code = $ap
            OPTIONAL MATCH (l:Live)-[:AT]->(a)
            WITH a, [x IN collect({source: l.source, kind: l.kind, reason: l.reason, detail: l.detail, as_of: l.as_of})
                     WHERE x.source IS NOT NULL] AS live
            OPTIONAL MATCH (a)-[r:ROUTE]-(b:Airport)
            WITH a, live, count(DISTINCT b) AS routes, sum(r.daily) AS daily_flights
            OPTIONAL MATCH (s:Airport)-[sr:ROUTE]-(a)
            WITH a, live, routes, daily_flights, s, sr
            OPTIONAL MATCH (s)-[sx:ROUTE]-(:Airport)
            WITH a, live, routes, daily_flights, s, sr.daily AS via, sum(sx.daily) AS total
            WITH a, live, routes, daily_flights,
                 collect(DISTINCT CASE WHEN s IS NOT NULL AND via * 2 >= total THEN s.code END) AS dependent
            RETURN a.code AS airport, a.name AS name, a.wx_cat AS flight_category, a.wx_raw AS metar,
                   a.wx_at AS observed, routes, daily_flights, dependent AS spokes_mostly_served_from_here, live,
                   a.adsb_ground AS aircraft_on_ground_now
            ORDER BY daily_flights DESC""", {"ap": ap})
        if rows:  # how central each airport is (PageRank over routes) and how many passengers connect there
            rank = {r["code"]: i + 1 for i, r in enumerate(self._run(
                "CALL algo.pageRank('Airport', 'ROUTE') YIELD node, score RETURN node.code AS code ORDER BY score DESC"))}
            codes = [r["airport"] for r in rows]
            conn = {r["code"]: r["pax"] for r in self._run("""
                MATCH (f:Flight) WITH count(DISTINCT f.date) AS days
                MATCH (x:Flight)-[c:PAX_CONNECT]->(:Flight) WHERE x.dest IN $c
                WITH x.dest AS code, days, sum(c.pax) AS total
                RETURN code, toInteger(total / days) AS pax""", {"c": codes})}
            for r in rows:
                r["network_rank"], r["airports_ranked"] = rank.get(r["airport"]), len(rank)
                r["connecting_pax_per_day"] = int(conn.get(r["airport"]) or 0)
        wx = self._run("""MATCH (a:Airport) WHERE a.wx_cat IS NOT NULL
            RETURN a.wx_cat AS cat, count(*) AS airports ORDER BY airports DESC""")
        return self._result("answered" if rows else "not_in_graph", rows, start=start, feed=feed[0],
                            weather_categories=wx)

    def read_back(self, op, act):
        """After an approved write: read the graph again and check it now holds the intended state."""
        start = len(self.trace)
        if op == "cancel_with_return":
            rows = self._run("""MATCH (f:Flight {id: $id}) OPTIONAL MATCH (f)-[:NEXT_LEG]->(g:Flight {status: 'cancelled'})
                WHERE g.dest = f.origin
                RETURN f.id AS flight, f.status AS status, f:Active AS active, g.id AS return_leg""", {"id": act["flight"]})
            ok = rows and rows[0]["status"] == "cancelled" and not rows[0]["active"]
        elif op == "publish_delays":
            want = {x["flight"]: x["delay"] for x in act["delays"]}
            rows = self._run("UNWIND $ids AS i MATCH (f:Flight {id: i}) RETURN f.id AS flight, f.delay AS delay, f.status AS status",
                             {"ids": list(want)})
            ok = len(rows) == len(want) and all(r["delay"] == want[r["flight"]] for r in rows)
        elif op == "assign_reserve":
            rows = self._run("""MATCH (c:Crew)-[a:ASSIGNED]->(f:Flight {id: $f})
                RETURN c.id AS crew, c.reserve AS reserve, a.relieved_by AS relieved_by""", {"f": act["flight"]})
            ok = any(r["crew"] == act["reserve"] and not r["relieved_by"] for r in rows) and \
                all(r["relieved_by"] == act["reserve"] for r in rows if r["crew"] == act["replaces"])
        elif op == "apply_rebook":
            rows = self._run("""MATCH (b:Booking {id: $b})-[s:SEGMENT]->(f:Flight)
                RETURN b.status AS status, s.seq AS seq, f.id AS flight, f:Active AS active ORDER BY s.seq""",
                             {"b": act["booking"]})
            ok = [r["flight"] for r in rows] == list(act["flights"]) and all(r["active"] for r in rows)
        elif op == "refund":
            rows = self._run("""MATCH (b:Booking {id: $b}) OPTIONAL MATCH (b)-[s:SEGMENT]->()
                RETURN b.status AS status, count(s) AS segments""", {"b": act["booking"]})
            ok = rows and rows[0]["status"] == "refunded" and rows[0]["segments"] == 0
        else:
            rows, ok = [], False
        return self._result("answered" if ok else "rejected", rows, start=start,
                            reason=None if ok else "the graph does not show the intended state")

    def reset_ops(self, forget_rejections=False):
        """Undo every simulated operational change: delays, cancellations, reserve swaps,
        rebookings, refunds and claims. Memory (decisions, lessons) is kept; with forget_rejections
        (the operator's reset, not a scenario reload) earlier rejections stop withholding proposals."""
        self._run("MATCH (f:Flight) WHERE f.status <> 'scheduled' SET f.delay = 0, f.status = 'scheduled', f:Active",
                  write=True)
        self._run("MATCH (:Crew)-[a:ASSIGNED]->() WHERE a.replaced IS NOT NULL DELETE a", write=True)
        self._run("MATCH ()-[a:ASSIGNED]->() WHERE a.relieved_by IS NOT NULL SET a.relieved_by = NULL", write=True)
        self._run("""MATCH (a:Flight)-[x:CREW_NEXT_RELIEVED]->(b:Flight)
            CREATE (a)-[:CREW_NEXT {crew: x.crew, slack: x.slack}]->(b) DELETE x""", write=True)
        for b in self._run("MATCH (b:Booking) WHERE b.orig IS NOT NULL RETURN b.id AS id"):
            # one booking per query, so pax counts on shared flights never race within a statement
            self._run("MATCH (b:Booking {id: $id})-[s:SEGMENT]->(f:Flight) SET f.pax = f.pax - b.pax DELETE s",
                      {"id": b["id"]}, write=True)
            self._run("""MATCH (b:Booking {id: $id}) UNWIND range(0, size(b.orig) - 1) AS i
                MATCH (f:Flight {id: b.orig[i]}) CREATE (b)-[:SEGMENT {seq: i + 1}]->(f) SET f.pax = f.pax + b.pax""",
                      {"id": b["id"]}, write=True)
            self._run("MATCH (b:Booking {id: $id}) SET b.status = 'confirmed', b.orig = NULL", {"id": b["id"]}, write=True)
        self._run("MATCH (:Crew)-[c:CLAIMED_FOR]->() DELETE c", write=True)
        self._run("MATCH (:Agent)-[c:CLAIMED]->() DELETE c", write=True)
        # proposals made against the old state must not be approved against the new one
        self._run("MATCH (d:Decision) WHERE d.status IN ['proposed', 'withheld'] SET d.status = 'superseded'", write=True)
        if forget_rejections:  # they stay on record, marked cleared
            self._run("MATCH (d:Decision {status: 'rejected'}) WHERE d.cleared IS NULL SET d.cleared = true", write=True)


# Tool schemas exposed to the LLM. Writes that change the operation are NOT here:
# agents propose them, a human approves, the orchestrator applies them.
CATALOGUE = {
    "resolve_flight": ("Find a flight by id or by flight number (e.g. 'WN2262 2022-12-22 DEN').",
                       {"ref": {"type": "string"}}, ["ref"]),
    "resolve_airport": ("Airport code from a code, airport name or city (full-text search), e.g. 'Midway'.",
                        {"name": {"type": "string"}}, ["name"]),
    "propagate_delay": ("Project how delays ripple through aircraft rotations and crew connections. "
                        "Returns downstream delayed flights with the graph path behind each, passenger "
                        "misconnects and crews pushed past their duty limit.",
                        {"roots": {"type": "array", "items": {"type": "object", "properties": {
                            "flight": {"type": "string"}, "delay": {"type": "integer"}},
                            "required": ["flight", "delay"], "additionalProperties": False}},
                         "max_hops": {"type": "integer"}}, ["roots", "max_hops"]),
    "crew_duty": ("A crew's legs, projected flight-duty-period use and limit.", {"crew_id": {"type": "string"}}, ["crew_id"]),
    "crews_over_limit": ("Crews the current delays push past their legal flight-duty limit (14 CFR 117.13), "
                         "optionally only at one airport (3-letter code).",
                         {"airport": {"type": "string"}}, []),
    "find_reserve": ("Free reserve crews at an airport, or the nearest base with one (A* deadhead route).",
                     {"airport": {"type": "string"}, "minutes_needed": {"type": "integer"}}, ["airport", "minutes_needed"]),
    "critical_flights": ("Most load-bearing remaining flights of a day (betweenness centrality).",
                         {"date": {"type": "string"}, "top": {"type": "integer"}}, ["date", "top"]),
    "network_health": ("Cancelled/delayed counts and airports cut off from the network (WCC).", {}, []),
    "disrupted_bookings": ("Bookings broken by cancellations or impossible connections.",
                           {"limit": {"type": "integer"}}, ["limit"]),
    "rebook_options": ("k fastest feasible re-routings for a booking (k-shortest paths).",
                       {"booking_id": {"type": "string"}, "k": {"type": "integer"}}, ["booking_id", "k"]),
    "rescue_capacity": ("Most passengers the rest of the network can still move from one airport to another in the "
                        "next hours (max flow over free seats on flights still to depart; an upper bound, connection "
                        "times not checked), and whether seats out, seats in or the routes between are the limit.",
                        {"origin": {"type": "string"}, "dest": {"type": "string"}, "hours": {"type": "integer"}},
                        ["origin", "dest"]),
    "live_conditions": ("Airports in the network constrained RIGHT NOW (FAA ground stops/delay programs/closures, "
                        "IFR weather from METAR, and TAF forecasts of IFR, thunderstorms or freezing precipitation in the "
                        "next 12h), with routes and daily flights through each. Pass an airport code for that "
                        "airport's current METAR even when it is not constrained.",
                        {"airport": {"type": "string"}}, []),
    "recall": ("Earlier decisions and lessons about a flight, crew, airport or booking.",
               {"about": {"type": "string"}}, ["about"]),
}
