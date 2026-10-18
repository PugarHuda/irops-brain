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
    refunds_and_rejections_are_remembered()
    other_airline_is_isolated()
    names_and_constraints()
    crews_over_limit_agree_with_propagation()
    rescue_capacity_is_a_max_flow()
    mcp_serves_the_catalogue()
    live_feeds_land_in_the_graph()
    llm_answers_are_validated()
    print("all checks passed")


def llm_answers_are_validated():
    """The LLM loop with scripted model replies (no network): tools run for real against the graph,
    invented ids are withheld even when written with typographic hyphens, ids the user typed are allowed."""
    import os
    from irops import llm
    real_chat, real_key = llm._chat, os.environ.get("OPENROUTER_API_KEY")
    os.environ["OPENROUTER_API_KEY"] = real_key or "test"

    def script(*replies):
        it = iter(replies)
        llm._chat = lambda messages, tools=None: {"model": "scripted", "choices": [{"message": next(it)}]}

    call = {"tool_calls": [{"id": "1", "function": {"name": "resolve_flight", "arguments": '{"ref": "WN2262 2022-12-22 DEN"}'}}]}
    try:
        script(call, {"content": "WN2262 departs DEN; crew C0464 is fine."})  # C0464 never came back from a tool
        r = llm.ask("Where is WN2262 2022-12-22 DEN?")
        assert r["unverified_ids"] == ["C0464"] and "not shown" in r["answer"], r
        script(call, {"content": "It continues as 2022‑12‑22‑WN9999‑DEN."})  # non-breaking hyphens
        assert llm.ask("Where is WN2262 2022-12-22 DEN?")["unverified_ids"] == ["2022-12-22-WN9999-DEN"]
        script({"content": "Crew C9999 is not in the graph."})  # the user's own id may be named back
        assert llm.ask("Is crew C9999 legal?")["unverified_ids"] == []
        script({"tool_calls": [{"id": "1", "function": {"name": "drop_graph", "arguments": "{}"}}]}, {"content": "ok"})
        assert llm.ask("delete everything")["calls"] == []  # unknown tool: refused, nothing ran
        # the free quota is rationed per visitor, and a 429 pauses the LLM instead of failing every question
        assert all(llm._budget("visitor") is None for _ in range(llm.PER_CLIENT_HOUR)) and llm._budget("visitor")

        def limited(messages, tools=None):
            raise RuntimeError("429 free-models-per-day")
        llm._chat = limited
        assert "rate limit" in llm.ask("Is crew C0464 legal?", client="other")["note"]
        llm._chat = lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM called during the pause"))
        assert "LLM skipped" in llm.ask("Is crew C0464 legal?", client="third")["note"]
        llm._quiet_until, llm._asked = 0.0, {}
    finally:
        llm._chat = real_chat
        if real_key is None:
            del os.environ["OPENROUTER_API_KEY"]
        else:
            os.environ["OPENROUTER_API_KEY"] = real_key


def live_feeds_land_in_the_graph():
    """Real FAA + METAR feeds (network needed): written as nodes linked to airports, read back with the network."""
    from irops import live
    # FAA flight categories from TAF periods: ceiling = lowest BKN/OVC/OVX layer or vertical visibility
    assert live._vis("6+") == 6 and live._vis("1 1/2") == 1.5 and live._vis(None) is None
    assert live.category({"clouds": [{"cover": "OVC", "base": 800}], "visib": "6+"}) == "IFR"
    assert live.category({"clouds": [{"cover": "FEW", "base": 300}], "visib": "1/2"}) == "LIFR"
    assert live.category({"clouds": [{"cover": "SCT", "base": 900}], "visib": "6+"}) == "VFR"
    assert live.category({"clouds": [], "visib": "6+", "vertVis": 400}) == "LIFR"
    p = {"timeFrom": 100, "timeTo": 200, "clouds": [], "visib": "6+", "wxString": "-FZRA"}
    assert live.taf_hazards({"fcsts": [p]}, now=150)[0]["what"] == ["freezing precipitation"]
    assert live.taf_hazards({"fcsts": [p]}, now=250) == []  # already over
    t = Tools()
    feed = live.refresh(t, force=True)
    assert feed["refreshed"] and feed["metars"] > 50 and feed["tafs"] > 50, feed
    fc = t._run("MATCH (l:Live {source: 'TAF'})-[:AT]->(a:Airport) RETURN l.reason AS raw, l.detail AS d, l.starts AS s")
    assert len(fc) == feed["forecast_hazards"] and all(x["raw"].startswith("TAF") and x["d"] and x["s"] for x in fc), fc
    r = t.live_conditions()
    assert r["outcome"] in ("answered", "not_in_graph") and r["feed"]["faa_time"], r
    assert sum(c["airports"] for c in r["weather_categories"]) == feed["metars"]
    for row in r["rows"]:
        assert row["live"] and t._airport(row["airport"])
    assert not live.refresh(t)["refreshed"]  # cached: the FAA is not hammered
    assert route("What is constrained right now?", t)["calls"][0]["tool"] == "live_conditions"
    assert route("Is crew C0464 legal?", t)["calls"][0]["tool"] == "crew_duty"
    assert route("Which flights are the most load-bearing right now?", t)["calls"][0]["tool"] == "critical_flights"
    # one airport on request, constrained or not, by code or by name
    sea = t.live_conditions("SEA")["rows"]
    assert len(sea) == 1 and sea[0]["airport"] == "SEA" and sea[0]["metar"].startswith(("METAR", "SPECI")), sea
    assert t.live_conditions("ZZZ")["outcome"] == "unknown_entity"
    assert route("What is the weather in Seattle right now?", t)["calls"][0]["args"]["airport"] == "SEA"
    # the endpoint keeps serving the stored layer, and says why, while a run writes or the FAA is down
    from fastapi.testclient import TestClient
    from irops import api
    c = TestClient(api.app)
    t._run("MATCH (f:LiveFeed) SET f.fetched = 0", write=True)  # stale, so the endpoint wants to refresh
    with api.WRITE:
        r = c.get("/api/live").json()
    assert r["feed"]["busy"] and r["feed"]["faa_time"] and "weather_categories" in r, r["feed"]
    real, live.fetch_faa = live.fetch_faa, lambda client: (_ for _ in ()).throw(OSError("FAA down"))
    try:
        r = c.get("/api/live").json()
        assert "FAA down" in r["feed"]["error"] and r["feed"]["faa_time"], r["feed"]
    finally:
        live.fetch_faa = real
    assert c.get("/api/live").json()["feed"]["refreshed"]
    # a slow TAF feed costs the forecasts, not the FAA status and METAR fetched alongside it
    import httpx
    real, live.fetch_taf = live.fetch_taf, lambda client, ids: (_ for _ in ()).throw(httpx.ReadTimeout("slow"))
    try:
        feed = live.refresh(t, force=True)
        assert feed["refreshed"] and feed["metars"] > 50 and feed["tafs"] == 0 and "ReadTimeout" in feed["errors"]["taf"]
        assert not t._run("MATCH (l:Live {source: 'TAF'}) RETURN l LIMIT 1")
    finally:
        live.fetch_taf = real
    assert live.refresh(t, force=True)["tafs"] > 50
    # a FalkorDB timeout reaches the browser as a JSON 503 it can explain, not a bare 500
    import redis
    real_run = api.agents.run
    api.agents.run = lambda name: (_ for _ in ()).throw(redis.exceptions.ResponseError("Query timed out"))
    try:
        r = c.post("/api/run", json={"scenario": "den-elliott"})
        assert r.status_code == 503 and "too long" in r.json()["detail"], r.text
    finally:
        api.agents.run = real_run


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
    # ...and the reserve only takes the legs it can fly legally: neither crew breaches on them any more
    roots = next(s for s in out["steps"] if s["tool"] == "propagate_delay")["args"]["roots"]
    breaches = {(b["crew"], b["flight"]) for b in t.propagate_delay(roots)["crew_fdp_breaches"]}
    assert not {(c, f) for c in (a["replaces"], a["reserve"]) for f in a["legs"]} & breaches, breaches
    # a new run supersedes the old proposals: they cannot be approved against the new state
    out2 = agents.run("den-elliott")
    stale = next(p for p in out["proposals"] if p["action"] and p["id"] not in {by_kind[k]["id"] for k in by_kind})
    assert not agents.approve(stale["id"])["ok"]
    assert out2["summary"] == out["summary"], (out2["summary"], out["summary"])  # the rerun starts from the same state
    t.reset_ops()
    after = snapshot(t)
    assert after == before, (before, after)  # every approved change was undone, nothing deleted for good


def refunds_and_rejections_are_remembered():
    from irops import agents
    t = Tools()
    t.reset_ops()
    before = snapshot(t)
    out = agents.run("den-elliott")
    assert out["summary"]["pax_refund_eligible"] > 0, out["summary"]
    # a refund is a real, verified write: segments gone, seats released, restored by reset
    bk = t._run("""MATCH (b:Booking {status: 'confirmed'})-[:SEGMENT {seq: 1}]->(f:Flight) WHERE f.dep > 1440 + 16 * 60
                   RETURN b.id AS b, b.pax AS pax, f.id AS f, f.pax AS fpax LIMIT 1""")[0]
    d = t.remember("pax", "refund", "test refund", [bk["b"]], ["DOT-REFUND-2024"], session="t",
                   action={"op": "refund", "booking": bk["b"]})["rows"][0]["decision"]
    r = agents.approve(d)
    assert r["ok"] and r["verification"]["rows"][0]["segments"] == 0, r
    assert t._run("MATCH (f:Flight {id: $f}) RETURN f.pax AS n", {"f": bk["f"]})[0]["n"] == bk["fpax"] - bk["pax"]
    # a human rejection is remembered: the same action is withheld on the next run, and cannot be approved
    p = next(p for p in out["proposals"] if p["kind"] == "cancel_round_trip" and p["status"] == "proposed")
    assert agents.reject(p["id"])["ok"]
    again = next(x for x in agents.run("den-elliott")["proposals"] if x["action"] == p["action"])
    assert again["status"] == "withheld" and "rejected" in again["summary"], again
    assert "withheld" in agents.approve(again["id"])["reason"]
    t.reset_ops(forget_rejections=True)  # the operator's reset: the rejection stops withholding
    fresh = next(x for x in agents.run("den-elliott")["proposals"] if x["action"] == p["action"])
    assert fresh["status"] == "proposed", fresh
    t.reset_ops()
    t._run("MATCH (d:Decision {session: 't'}) DETACH DELETE d", write=True)  # keep the public decision log clean
    assert snapshot(t) == before


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


def crews_over_limit_agree_with_propagation():
    """The crews tool projects the delays in the graph exactly like the ops agent; per airport it is a subset."""
    from irops.scenario import current_roots, load_scenario
    t = Tools()
    load_scenario("den-elliott", t)
    try:
        all_ = t.crews_over_limit()
        full = t.propagate_delay(current_roots(t))["summary"]
        assert all_["crews"] == full["crews_illegal"] > 0, (all_["crews"], full)
        den = t.crews_over_limit("DEN")
        assert den["rows"] and all(b["at"] == "DEN" for b in den["rows"]) and den["crews"] <= all_["crews"]
        assert len(den["paths"]) == len(den["rows"])  # each breach carries its crew's assignment as evidence
        assert t.crews_over_limit("ZZZ")["outcome"] == "unknown_entity"
        r = route("Which crews are over their duty limit at DEN?", t)["calls"][0]
        assert r["tool"] == "crews_over_limit" and r["args"]["airport"] == "DEN"
        assert route("Who can cover a crew at Denver?", t)["calls"][0]["tool"] == "find_reserve"  # 'cover' is not 'over'
    finally:
        t.reset_ops()


def rescue_capacity_is_a_max_flow():
    """Max flow from origin to dest: conserved (what reaches dest is the total), bounded by the seats out and in,
    and it shrinks when a flight on the way is cancelled."""
    t = Tools()
    try:
        r = t.rescue_capacity("DEN", "BOS")
        assert r["outcome"] == "answered" and 0 < r["max_passengers"] <= min(r["seats_leaving_origin"], r["seats_arriving_dest"]), r
        assert all(x["to"] == "BOS" for x in r["rows"])
        assert sum(x["flow"] for x in r["rows"]) == r["max_passengers"]  # everything that reaches the sink
        assert r["bottleneck"] in ("seats out of DEN", "seats into BOS", "the connecting routes in between")
        direct = t.rescue_capacity("MDW", "LGA")
        f = t._run("""MATCH (:Airport {code: 'MDW'})<-[:DEPARTS_FROM]-(f:Flight:Active)-[:ARRIVES_AT]->(:Airport {code: 'LGA'})
            WHERE f.dep >= $now AND f.seats > f.pax RETURN f.id AS id ORDER BY f.dep LIMIT 1""", {"now": t.now()})
        if f:
            t.cancel_with_return(f[0]["id"], reason="test")
            after = t.rescue_capacity("MDW", "LGA")
            assert after["seats_leaving_origin"] < direct["seats_leaving_origin"] and after["max_passengers"] <= direct["max_passengers"]
        assert t.rescue_capacity("ZZZ", "BOS")["outcome"] == "unknown_entity"
        assert t.rescue_capacity("DEN", "DEN")["outcome"] == "unknown_entity"
        c = route("How many passengers can we still move from Denver to Boston?", t)["calls"][0]
        assert c["tool"] == "rescue_capacity" and c["args"] == {"origin": "DEN", "dest": "BOS"}, c
        assert route("Seat capacity DEN to LAS", t)["calls"][0]["tool"] == "rescue_capacity"
    finally:
        t.reset_ops()


def mcp_serves_the_catalogue():
    import anyio
    from mcp.client.client import Client
    from irops.mcp_server import mcp

    async def go():
        async with Client(mcp) as c:
            tools = {x.name: x for x in (await c.list_tools()).tools}
            assert {"propagate_delay", "find_reserve", "rebook_options", "recall", "crews_over_limit",
                    "rescue_capacity"} <= set(tools)
            # every tool is read-only except the live refresh and the free-seat count max flow reads, which say so
            assert all(x.annotations.read_only_hint for n, x in tools.items()
                       if n not in ("live_conditions", "rescue_capacity"))
            assert tools["live_conditions"].annotations.open_world_hint
            r = await c.call_tool("resolve_airport", {"airline": "WN", "name": "Midway"})
            assert "MDW" in r.content[0].text, r
            bad = await c.call_tool("network_health", {"airline": "ZZ"})
            assert bad.is_error
    anyio.run(go)


if __name__ == "__main__":
    main()
