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
def live_conditions(airline: str) -> dict:
    """Airports in the airline's network constrained RIGHT NOW: FAA ground stops, ground delay programs, delays and
    closures, plus IFR/LIFR weather from METAR, with routes and daily flights through each. Refreshes the graph's
    live layer from the FAA and aviationweather.gov at most every 5 minutes."""
    from irops import live
    t = _t(airline)
    feed = live.refresh(t)
    return {"feed": feed, **_out(t.live_conditions())}


@mcp.tool(annotations=RO)
def recall(airline: str, about: str) -> dict:
    """Earlier agent decisions and lessons about a flight, crew, booking or airport (agent memory in the graph)."""
    return _out(_t(airline).recall(about))


if __name__ == "__main__":
    mcp.run()
