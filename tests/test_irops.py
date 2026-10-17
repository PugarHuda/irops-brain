"""Runnable self-check against the loaded graph:   uv run python -m tests.test_irops
(needs FalkorDB up and `uv run python -m irops.ingest` done)."""
import json

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
    approvals_are_atomic_verified_and_reversible()
    other_airline_is_isolated()
    names_and_constraints()
    mcp_serves_the_catalogue()
    print("all checks passed")


def snapshot(t):
    return t._run("""MATCH (f:Flight) WITH sum(f.pax) AS pax, sum(CASE WHEN f:Active THEN 1 ELSE 0 END) AS active
        MATCH ()-[a:ASSIGNED]->() WITH pax, active, count(a) AS assigned
        MATCH ()-[s:SEGMENT]->() WITH pax, active, assigned, count(s) AS segs
        MATCH ()-[c:CONN]->() WITH pax, active, assigned, segs, count(c) AS conns
        MATCH ()-[x:CREW_NEXT]->() RETURN pax, active, assigned, segs, conns, count(x) AS crew_next""")[0]


def approvals_are_atomic_verified_and_reversible():
    from irops import agents
    t = Tools()
    t.reset_ops()
    before = snapshot(t)
    out = agents.run("den-elliott")
    by_kind = {}
    for p in out["proposals"]:
        if p["action"]:
            by_kind.setdefault(p["kind"], p)
    assert {"assign_reserve", "cancel_round_trip", "rebook", "publish_delays"} <= set(by_kind), set(by_kind)
    for kind in ("assign_reserve", "cancel_round_trip", "rebook", "publish_delays"):
        r = agents.approve(by_kind[kind]["id"])
        assert r["ok"] and r["verification"]["outcome"] == "answered", (kind, r)
    # a second approval of the same proposal (double click, second operator) does nothing
    again = agents.approve(by_kind["assign_reserve"]["id"])
    assert not again["ok"], again
    # an approved reserve swap is visible to the next propagation: the relieved crew no longer breaches
    a = json.loads(by_kind["assign_reserve"]["action"])
    duty = t.crew_duty(a["replaces"])["rows"]
    assert a["flight"] not in [d["flight"] for d in duty], "relieved crew still shows the leg"
    # a new run supersedes the old proposals: they cannot be approved against the new state
    out2 = agents.run("den-elliott")
    stale = next(p for p in out["proposals"] if p["action"] and p["id"] not in {by_kind[k]["id"] for k in by_kind})
    assert not agents.approve(stale["id"])["ok"]
    assert out2["summary"] == out["summary"], (out2["summary"], out["summary"])  # the rerun starts from the same state
    t.reset_ops()
    after = snapshot(t)
    assert after == before, (before, after)  # every approved change was undone, nothing deleted for good


def other_airline_is_isolated():
    from irops import agents
    wn_before = Tools("irops_WN")._run("MATCH (f:Flight) RETURN count(f) AS n")[0]["n"]
    out = agents.run("sea-ice")
    assert out["tenant"] == "irops_AS" and out["summary"]["roots"] > 0, out["summary"]
    assert all(s["outcome"] in ("answered", "from_memory", "not_in_graph") for s in out["steps"])
    a = Tools("irops_AS")
    assert a.resolve_flight("2022-12-22-WN2262-DEN")["outcome"] == "unknown_entity"  # Southwest is not in this graph
    assert a._run("MATCH (f:Flight) WHERE NOT f.fno STARTS WITH 'AS' RETURN count(f) AS n")[0]["n"] == 0
    assert Tools("irops_WN")._run("MATCH (f:Flight) RETURN count(f) AS n")[0]["n"] == wn_before
    assert route("Who can cover a crew at Seattle?", a)["calls"][0]["args"]["airport"] == "SEA"
    a.reset_ops()


def names_and_constraints():
    t = Tools()
    assert t.resolve_airport("Midway")["rows"][0]["code"] == "MDW"
    assert t.resolve_airport("denver")["rows"][0]["code"] == "DEN"
    assert t.resolve_airport("Atlantis")["outcome"] == "unknown_entity"
    assert route("Who can cover a crew at Denver?", t)["calls"][0]["args"]["airport"] == "DEN"
    try:  # the database itself refuses a duplicate identity
        t.g.query("CREATE (:Flight {id: '2022-12-22-WN2262-DEN'})")
        raise AssertionError("duplicate flight id accepted")
    except Exception as e:
        assert "AssertionError" not in type(e).__name__


def mcp_serves_the_catalogue():
    import anyio
    from mcp.client.client import Client
    from irops.mcp_server import mcp

    async def go():
        async with Client(mcp) as c:
            tools = {x.name: x for x in (await c.list_tools()).tools}
            assert {"propagate_delay", "find_reserve", "rebook_options", "recall"} <= set(tools)
            assert all(x.annotations.read_only_hint for x in tools.values())
            r = await c.call_tool("resolve_airport", {"airline": "WN", "name": "Midway"})
            assert "MDW" in r.content[0].text, r
            bad = await c.call_tool("network_health", {"airline": "ZZ"})
            assert bad.is_error
    anyio.run(go)


if __name__ == "__main__":
    main()
