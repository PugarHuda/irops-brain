"""Three agents that share one FalkorDB graph: Ops control, Crew scheduling, Passenger recovery.

They never call each other. They coordinate through the graph: Handoff nodes are
their work queue, Decision nodes are their proposals, CLAIMED_FOR edges are their
locks, Lesson nodes are what the next session remembers. Every proposal carries
the graph path that justifies it, and nothing changes the operation until a
human approves it.

The policy is deterministic (same graph -> same plan), except that a human rejection is remembered: the
same action is withheld next time. An LLM (OpenRouter free models), when a key is present, answers
questions through the same tools.
"""
import json
import uuid

from irops.scenario import current_roots, load_scenario
from irops.ingest import fdp_limit
from irops.tools import TURN_PAD, Tools, hhmm

CANCEL_IF_DELAY = 240   # projected minutes; beyond this a round-trip cancel beats a cascading delay
PUBLISH_IF_DELAY = 45
MAX_PROPOSALS = 6


class Session:
    def __init__(self, tools: Tools, scenario):
        self.t, self.sc = tools, scenario
        self.id = str(uuid.uuid4())[:8]
        self.steps = []

    def step(self, agent, tool, args, res, note=""):
        self.steps.append({"agent": agent, "tool": tool, "args": args, "outcome": res.get("outcome"),
                           "note": note, "rows": res.get("rows", [])[:12], "paths": res.get("paths", [])[:12],
                           "cypher": res.get("cypher", []), "extra": {k: v for k, v in res.items() if k not in
                                                                      ("rows", "paths", "cypher", "outcome")}})
        return res

    def call(self, who, tool, note="", **args):
        return self.step(who, tool, args, getattr(self.t, tool)(**args), note)


def ops_agent(s: Session):
    t, a = s.t, "ops"
    memory = [s.call(a, "recall", "what happened last time here?", about=ap) for ap in s.sc["airports"]]
    lessons = [l for m in memory for l in m.get("lessons", [])]
    s.call(a, "network_health", "state of the network right now")
    roots = current_roots(t)
    prop = s.call(a, "propagate_delay", f"{len(roots)} flights already delayed; project the ripple",
                  roots=roots, max_hops=8)
    crit = s.call(a, "critical_flights", "which remaining flights carry the most connections?",
                  date=s.sc["date"], top=10)
    critical = {r["id"] for r in crit["rows"]}
    down = sorted(prop["rows"], key=lambda r: -r["delay"])
    path_of = {r["id"]: p for r, p in zip(prop["rows"], prop["paths"])}
    breached = {b["flight"] for b in prop.get("crew_fdp_breaches", [])}

    cancel = []
    for r in sorted((r for r in down if r["delay"] >= CANCEL_IF_DELAY), key=lambda r: r["dep"]):
        upstream = {n["key"] for n in path_of[r["id"]]["nodes"]}
        if upstream & {c["id"] for c in cancel}:
            continue  # an earlier cancel on the same chain already contains this delay
        cancel.append(r)
    for r in sorted(cancel, key=lambda r: -r["delay"])[:MAX_PROPOSALS]:
        why = (f"{r['fno']} {r['origin']}->{r['dest']} projected {r['delay']} min late "
               f"({r['pax']} pax){', crew over duty limit' if r['id'] in breached else ''}"
               f"{', load-bearing flight' if r['id'] in critical else ''}. "
               "Cancel it and the aircraft's return leg so the tail resets instead of dragging the delay all day.")
        t.remember(a, "cancel_round_trip", why, [r["id"], r["origin"]], ["14CFR117.13"] if r["id"] in breached else [],
                   evidence=path_of[r["id"]], session=s.id, action={"op": "cancel_with_return", "flight": r["id"]})
    publish = [{"flight": r["id"], "delay": r["delay"]} for r in down if PUBLISH_IF_DELAY <= r["delay"] < CANCEL_IF_DELAY]
    if publish:
        t.remember(a, "publish_delays", f"Publish revised departure times for {len(publish)} flights reached by the ripple.",
                   [p["flight"] for p in publish], session=s.id, action={"op": "publish_delays", "delays": publish})
    # hand work to the other agents through the graph: one handoff per crew, at the first leg it times out on
    cancelled = {r["id"] for r in cancel}
    first_breach = {}
    for b in prop.get("crew_fdp_breaches", []):  # already ordered by departure
        if b["flight"] not in cancelled:
            first_breach.setdefault(b["crew"], b)
    for b in first_breach.values():
        t.handoff(a, "crew", "fdp_breach", [b["flight"]], json.dumps(b), s.id)
    if prop.get("misconnects"):
        t.handoff(a, "pax", "misconnects", [m["outbound"] for m in prop["misconnects"][:20]],
                  json.dumps(prop["misconnects"][:20]), s.id)
    s.delays = {**{r["flight"]: r["delay"] for r in roots}, **{r["id"]: r["delay"] for r in prop["rows"]}}
    return {"lessons": lessons, "summary": prop.get("summary", {})}


def legal_prefix(t, legs, delays, start):
    """How many of `legs` (in order) a crew starting duty at `start` can fly inside its 14 CFR 117.13 limit."""
    if not legs:
        return 0
    hour = int(hhmm(start, t._tz(legs[0]["origin"]))[-5:-3])
    n = 0
    for i, leg in enumerate(legs):
        if leg["arr"] + delays.get(leg["flight"], leg["delay"] or 0) + TURN_PAD - start > fdp_limit(hour, i + 1):
            break
        n = i + 1
    return n


def crew_agent(s: Session):
    t, a = s.t, "crew"
    work = s.call(a, "inbox", "work handed over by ops", agent=a, session=s.id)
    used = 0
    for h in work["rows"]:
        b = json.loads(h["payload"])
        duty = s.call(a, "crew_duty", f"check {b['crew']}'s duty day", crew_id=b["crew"])
        at = next((r for r in duty["rows"] if r["flight"] == b["flight"]), None)
        # the rest of this duty period: the legs that share its report time, from the one it times out on
        legs = [r for r in duty["rows"] if at and r["duty_start"] == at["duty_start"] and r["dep"] >= at["dep"]
                and r["status"] != "cancelled"]
        need = max(300, max((r["arr"] + s.delays.get(r["flight"], 0) for r in legs), default=0) + TURN_PAD - t.now())
        opts = s.call(a, "find_reserve", f"{b['crew']} times out at {b['at']}; who can cover its remaining {len(legs)} leg(s)?",
                      airport=b["at"], minutes_needed=need)
        won = None
        for i, o in enumerate(opts["rows"]):
            n = legal_prefix(t, legs, s.delays, t.now())
            if not n:
                continue
            c = s.call(a, "claim_reserve", "lock the reserve before anyone else takes it",
                       agent=a, reserve_id=o["reserve"], flight_ref=b["flight"])
            if c["outcome"] == "answered":
                won = (o, opts["paths"][i] if i < len(opts["paths"]) else None, n)
                break
        if won:
            o, path, n = won
            used += 1
            cover = [x["flight"] for x in legs[:n]]
            how = "on call at the airport" if not o["deadhead_minutes"] else \
                f"deadheading from {o['base']} ({int(o['deadhead_minutes'])} min, A* route)"
            rest = legs[n:]
            t.remember(a, "assign_reserve",
                       f"Replace {b['crew']} ({b['over_by']} min over FDP) with reserve {o['reserve']}, {how}, for "
                       f"{n} leg(s) from {b['flight']}, " + ("the most it can fly legally." if rest else "the rest of the duty period.")
                       + (f" Its last {len(rest)} leg(s) from {rest[0]['flight']} still need a crew." if rest else ""),
                       [b["flight"], b["crew"]], ["14CFR117.13", "14CFR117.21"], evidence=path, session=s.id,
                       action={"op": "assign_reserve", "reserve": o["reserve"], "flight": b["flight"],
                               "replaces": b["crew"], "legs": cover})
            if rest:
                t.handoff(a, "ops", "no_legal_crew", [rest[0]["flight"]],
                          json.dumps({**b, "flight": rest[0]["flight"], "partial": o["reserve"]}), s.id)
        else:
            t.handoff(a, "ops", "no_legal_crew", [b["flight"]], json.dumps(b), s.id)
        t.close_handoff(h["handoff"])
    return used


def pax_agent(s: Session):
    t, a = s.t, "pax"
    work = s.call(a, "inbox", "misconnects handed over by ops", agent=a, session=s.id)
    for h in work["rows"]:
        t.close_handoff(h["handoff"])
    broken = s.call(a, "disrupted_bookings", "itineraries broken by cancellations or impossible connections", limit=12)
    done = 0
    for b in broken["rows"][:MAX_PROPOSALS]:
        opt = s.call(a, "rebook_options", f"{b['pax']} pax {b['origin']}->{b['dest']}: {b['reason']}",
                     booking_id=b["booking"], k=3)
        if opt["rows"]:
            best = opt["rows"][0]
            t.remember(a, "rebook", f"Rebook {b['booking']} ({b['pax']} pax {b['origin']}->{b['dest']}) via "
                       f"{' + '.join(best['flights'])}, arriving {best['arrives_local']} ({best['late_by']} min late).",
                       [b["booking"]] + best["flights"], evidence=opt["paths"][0], session=s.id,
                       action={"op": "apply_rebook", "booking": b["booking"], "flights": best["flights"]})
        else:
            t.remember(a, "refund", f"No feasible re-routing for {b['booking']} ({b['pax']} pax {b['origin']}->{b['dest']}, "
                       f"{b['reason']}) today. Refund it, as 14 CFR 260.6 requires, and put its seats back on sale.",
                       [b["booking"]], ["DOT-REFUND-2024"], session=s.id, action={"op": "refund", "booking": b["booking"]})
        done += 1
    return done


def ops_followup(s: Session):
    t, a = s.t, "ops"
    work = s.call(a, "inbox", "flights crew scheduling could not cover", agent=a, session=s.id)
    for h in work["rows"]:
        b = json.loads(h["payload"])
        why = f"reserve {b['partial']} can only cover the legs before it legally" if b.get("partial") else "no reserve reachable"
        t.remember(a, "cancel_round_trip", f"No legal crew for {b['flight']} ({b['crew']} over FDP by {b['over_by']} min, "
                   f"{why}). Cancel round trip.", [b["flight"]], ["14CFR117.13"], session=s.id,
                   action={"op": "cancel_with_return", "flight": b["flight"]})
        t.close_handoff(h["handoff"])


def run(scenario="den-elliott", tenant=None):
    from irops.scenario import SCENARIOS
    t = Tools(tenant or SCENARIOS[scenario]["tenant"])
    sc = load_scenario(scenario, t)
    s = Session(t, sc)
    ops = ops_agent(s)
    used = crew_agent(s)
    pax_agent(s)
    ops_followup(s)
    summ = ops["summary"]
    for ap in sc["airports"]:
        t.learn(f"{sc['date']} {ap} at {hhmm(sc['clock'])} UTC: {summ.get('roots', 0)} delayed flights rippled into "
                f"{summ.get('downstream_flights', 0)} more, {summ.get('crews_illegal', 0)} crews over duty limit, "
                f"{used} reserves claimed." + (" Pre-position reserves from the nearest A* bases early next time." if used else ""),
                [ap], s.id)
    proposals = t.decisions(session=s.id)
    briefing = brief(sc, summ, proposals, ops["lessons"])
    return {"session": s.id, "tenant": t.tenant, "scenario": {k: v for k, v in sc.items() if k not in ("roots", "cancelled")},
            "known": {"delayed": len(sc["roots"]), "cancelled": len(sc["cancelled"])},
            "summary": summ, "lessons_recalled": ops["lessons"], "steps": s.steps, "proposals": proposals,
            "briefing": briefing}


# -- human approval: the only path that changes the operation --------------
def approve(decision_id, tenant="irops_WN"):
    t = Tools(tenant)
    d = t.take_decision(decision_id)  # atomic: a double click or a second approver cannot run it twice
    if not d:
        return {"ok": False, "reason": not_open(t, decision_id)}
    act = json.loads(d["action"])
    op = act["op"]
    try:
        if op == "cancel_with_return":
            res = t.cancel_with_return(act["flight"], reason=d["summary"])
        elif op == "publish_delays":
            res = {"outcome": "answered", "rows": [t.set_delay(x["flight"], x["delay"])["rows"][0] for x in act["delays"]]}
        elif op == "assign_reserve":
            res = t.assign_reserve(act["reserve"], act["flight"], act["replaces"], act.get("legs"))
        elif op == "apply_rebook":
            res = t.apply_rebook(act["booking"], act["flights"])
        elif op == "refund":
            res = t.refund(act["booking"])
        else:
            res = {"outcome": "rejected", "rows": [], "reason": f"unknown op {op}"}
        check = t.read_back(op, act)
        # approved only if the write happened AND reading the graph back shows the intended state
        ok = res.get("outcome") == "answered" and check["outcome"] == "answered"
    except Exception as e:
        t.set_decision(decision_id, "failed", f"{type(e).__name__}: {e}"[:500])
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}
    t.set_decision(decision_id, "approved" if ok else "failed", json.dumps(res.get("rows", []), default=str)[:500])
    return {"ok": ok, "reason": None if ok else (res.get("reason") or check.get("reason") or "the graph refused this change"),
            "result": res, "verification": {"outcome": check["outcome"], "rows": check["rows"][:10],
                                            "cypher": check["cypher"], "reason": check.get("reason")}}


def not_open(t, decision_id):
    r = t._run("MATCH (d:Decision {id: $id}) RETURN d.status AS s", {"id": decision_id})
    s = r[0]["s"] if r else None
    return {None: "no such proposal in this airline's graph",
            "withheld": "withheld: a human already rejected this exact action; reset to propose it again",
            "superseded": "superseded by a newer run or a reset"}.get(s, f"already {s}" if s else "")


def reject(decision_id, tenant="irops_WN"):
    t = Tools(tenant)
    if not t.take_decision(decision_id):
        return {"ok": False, "reason": not_open(t, decision_id)}
    t.set_decision(decision_id, "rejected")
    return {"ok": True}


# -- briefing from the graph numbers ---------------------
def brief(sc, summ, proposals, lessons):
    # ponytail: built from the graph numbers, not by an LLM. An LLM draft cost 10-30 s inside the write lock and
    # most of the free OpenRouter quota, and the number check rejected half the drafts. The LLM answers questions.
    kinds = {}
    for p in proposals:
        if p["status"] == "proposed":
            kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
    lines = [f"{sc['title']}. {summ.get('roots', 0)} flights already late are projected to delay "
             f"{summ.get('downstream_flights', 0)} more ({summ.get('pax_delayed', 0):,} passengers), break "
             f"{int(summ.get('pax_misconnecting', 0)):,} passenger connections and push {summ.get('crews_illegal', 0)} "
             "crews past their legal duty limit. "
             f"{summ.get('pax_refund_eligible', 0):,} passengers are owed a refund if they ask (arriving 3h+ late or "
             "booked on a cancelled flight, 14 CFR 260.6)."]
    if kinds:
        lines.append("Proposed: " + ", ".join(f"{v} x {k.replace('_', ' ')}" for k, v in kinds.items()) +
                     ". Each proposal carries the graph path behind it and waits for approval.")
    held = sum(p["status"] == "withheld" for p in proposals)
    if held:
        lines.append(f"{held} action(s) a human already rejected were not proposed again.")
    if lessons:
        n = lessons[0].get("times_seen", 1)
        lines.append(f"Remembered from {'an earlier session' if n == 1 else f'{n} earlier sessions'}: " + lessons[0]["lesson"])
    return {"by": "template", "text": " ".join(lines)}


if __name__ == "__main__":
    import sys
    out = run(sys.argv[1] if len(sys.argv) > 1 else "den-elliott")
    print(json.dumps({k: out[k] for k in ("session", "known", "summary", "briefing")}, indent=1))
    for p in out["proposals"]:
        print(f"- [{p['agent']}] {p['kind']}: {p['summary']} {p['rules']}")
    print("steps:", [(st["agent"], st["tool"], st["outcome"]) for st in out["steps"]])
