"""Three agents that share one FalkorDB graph: Ops control, Crew scheduling, Passenger recovery.

They never call each other. They coordinate through the graph: Handoff nodes are
their work queue, Decision nodes are their proposals, CLAIMED_FOR edges are their
locks, Lesson nodes are what the next session remembers. Every proposal carries
the graph path that justifies it, and nothing changes the operation until a
human approves it.

The policy is deterministic (same graph -> same plan). An LLM (OpenRouter free models), when a key
is present, writes the briefing and answers questions through the same tools.
"""
import json
import uuid

from irops.scenario import current_roots, load_scenario
from irops.tools import Tools, hhmm

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
    return {"lessons": lessons, "summary": prop.get("summary", {})}


def crew_agent(s: Session, lessons):
    t, a = s.t, "crew"
    work = s.call(a, "inbox", "work handed over by ops", agent=a, session=s.id)
    used = 0
    for h in work["rows"]:
        b = json.loads(h["payload"])
        s.call(a, "crew_duty", f"check {b['crew']}'s duty day", crew_id=b["crew"])
        opts = s.call(a, "find_reserve", f"{b['crew']} times out at {b['at']}; who can cover?",
                      airport=b["at"], minutes_needed=300)
        won = None
        for i, o in enumerate(opts["rows"]):
            c = s.call(a, "claim_reserve", "lock the reserve before anyone else takes it",
                       agent=a, reserve_id=o["reserve"], flight_ref=b["flight"])
            if c["outcome"] == "answered":
                won = (o, opts["paths"][i] if i < len(opts["paths"]) else None)
                break
        if won:
            o, path = won
            used += 1
            how = "on call at the airport" if not o["deadhead_minutes"] else \
                f"deadheading from {o['base']} ({int(o['deadhead_minutes'])} min, A* route)"
            t.remember(a, "assign_reserve",
                       f"Replace {b['crew']} on {b['flight']} ({b['over_by']} min over FDP) with reserve {o['reserve']}, {how}.",
                       [b["flight"], b["crew"]], ["14CFR117.13", "14CFR117.21"], evidence=path, session=s.id,
                       action={"op": "assign_reserve", "reserve": o["reserve"], "flight": b["flight"], "replaces": b["crew"]})
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
            t.remember(a, "refund", f"No feasible re-routing for {b['booking']} today; offer refund.",
                       [b["booking"]], ["DOT-REFUND-2024"], session=s.id, action=None)
        done += 1
    return done


def ops_followup(s: Session):
    t, a = s.t, "ops"
    work = s.call(a, "inbox", "flights crew scheduling could not cover", agent=a, session=s.id)
    for h in work["rows"]:
        b = json.loads(h["payload"])
        t.remember(a, "cancel_round_trip", f"No legal crew for {b['flight']} ({b['crew']} over FDP by {b['over_by']} min, "
                   "no reserve reachable). Cancel round trip.", [b["flight"]], ["14CFR117.13"], session=s.id,
                   action={"op": "cancel_with_return", "flight": b["flight"]})
        t.close_handoff(h["handoff"])


def run(scenario="den-elliott", tenant=None):
    from irops.scenario import SCENARIOS
    t = Tools(tenant or SCENARIOS[scenario]["tenant"])
    sc = load_scenario(scenario, t)
    s = Session(t, sc)
    ops = ops_agent(s)
    used = crew_agent(s, ops["lessons"])
    pax_agent(s)
    ops_followup(s)
    summ = ops["summary"]
    for ap in sc["airports"]:
        t.learn(f"{sc['date']} {ap} at {hhmm(sc['clock'])} UTC: {summ.get('roots', 0)} delayed flights rippled into "
                f"{summ.get('downstream_flights', 0)} more, {summ.get('crews_illegal', 0)} crews over duty limit, "
                f"{used} reserves claimed. Pre-position reserves from the nearest A* bases early next time.",
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
        return {"ok": False, "reason": "not an open proposal (already decided, or superseded by a newer run)"}
    act = json.loads(d["action"])
    op = act["op"]
    try:
        if op == "cancel_with_return":
            res = t.cancel_with_return(act["flight"], reason=d["summary"])
        elif op == "publish_delays":
            res = {"outcome": "answered", "rows": [t.set_delay(x["flight"], x["delay"])["rows"][0] for x in act["delays"]]}
        elif op == "assign_reserve":
            res = t.assign_reserve(act["reserve"], act["flight"], act["replaces"])
        elif op == "apply_rebook":
            res = t.apply_rebook(act["booking"], act["flights"])
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


def reject(decision_id, tenant="irops_WN"):
    t = Tools(tenant)
    if not t.take_decision(decision_id):
        return {"ok": False, "reason": "not an open proposal (already decided, or superseded by a newer run)"}
    t.set_decision(decision_id, "rejected")
    return {"ok": True}


# -- LLM: briefing and questions over the same tools ---------------------
def brief(sc, summ, proposals, lessons):
    facts = {"scenario": sc["title"], "story": sc["story"], "summary": summ,
             "proposals": [{k: p[k] for k in ("agent", "kind", "summary", "rules")} for p in proposals],
             "status": "every proposal is waiting for human approval; nothing has been executed yet",
             "lessons_from_earlier_sessions": [l["lesson"] for l in lessons]}  # text only: no raw minute counts
    from irops import llm
    out = llm.briefing(facts) if llm.available() else None
    if out:
        return {"by": out["model"] or "llm", "text": out["text"]}
    kinds = {}
    for p in proposals:
        kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
    lines = [f"{sc['title']}. {summ.get('roots', 0)} flights already late are projected to delay "
             f"{summ.get('downstream_flights', 0)} more ({summ.get('pax_delayed', 0):,} passengers), break "
             f"{int(summ.get('pax_misconnecting', 0)):,} passenger connections and push {summ.get('crews_illegal', 0)} "
             "crews past their legal duty limit."]
    if kinds:
        lines.append("Proposed: " + ", ".join(f"{v} x {k.replace('_', ' ')}" for k, v in kinds.items()) +
                     ". Each proposal carries the graph path behind it and waits for approval.")
    if lessons:
        lines.append("Remembered from an earlier session: " + lessons[0]["lesson"])
    return {"by": "template", "text": " ".join(lines)}


if __name__ == "__main__":
    import sys
    out = run(sys.argv[1] if len(sys.argv) > 1 else "den-elliott")
    print(json.dumps({k: out[k] for k in ("session", "known", "summary", "briefing")}, indent=1))
    for p in out["proposals"]:
        print(f"- [{p['agent']}] {p['kind']}: {p['summary']} {p['rules']}")
    print("steps:", [(st["agent"], st["tool"], st["outcome"]) for st in out["steps"]])
