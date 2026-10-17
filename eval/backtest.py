"""Does the graph predict the real knock-on delays?

US DOT records, per flight, how many minutes of delay came from the late arrival
of the same aircraft (LateAircraftDelay). We seed the graph only with each
flight's *own* cause delay (weather + air traffic + carrier) and let the
NEXT_LEG traversal propagate it. Then we compare the projected knock-on delay
with what DOT recorded, against a 1-hop baseline (previous leg only).

    uv run python -m eval.backtest          # both airline graphs: irops_WN, irops_AS

Then the agents' own decisions are scored against what the airline really did (decisions()).
"""
from irops.db import graph

PROPAGATE = """
MATCH (f:Flight) WHERE f.act_weather + f.act_nas + f.act_carrier >= 15 AND NOT f.act_cancelled
MATCH p = (f)-[:NEXT_LEG*1..{hops}]->(x:Flight)
WHERE NOT x.act_cancelled AND none(n IN nodes(p)[1..] WHERE n.act_cancelled)
WITH x, reduce(d = f.act_weather + f.act_nas + f.act_carrier, e IN relationships(p) |
     CASE WHEN d - e.slack > 0 THEN d - e.slack ELSE 0 END) AS d
WHERE d > 0
RETURN x.id AS id, max(d) AS predicted
"""


def score(hops, tenant="irops_WN"):
    g = graph(tenant)
    pred = {r[0]: r[1] for r in g.ro_query(PROPAGATE.format(hops=hops), timeout=120000).result_set}
    actual = {r[0]: r[1] for r in g.ro_query(
        "MATCH (f:Flight) WHERE NOT f.act_cancelled RETURN f.id, f.act_late_aircraft", timeout=60000).result_set}
    late = {k for k, v in actual.items() if v >= 15}
    hit = {k for k, v in pred.items() if v >= 15}
    tp = late & hit
    mae = sum(abs(pred[k] - actual[k]) for k in tp) / max(1, len(tp))
    return {"airline": tenant.rsplit("_", 1)[-1], "hops": hops, "real_knock_on_flights": len(late), "predicted": len(hit), "caught": len(tp),
            "recall": round(len(tp) / max(1, len(late)), 3), "precision": round(len(tp) / max(1, len(hit)), 3),
            "mae_minutes_on_caught": round(mae, 1)}


def decisions(scenario):
    """Score the agents' decisions against what the airline really did after the scenario clock.
    Cancels: did the airline also cancel the flight, or fly it 3h+ late? Significant change (14 CFR 260.6): of the
    flights the projection says will be 3h+ late, how many really arrived 3h+ late or were cancelled, compared with
    the base rate among all flights reachable from the known delays (aircraft and crew links, 8 hops)."""
    import json as _json
    from irops import agents
    from irops.scenario import current_roots
    from irops.tools import SIGNIFICANT_DELAY as SIG, Tools
    out = agents.run(scenario)
    t = Tools(out["tenant"])
    try:
        def bad(ids):  # really cancelled, or really arrived 3h+ late
            return {r["id"] for r in t._run("""UNWIND $ids AS i MATCH (f:Flight {id: i})
                WHERE f.act_cancelled OR f.act_arr_delay >= $sig RETURN f.id AS id""", {"ids": list(ids), "sig": SIG})}
        cancels = [_json.loads(p["action"])["flight"] for p in out["proposals"]
                   if p["kind"] == "cancel_round_trip" and p["action"]]
        cancelled = {r["id"] for r in t._run("UNWIND $ids AS i MATCH (f:Flight {id: i}) WHERE f.act_cancelled RETURN f.id AS id",
                                             {"ids": cancels})}
        roots = current_roots(t)
        predicted = {r["id"] for r in t.propagate_delay(roots, 8)["rows"] if r["delay"] >= SIG}
        reach = {r["id"] for r in t._run("""UNWIND $ids AS i MATCH (f:Flight {id: i})-[:NEXT_LEG|CREW_NEXT*1..8]->(x:Flight:Active)
            RETURN DISTINCT x.id AS id""", {"ids": [r["flight"] for r in roots]})}
        truth, base = bad(predicted), bad(reach)
        precision = len(truth) / len(predicted) if predicted else None  # no prediction is not a wrong one
        base_rate = len(base) / len(reach) if reach else None
        return {"scenario": scenario, "airline": out["tenant"].rsplit("_", 1)[-1],
                "cancels_proposed": len(cancels), "airline_also_cancelled": len(cancelled),
                "cancels_cancelled_or_3h_late": len(bad(cancels)),
                "projected_3h_late": len(predicted), "really_3h_late_or_cancelled": len(truth),
                "precision": precision and round(precision, 3), "reachable_flights": len(reach),
                "base_rate": base_rate and round(base_rate, 3),
                "lift": round(precision / base_rate, 2) if precision is not None and base_rate else None}
    finally:
        t.reset_ops()


if __name__ == "__main__":
    import json
    from pathlib import Path
    results = []
    for tenant in ("irops_WN", "irops_AS"):
        mine = [score(h, tenant) for h in (1, 4, 8)]
        for r in mine:
            print(r)
        # self-check: multi-hop traversal must catch strictly more real knock-on delays than 1 hop
        assert mine[2]["caught"] > mine[0]["caught"], mine
        results += mine
    from irops.scenario import SCENARIOS
    calls = [decisions(sc) for sc in SCENARIOS]
    for c in calls:
        print(c)
    Path(__file__).with_name("results.json").write_text(json.dumps({"results": results, "decisions": calls}, indent=1))
