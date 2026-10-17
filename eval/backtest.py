"""Does the graph predict the real knock-on delays?

US DOT records, per flight, how many minutes of delay came from the late arrival
of the same aircraft (LateAircraftDelay). We seed the graph only with each
flight's *own* cause delay (weather + air traffic + carrier) and let the
NEXT_LEG traversal propagate it. Then we compare the projected knock-on delay
with what DOT recorded, against a 1-hop baseline (previous leg only).

    uv run python -m eval.backtest          # both airline graphs: irops_WN, irops_AS
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
    Path(__file__).with_name("results.json").write_text(json.dumps({"results": results}, indent=1))
