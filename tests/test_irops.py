"""Runnable self-check against the loaded graph:   uv run python -m tests.test_irops
(needs FalkorDB up and `uv run python -m irops.ingest` done)."""
from irops.llm import IDS, route
from irops.tools import Tools


def main():
    t = Tools()
    t.reset_ops()
    t.set_clock(1440 + 15 * 60)
    q = t._run

    # synthetic crews are legal by construction: no scheduled leg exceeds its FDP limit...
    assert q("""MATCH (c:Crew {reserve: false})-[a:ASSIGNED]->(f:Flight)
                WHERE f.arr + 15 - a.duty_start > a.fdp_limit RETURN count(*) AS n""")[0]["n"] == 0
    # ...and no crew has a 4-10h gap (neither same duty nor legal 10h rest, 14 CFR 117.25)
    duties = q("""MATCH (c:Crew {reserve: false})-[:ASSIGNED]->(f:Flight)
                  WITH c, f ORDER BY f.dep RETURN c.id AS c, collect([f.dep, f.arr]) AS legs""")
    bad = [d["c"] for d in duties for (_, arr), (dep, _) in zip(d["legs"], d["legs"][1:]) if 240 < dep - arr < 600]
    assert not bad, bad[:5]

    # propagation consumes slack hop by hop: the first aircraft leg after a root gets delay - slack
    leg = q("""MATCH (a:Flight)-[e:NEXT_LEG]->(b:Flight) WHERE e.slack = 20 AND NOT (a)-[:CREW_NEXT]->(b)
               AND NOT ()-[:CREW_NEXT]->(b) RETURN a.id AS a, b.id AS b LIMIT 1""")[0]
    r = t.propagate_delay([{"flight": leg["a"], "delay": 100}], 1)
    got = {x["id"]: x["delay"] for x in r["rows"]}
    assert got.get(leg["b"]) == 80, got
    assert r["paths"] and r["paths"][0]["edges"][0]["type"] in ("NEXT_LEG", "CREW_NEXT")
    # a delay smaller than the slack goes nowhere, and the tool says so
    assert t.propagate_delay([{"flight": leg["a"], "delay": 10}], 1)["outcome"] in ("not_in_graph", "answered")
    assert leg["b"] not in {x["id"] for x in t.propagate_delay([{"flight": leg["a"], "delay": 10}], 1)["rows"]}

    # names are resolved against the graph, never trusted
    assert t.resolve_flight("WN99999")["outcome"] == "unknown_entity"
    assert t.find_reserve("XXX", minutes_needed=60)["outcome"] == "unknown_entity"

    # two agents racing for one reserve: exactly one wins
    res = t.find_reserve("DEN", minutes_needed=300)["rows"][0]["reserve"]
    a = t.claim_reserve("ops", res, leg["b"])
    b = t.claim_reserve("crew", res, leg["b"])
    assert (a["outcome"], b["outcome"]) == ("answered", "rejected"), (a, b)

    # read-only path really is read-only
    try:
        t.g.ro_query("CREATE (:Hack)")
        raise AssertionError("ro_query accepted a write")
    except Exception as e:
        assert "AssertionError" not in type(e).__name__

    # rebooking never routes through a cancelled flight
    bk = q("""MATCH (b:Booking)-[:SEGMENT {seq: 1}]->(f:Flight) WHERE f.dep > 1440 + 16 * 60
              RETURN b.id AS b, f.id AS f LIMIT 1""")[0]
    t.cancel_with_return(bk["f"], reason="test")
    for opt in t.rebook_options(bk["b"], 3)["rows"]:
        assert bk["f"] not in opt["flights"]
        assert all(q("MATCH (f:Flight {id: $i}) RETURN f:Active AS a", {"i": i})[0]["a"] for i in opt["flights"])

    # the id pattern the answer validator relies on
    assert IDS.findall("2022-12-22-WN2262-DEN R-DEN-101 C0464 B09455") == \
        ["2022-12-22-WN2262-DEN", "R-DEN-101", "C0464", "B09455"]
    assert route("What breaks if WN2262 2022-12-22 DEN is 2h late?", t)["calls"][0]["tool"] == "propagate_delay"

    t.reset_ops()
    print("all checks passed")


if __name__ == "__main__":
    main()
