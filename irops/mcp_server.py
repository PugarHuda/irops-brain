"""The same read-only tool catalogue as an MCP server, so any MCP client (Claude Code,
Claude Desktop, an IDE agent) can reason over the IROPS graph with the human-written
Cypher, the id resolution and the paths-as-explanations the in-app agents use.

    uv run python -m irops.mcp_server                       # stdio
    claude mcp add --transport http irops https://<host>/mcp/   # streamable HTTP, mounted by irops.api

Every tool takes `airline`: one FalkorDB graph per airline (WN = Southwest, AS = Alaska).
Writes are not exposed; changing the operation needs a human approval in the app.
"""
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from irops.tools import Tools

AIRLINES = {"WN": "irops_WN", "AS": "irops_AS"}
RO = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)

mcp = MCPServer("IROPS Brain", instructions=(
    "Airline irregular-operations graph (FalkorDB) for Southwest (WN) and Alaska (AS), 21-23 Dec 2022, real US DOT "
    "flights plus synthetic crews and bookings. Times are minutes since 2022-12-21 00:00 UTC. Outcomes: answered, "
    "from_memory, not_in_graph (no record, which is not 'nothing affected'), unknown_entity. Quote ids exactly."))


def _t(airline):
    if airline.upper() not in AIRLINES:
        raise ToolError(f"airline must be one of {sorted(AIRLINES)}")
    return Tools(AIRLINES[airline.upper()])


def _out(res):
    res = dict(res)
    res["paths"] = res.get("paths", [])[:15]
    res["rows"] = res.get("rows", [])[:50]
    return res


@mcp.tool(annotations=RO)
def resolve_flight(airline: str, ref: str) -> dict:
    """Find a flight by id or flight number, e.g. 'WN2262 2022-12-22 DEN'."""
    return _out(_t(airline).resolve_flight(ref))


@mcp.tool(annotations=RO)
def resolve_airport(airline: str, name: str) -> dict:
    """Airport code from a code, airport name or city ('Midway', 'Seattle'), via the full-text index."""
    return _out(_t(airline).resolve_airport(name))


@mcp.tool(annotations=RO)
def propagate_delay(airline: str, flight: str, delay_minutes: int, max_hops: int = 8) -> dict:
    """What breaks if this flight is late: downstream flights through aircraft rotations and crew connections
    (slack consumed per hop), passengers who misconnect, crews pushed past 14 CFR 117.13. Paths explain each row."""
    return _out(_t(airline).propagate_delay([{"flight": flight, "delay": delay_minutes}], max_hops))


@mcp.tool(annotations=RO)
def crew_duty(airline: str, crew_id: str) -> dict:
    """A crew's legs with projected flight-duty-period use against its legal limit."""
    return _out(_t(airline).crew_duty(crew_id))


@mcp.tool(annotations=RO)
def crews_over_limit(airline: str, airport: str = "") -> dict:
    """Crews the delays now in the graph push past 14 CFR 117.13, optionally only on legs departing one airport."""
    return _out(_t(airline).crews_over_limit(airport or None))


@mcp.tool(annotations=RO)
def find_reserve(airport: str, airline: str, minutes_needed: int = 300) -> dict:
    """Free reserve crews at an airport, else the nearest base with one, routed by A* over the route network."""
    return _out(_t(airline).find_reserve(airport, minutes_needed=minutes_needed))


@mcp.tool(annotations=RO)
def critical_flights(airline: str, date: str, top: int = 10) -> dict:
    """Most load-bearing remaining flights of a day (YYYY-MM-DD), by betweenness centrality."""
    return _out(_t(airline).critical_flights(date, top))


@mcp.tool(annotations=RO)
def network_health(airline: str) -> dict:
    """Scheduled, cancelled and delayed counts, and airports cut off from the network (weakly connected components)."""
    return _out(_t(airline).network_health())


@mcp.tool(annotations=RO)
def rebook_options(airline: str, booking_id: str, k: int = 3) -> dict:
    """The k fastest feasible re-routings for a booking (k-shortest paths; seats, cancellations, delays checked)."""
    return _out(_t(airline).rebook_options(booking_id, k))


@mcp.tool(annotations=RO)
def disrupted_bookings(airline: str, limit: int = 20) -> dict:
    """Bookings broken by a cancelled leg or a connection the projected delays make impossible."""
    return _out(_t(airline).disrupted_bookings(limit))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True, openWorldHint=True))
def live_conditions(airline: str, airport: str = "") -> dict:
    """Airports in the airline's network constrained RIGHT NOW: FAA ground stops, ground delay programs, delays and
    closures, plus IFR/LIFR weather from METAR, with routes and daily flights through each. Refreshes the graph's
    live layer from the FAA and aviationweather.gov at most every 5 minutes. With `airport`, that airport's current
    METAR and FAA entries even when it is not constrained."""
    from irops import live
    t = _t(airline)
    feed = live.refresh(t)
    return {"feed": feed, **_out(t.live_conditions(airport or None))}


@mcp.tool(annotations=RO)
def recall(airline: str, about: str) -> dict:
    """Earlier agent decisions and lessons about a flight, crew, booking or airport (agent memory in the graph)."""
    return _out(_t(airline).recall(about))


@mcp.tool(annotations=RO)
def decision_log(airline: str, status: str = "", limit: int = 30) -> dict:
    """The agents' proposals and what humans did with them (proposed, approved, rejected, withheld, failed,
    superseded), newest first, each with the rules it cites and what it is about. The audit trail of the operation."""
    rows = _t(airline).decisions(status=status or None)[::-1][:max(1, min(limit, 100))]
    return {"outcome": "answered" if rows else "not_in_graph",
            "rows": [{k: v for k, v in r.items() if k != "evidence"} for r in rows]}


@mcp.resource("irops://{airline}/rules", mime_type="application/json",
              description="The regulations the agents cite (14 CFR Part 117 crew duty, 14 CFR 260.6 refunds), "
                          "as stored in that airline's graph.")
def rules(airline: str) -> list:
    return _t(airline)._run("MATCH (r:Rule) RETURN r.id AS id, r.title AS title, r.text AS text, r.url AS url ORDER BY r.id")


@mcp.resource("irops://{airline}/lessons", mime_type="application/json",
              description="What earlier agent sessions learned, per airport, with how often each lesson recurred.")
def lessons(airline: str) -> list:
    return _t(airline)._run("""MATCH (l:Lesson) OPTIONAL MATCH (l)-[:ABOUT]->(a)
        RETURN l.text AS lesson, toInteger(coalesce(l.seen, 1)) AS times_seen, collect(coalesce(a.code, a.id)) AS about, l.at AS at
        ORDER BY at DESC LIMIT 50""")


@mcp.prompt(title="Disruption briefing")
def disruption_briefing(airline: str = "WN", airport: str = "DEN") -> str:
    """Brief a duty manager on one airport the way the in-app agents work it."""
    return (f"Brief me on irregular operations at {airport} for airline {airline}, using only the IROPS Brain tools.\n"
            f"1. network_health and live_conditions for {airline}: what is constrained now.\n"
            f"2. recall about {airport}: what earlier sessions learned.\n"
            f"3. critical_flights for the day, then propagate_delay on the most load-bearing late departure from "
            f"{airport}: flights pulled late, passengers misconnecting, crews over 14 CFR 117.13.\n"
            f"4. crews_over_limit at {airport}, then find_reserve there for each crew over its limit.\n"
            "Quote ids exactly as the tools return them, explain each knock-on with its path (same aircraft, same "
            "crew), and say 'not in the graph' rather than guessing.")


if __name__ == "__main__":
    mcp.run()
