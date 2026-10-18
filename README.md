# IROPS Brain

**Recovery agents for airline irregular operations, reasoning over a FalkorDB graph of real flights.**

In the week before Christmas 2022, Southwest Airlines cancelled nearly 17,000 flights. The US DOT later fined the
airline $140 million. The core failure was not the weather. It was that the airline's systems lost track of *how things
connect*: which crew was where, which aircraft fed which flight, which passengers were connecting. Crews waited on hold
for hours to tell scheduling where they were.

IROPS Brain replays that week from public US DOT data. The connections are the database: every flight, aircraft
rotation, crew pairing and passenger connection is a node or edge in FalkorDB. Three agents (Ops control, Crew
scheduling, Passenger recovery) work the storm from that graph. Every recommendation carries the graph path behind it,
cites the regulation it relies on, and waits for a human to approve it.

**Tracks:** 01 Agents That Act on Connected Data (primary) · 02 Agent Memory and Coordination · 03 Company Brain

- Demo video: _add link_
- Live: https://irops-brain.vercel.app (static UI on Vercel; `/api` and `/mcp` are proxied to the FastAPI + FalkorDB backend at https://irops-brain.onrender.com, which on the free tier takes ~1 min to wake after idle)

## What it does

1. **Replays what was known.** Pick a moment (Denver, Thu 22 Dec 2022, 08:00). Only the delays and cancellations that
   had already happened by then are written to the graph. Everything later stays hidden ground truth.
2. **Ops agent** projects the ripple through aircraft rotations and crew connections, consuming slack hop by hop,
   finds the load-bearing flights (betweenness centrality), and proposes round-trip cancellations where a cancel beats a
   cascading delay.
3. **Crew agent** takes every crew the ripple pushes past its legal duty limit (14 CFR 117.13), finds a reserve on call
   at that airport, or the nearest base with one using **A\*** over the route network, and **atomically claims** it.
   A crew that times out cannot fly the rest of its duty period either, so the reserve takes over those legs too, but
   only as many as its own FDP limit allows; the first leg nobody can fly legally goes back to Ops as a cancel proposal.
4. **Passenger agent** finds broken itineraries and computes **k-shortest feasible re-routings** (seats, cancellations
   and projected delays checked inside the query). A booking with no feasible re-routing gets an executable
   **refund** proposal (14 CFR 260.6); approving it releases the seats. The impact panel counts the passengers owed a
   refund if they ask: last leg projected 3h+ late, or a leg cancelled.
5. **You approve or reject** each proposal. Approval is atomic (a double click or a second operator cannot run it
   twice), writes to the graph, then reads the graph back and is only stamped APPROVED if the read-back shows the
   intended state. The impact panel is then re-projected on the changed graph and shows what your approvals moved
   (one fewer crew over its limit, fewer knock-on delays, more refunds owed after a cancel). "Undo all approvals"
   reverts every change (reserve swaps, cancellations, rebookings, refunds) exactly.
6. **Next session remembers.** Decisions and lessons are nodes. Run the scenario again and the agents recall what
   happened last time at that airport (a lesson learned again is counted, not duplicated). A **rejection is
   remembered**: the next run stores the identical action as WITHHELD, citing the rejected decision, until the
   operator resets. Proposals from an older run are superseded and can no longer be approved.
7. **A second airline, a second graph.** Alaska Airlines' Seattle ice storm (22 Dec 2022) runs on its own graph,
   `irops_AS`, with the same agents. Tenants are isolated by FalkorDB's multi-graph, not by a filter.
8. **Live now.** The current FAA airport status (ground stops, ground delay programs, delays, closures), METAR
   weather and the TAF forecast for every airport in the network (the next 12 hours, flagged when a period is IFR or
   LIFR by the FAA ceiling/visibility definition, or forecasts thunderstorms or freezing precipitation, the de-icing
   trigger behind Elliott) are written into each airline's graph as `(:Live)-[:AT]->(:Airport)`
   nodes (refreshed at most every 5 minutes), then matched against the route network: how many routes and daily
   flights touch each constrained airport, and which spoke airports depend on it.
9. **Bring your own agent (MCP).** The read-only tool catalogue is also served as an MCP server at `/mcp/`
   (streamable HTTP) and over stdio: `claude mcp add --transport http irops-brain https://irops-brain.vercel.app/mcp/`.
   Besides the tools (including `crews_over_limit`, which projects the delays now in the graph onto every crew's
   duty limit, per airport, and `live_conditions` for any one airport's current METAR) it serves a `decision_log` tool (every proposal and what a human did with it), the resources
   `irops://{airline}/rules` and `irops://{airline}/lessons`, and a `disruption_briefing` prompt that walks an outside
   agent through the same steps the in-app agents take.

## Does it predict what really happened?

US DOT records, for every flight, how many minutes of delay came from the aircraft arriving late (`LateAircraftDelay`).
We seed the graph only with each flight's *own* weather/air-traffic/carrier delay and let the `NEXT_LEG` traversal
propagate it. `uv run python -m eval.backtest` on 21-23 Dec 2022 (11,754 flights):

| Airline | Traversal | Real knock-on delays | Caught | Recall | Precision | Mean error |
|---|---|---|---|---|---|---|
| Southwest | 1 hop (previous leg only) | 3,087 | 1,211 | 39% | 95% | 33.7 min |
| Southwest | **8 hops** | 3,087 | **1,505** | **49%** | **95%** | **30.2 min** |
| Alaska | 1 hop (previous leg only) | 314 | 96 | 31% | 85% | 23.9 min |
| Alaska | **8 hops** | 314 | **119** | **38%** | **87%** | 26.6 min |

On both airlines, multi-hop traversal catches 24% more real knock-on delays than looking at the previous leg, at the
same or better precision.
Recall is capped mainly by delay chains that started before 21 Dec (outside the loaded window) and flights with no tail
number in the DOT file.

### The agents' own calls, against what the airline did

The same script runs each scenario as the agents would at its clock and scores their output against what happened
after it:

| Scenario | Cancels proposed | Airline also cancelled | Cancelled or 3h+ late | Projected 3h+ late | Really 3h+ late or cancelled | Base rate downstream |
|---|---|---|---|---|---|---|
| Denver (WN) | 11 | 5 | 7 | 24 | 13 (54%) | 52% of 105 |
| Midway (WN) | 0 | 0 | 0 | 0 | 0 | 83% of 6 |
| Seattle (AS) | 2 | 0 | 0 | 0 | 0 | 52% of 97 |

The honest reading: the knock-on model is accurate about delay that is already in the system (the table above), but at
08:00 in Denver it cannot see the storm's *new* delays still to come. Its 3h+ calls are right 54% of the time against
a 52% base rate downstream: no better than "everything downstream of a delayed hub goes bad". Its cancels do better:
7 of the 11 were cancelled by the airline too or flown 3h+ late. Seattle's two cancel
proposals come from crews with no legal reserve on hand, and Alaska flew both less than 3h late: the agents were more
conservative than the airline there.

## Graph data model

```
(:Airport {code, name, lat, lon, tz})
(:Flight:Active {id, fno, date, origin, dest, tail, dep, arr, block, seats, pax, delay, status})   // :Active removed on cancel
(:Aircraft {tail})        (:Crew {id, base, reserve, on_call_from, on_call_to})        (:Booking {id, pax, origin, dest})
(:Rule {id, title, text, url})                                                       // 14 CFR 117.13 / 117.21 / 117.25, DOT refunds
(:Agent {name})  (:Decision {kind, summary, status, action, evidence, session})  (:Handoff {from, to, kind, status})  (:Lesson {text})

(Flight)-[:DEPARTS_FROM]->(Airport)         (Flight)-[:ARRIVES_AT {w: 0}]->(Airport)
(Aircraft)-[:OPERATES]->(Flight)            (Flight)-[:NEXT_LEG {turn, slack, tail}]->(Flight)     // aircraft rotation
(Crew)-[:ASSIGNED {duty_start, fdp_limit}]->(Flight)  (Flight)-[:CREW_NEXT {crew, slack}]->(Flight) // crew connection
(Booking)-[:SEGMENT {seq}]->(Flight)        (Flight)-[:PAX_CONNECT {pax, slack}]->(Flight)          // passenger connection
(Flight)-[:CONN {w}]->(Flight)              // time-feasible connection, for re-routing
(Airport)-[:ROUTE {block, daily}]->(Airport)  (Crew)-[:BASED_AT]->(Airport)
(Agent)-[:DECIDED]->(Decision)-[:ABOUT]->(Flight|Crew|Booking|Airport)   (Decision)-[:CITES]->(Rule)
(Crew)-[:CLAIMED_FOR {by}]->(Flight)        (Handoff)-[:ABOUT]->(Flight)   (Lesson)-[:ABOUT]->(Airport)
```

Southwest: about 47,000 nodes and 410,000 relationships (~14 s to ingest). Alaska: about 6,000 nodes and 51,000
relationships. Unique constraints on Flight/Crew/Booking ids and Airport codes; a full-text index on airport names
resolves "Midway" or "Seattle" to a code. Times are minutes since
2022-12-21 00:00 UTC, converted from local schedule times with each airport's time zone.

**Real vs synthetic.** Flights, tails, schedules and outcomes are real public data (US DOT BTS On-Time Performance,
December 2022). Airport coordinates and time zones come from OpenFlights. Cockpit crews, reserves and connecting
bookings are **synthetic**, generated deterministically (seed 22) on top of the real schedule. Crews are legal by
construction (FDP and 10h rest are checked by `tests/test_irops.py`). No private data is used.

## The queries the product depends on

All Cypher is written by humans in `irops/tools.py`. Agents pick a tool and fill in arguments; every argument is
resolved against the graph first. Reads go through `GRAPH.RO_QUERY` with a timeout.

**Delay propagation** (multi-hop, slack consumed per hop, path returned as the explanation):
```cypher
UNWIND $roots AS r
MATCH (f:Flight {id: r.id})
MATCH p = (f)-[:NEXT_LEG|CREW_NEXT*1..8]->(x:Flight:Active)
WITH x, p, reduce(d = r.delay, e IN relationships(p) |
     CASE WHEN d - e.slack > 0 THEN d - e.slack ELSE 0 END) AS d
WHERE d > 0
WITH x, d, p ORDER BY d DESC
WITH x, collect(p)[0] AS p, max(d) AS d
RETURN x.id, d AS delay, p AS path
```

**Crews pushed past their flight-duty-period limit** (cites the rule node):
```cypher
UNWIND $imp AS i
MATCH (c:Crew)-[a:ASSIGNED]->(f:Flight:Active {id: i.id})
WITH c, f, f.arr + i.delay + 15 - a.duty_start - a.fdp_limit AS over WHERE over > 0
MATCH (r:Rule {id: '14CFR117.13'}) RETURN c.id, f.id, over, r.url
```

**Re-routing passengers: k-shortest paths** over the time-expanded connection network:
```cypher
MATCH (s:Flight:Active)-[:DEPARTS_FROM]->(:Airport {code: $at})
WHERE s.dep + s.delay >= $ready AND s.seats - s.pax >= $pax
WITH s ORDER BY CASE WHEN s.dest = $dest THEN 0 ELSE 1 END, s.dep LIMIT 16
MATCH (t:Airport {code: $dest})
CALL algo.SPpaths({sourceNode: s, targetNode: t, relTypes: ['CONN', 'ARRIVES_AT'],
                   weightProp: 'w', maxLen: 3, pathCount: 4}) YIELD path, pathWeight
WITH path, [n IN nodes(path) WHERE n:Flight] AS fl
WHERE all(n IN fl WHERE n:Active AND n.seats - n.pax >= $pax)   // cancelled flights keep their edges, the query skips them
  AND all(i IN range(0, size(fl) - 2) WHERE fl[i].arr + fl[i].delay + 35 <= fl[i+1].dep + fl[i+1].delay)
RETURN [n IN fl | n.id], fl[size(fl)-1].arr + fl[size(fl)-1].delay AS arrives ORDER BY arrives LIMIT $k
```

**Reserve crew deadhead: A\*** with the haversine heuristic scaled to minutes (900 km/h upper bound):
```cypher
CALL algo.AStar({sourceNode: b, targetNode: a, relTypes: ['ROUTE'], weightProp: 'block',
                 latitudeProperty: 'lat', longitudeProperty: 'lon', heuristicScale: 0.0000666})
YIELD path, pathWeight
```

**Load-bearing flights: betweenness centrality** on aircraft + crew + passenger connections:
```cypher
CALL algo.betweenness({nodeLabels: ['Active'], relationshipTypes: ['NEXT_LEG','CREW_NEXT','PAX_CONNECT'],
                       samplingSize: 64, samplingSeed: 7}) YIELD node, score
```

**Airports cut off by cancellations: weakly connected components** (cancelled flights lose `:Active`):
```cypher
CALL algo.WCC({nodeLabels: ['Airport', 'Active'], relationshipTypes: ['DEPARTS_FROM', 'ARRIVES_AT']})
YIELD node, componentId
```

**Two agents, one reserve: atomic claim.** FalkorDB runs each write query alone, so check-and-claim in one query
cannot be won twice:
```cypher
MATCH (r:Crew {id: $rid, reserve: true}), (f:Flight {id: $fid}), (a:Agent {name: $agent})
WHERE NOT (r)-[:CLAIMED_FOR]->()
CREATE (r)-[:CLAIMED_FOR {by: $agent, at: $at}]->(f), (a)-[:CLAIMED {at: $at}]->(r)
RETURN r.id
```

## How it is built

```
irops/ingest.py    BTS + OpenFlights -> graph; synthetic crews (FDP/rest-legal) and bookings
irops/tools.py     the fixed tool catalogue: Cypher, argument resolution, read-only guard, paths out
irops/agents.py    Ops / Crew / Pax agents coordinating through Handoff, Decision, CLAIMED_FOR, Lesson nodes
irops/scenario.py  replay "what was known at time T"
irops/llm.py       OpenRouter free models over the same tools + answer validator; intent router without a key
irops/mcp_server.py the read-only catalogue as an MCP server (stdio, and /mcp/ in the API)
irops/live.py      FAA NAS status + aviationweather METAR -> (:Live) nodes in each airline graph
irops/api.py       FastAPI;  web/  single-page UI (cytoscape.js)
eval/backtest.py   prediction vs DOT LateAircraftDelay; the agents' own calls vs what the airline did
video/             the demo video as code: edge-tts voice-over + word timings, Playwright recording, Remotion
.github/workflows  keep-warm: pings the free backend every 10 min so visitors never meet a cold start
tests/test_irops.py invariants: crew legality, slack propagation, claim race, read-only, no routing via cancelled
                   flights, atomic + verified + exactly reversible approvals, tenant isolation, MCP, constraints
```

Design choices, from "how to build an agent that reasons over a graph":
- The model never writes Cypher. It chooses from a fixed catalogue; ids are resolved against the graph first.
- Every answer returns the traversal path, not an explanation reconstructed afterwards.
- Outcomes are explicit: `answered`, `from_memory`, `not_in_graph` (not the same as "nothing affected"), `unknown_entity`.
- With an LLM enabled, any flight/crew/booking id in the model's answer that no tool returned is withheld (typographic
  hyphens are normalised first, so `2022‑12‑22‑WN…` cannot slip past). Tools hand the model readable times, never raw minute counts.
- Memory is more nodes in the same graph. One graph per airline (`IROPS_GRAPH`), so tenants are isolated by FalkorDB's
  multi-graph, not by filters.
- Nothing changes the operation until a human approves; every approval is re-read from the graph.

## Run it

Requires Docker and [uv](https://docs.astral.sh/uv/).

```bash
docker compose up -d                       # FalkorDB v4.22.1 (pinned: 6.x has not ported A*/CCH yet)
mkdir -p data/raw
curl -o data/raw/bts_2022_12.zip https://transtats.bts.gov/PREZIP/On_Time_Reporting_Carrier_On_Time_Performance_1987_present_2022_12.zip
curl -o data/raw/airports.dat https://raw.githubusercontent.com/jpatokal/openflights/master/data/airports.dat
uv sync
uv run python -m irops.ingest              # Southwest graph, ~14 s
uv run python -m irops.ingest irops_AS     # Alaska graph
uv run python -m eval.backtest             # writes eval/results.json
uv run python -m tests.test_irops          # "all checks passed"
uv run uvicorn irops.api:app --port 8000   # open http://localhost:8000
```

Optional: `export OPENROUTER_API_KEY=...` to have a free OpenRouter model answer free-form questions through the same tools
(the briefing is built from the graph numbers, so a run never waits on an LLM). The free tier allows about 50
requests a day without account credits, so each visitor gets 6 LLM questions an hour and a 429 pauses the LLM for
10 minutes; past that, answers come from the intent router and say so. Requests use OpenRouter's `models` fallback routing over free tool-calling models
(default `poolside/laguna-s-2.1:free`, then two Nemotron models; override with `OPENROUTER_MODELS`), so one busy free
model does not fail the request. Without a key, or if every model is down, questions go through a small intent router
and the answer says so.
Against FalkorDB Cloud, set `FALKORDB_HOST`, `FALKORDB_PORT`, `FALKORDB_USERNAME`, `FALKORDB_PASSWORD`.

The FalkorDB Browser is at http://localhost:3000 to explore the graph (graph `irops_WN`).

### The demo video

The video is generated from this repo, against the live app:

```bash
cd video && npm ci
npm run tts      # Microsoft neural voice (edge-tts) per scene + word timings -> public/*.mp3, timing.json
npm run record   # Playwright drives https://irops-brain.vercel.app, long waits marked as cuts -> public/screen.webm
npm run render   # Remotion: title cards, the recording, karaoke subtitles from the word timings -> out/*.mp4
npm run finish   # loudness to -16 LUFS for upload -> out/irops-brain-demo-final.mp4
```

The narration is `video/narration.json`. Every screen in the video is the real app doing the real thing; the waits for
the agents' run are cut out, nothing else is edited.

## AI assistance disclosure

This project was built with Claude Code (Anthropic) as a coding assistant: research, code generation, and debugging,
under the direction and review of the team. The team chose the problem, the graph model and the agent policy, checked
the results against DOT data, and can explain every query above.

## Data and licences

- US DOT Bureau of Transportation Statistics, Reporting Carrier On-Time Performance (public domain).
- OpenFlights airports database (Open Database License).
- Live: FAA NAS Status (nasstatus.faa.gov) and NOAA aviationweather.gov METAR (US government, public).
- Regulations: 14 CFR Part 117 (eCFR), 14 CFR 260.6 (refunds). Not affiliated with, or endorsed by, any airline.
- Voice-over: Microsoft Edge neural text-to-speech (en-US-AndrewMultilingualNeural) via edge-tts. UI look inspired by
  Halo Lab's "BLITZ" agency landing page on Dribbble.
