"""Replay a real disruption: what the airline knew at a given moment.

Only events that had already happened by the clock are written into the graph
(own-cause departure delays and weather cancellations). Everything after the
clock stays hidden ground truth.
"""
from irops.tools import Tools

SCENARIOS = {
    "den-elliott": {
        "title": "Winter Storm Elliott hits Denver",
        "story": "Thu 22 Dec 2022, 08:00 in Denver. -24°F, de-icing backlog. The first three hours of "
                 "departures are already late or cancelled. Southwest went on to cancel 1,026 flights that day.",
        "airports": ["DEN"], "tz": "America/Denver", "date": "2022-12-22", "from_min": 1440 + 12 * 60, "clock": 1440 + 15 * 60,
    },
    "mdw-elliott": {
        "title": "Chicago Midway freezes",
        "story": "Thu 22 Dec 2022, 09:00 in Chicago. Blizzard conditions at MDW, ground delays building.",
        "airports": ["MDW"], "tz": "America/Chicago", "date": "2022-12-22", "from_min": 1440 + 12 * 60, "clock": 1440 + 15 * 60,
    },
}


def load_scenario(name, tools: Tools):
    s = SCENARIOS[name]
    tools.reset_ops()
    tools.set_clock(s["clock"])
    g = tools._run
    # departures in the window, with what was known by the clock
    known = g("""MATCH (f:Flight) WHERE f.origin IN $aps AND f.dep >= $from AND f.dep < $clock
        RETURN f.id AS id, f.act_cancelled AS cancelled, f.act_cancel_code AS code,
               toInteger(f.act_weather + f.act_nas + f.act_carrier) AS own_delay""",
              {"aps": s["airports"], "from": s["from_min"], "clock": s["clock"]})
    roots, cancelled = [], []
    for f in known:
        if f["cancelled"] and f["code"] == "B":
            cancelled.append(f["id"])
        elif f["own_delay"] and f["own_delay"] >= 30:
            roots.append({"flight": f["id"], "delay": f["own_delay"]})
    g("""UNWIND $r AS r MATCH (f:Flight {id: r.flight}) SET f.delay = r.delay, f.status = 'delayed'""",
      {"r": roots}, write=True)
    g("""UNWIND $ids AS i MATCH (f:Flight {id: i}) REMOVE f:Active SET f.status = 'cancelled', f.cancel_reason = 'weather'""",
      {"ids": cancelled}, write=True)
    g("UNWIND $ids AS i MATCH (f:Flight {id: i})-[c:CONN]-() DELETE c", {"ids": cancelled}, write=True)
    return {"name": name, **s, "roots": roots, "cancelled": cancelled}


def current_roots(tools: Tools):
    """Flights the graph currently marks as delayed: the starting points for propagation."""
    return [{"flight": r["id"], "delay": r["delay"]} for r in
            tools._run("MATCH (f:Flight:Active {status: 'delayed'}) RETURN f.id AS id, f.delay AS delay")]
