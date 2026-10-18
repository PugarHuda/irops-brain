# We replayed the Southwest meltdown on a graph, and the graph saw it coming

In the week before Christmas 2022, Southwest Airlines cancelled nearly 17,000 flights. The US DOT later fined the
airline $140 million. Weather started it, but what turned a bad storm into a meltdown was something else. The airline
lost track of how its operation was connected. Crews spent hours on hold just to tell scheduling where they were.

The airline had the data. Each fact sat in a separate system, and none of those systems recorded how the facts
connected. A flight delay only matters because of what it touches: the next leg that aircraft flies, the crew whose
legal duty clock is running, the passengers who need to make a connection. That is a graph problem, so for
[Graph Hacks](https://www.wemakedevs.org/hackathons/falkordb) we built one.

## The graph

We loaded every Southwest flight from 21 to 23 December 2022 from the public DOT On-Time Performance data into
FalkorDB. That is 11,754 flights, the aircraft that flew them, and the airports. On top of the real schedule we
generated crews and connecting passengers. They are synthetic, but they follow the real rules: no crew exceeds its
flight-duty limit under 14 CFR 117.13, and every crew gets its 10 hours of rest.

These are the edges that matter:

- `NEXT_LEG`: the same aircraft's next flight, with the turn time and the spare minutes (slack)
- `CREW_NEXT`: the same crew's next flight
- `PAX_CONNECT`: passengers connecting between two flights
- `CONN`: a connection that is feasible on time, which we use for re-routing

The result is about 47,000 nodes and 410,000 relationships, and it loads in 14 seconds.

## The agents

There are three agents, one each for Ops control, Crew scheduling and Passenger recovery. They do not write Cypher.
Each one picks from a fixed catalogue of queries that we wrote and reviewed, and every id it passes in is checked
against the graph before the query runs. That idea came from the WeMakeDevs post on agents that reason over graphs.

- **Ops** runs a variable-length traversal over `NEXT_LEG|CREW_NEXT` and subtracts the slack at each hop. The path the
  query returns is the explanation the agent shows. It uses `algo.betweenness` to find the flights the network depends
  on most.
- **Crew** looks for a reserve pilot at the airport. If none is on call there, it runs `algo.AStar` over the route
  network to find the nearest base that has one. It claims the reserve with a single check-and-create query, so two
  agents can never win the same pilot. A crew that times out cannot fly the rest of its duty period either, so the
  reserve takes over those legs too, but only as many as its own duty limit allows. The first leg nobody can fly
  legally goes back to Ops, which proposes cancelling it.
- **Passenger** finds re-routings with `algo.SPpaths` (k-shortest paths) over the connection network. Seats,
  cancellations and projected delays are checked inside the query. A booking with no feasible re-routing gets a
  refund proposal under 14 CFR 260.6, and the impact panel counts every passenger owed one: last leg 3h+ late, or a
  leg cancelled.

Every proposal cites a rule node and waits for a human to approve it. Decisions, handoffs and lessons are stored as
nodes too. So when you run the scenario again, the agents remember what happened at Denver last time, and if a
human rejected an action, the same action comes back as WITHHELD instead of being proposed again.

## Does it actually predict anything?

DOT records how many minutes of each flight's delay came from its aircraft arriving late. We seeded the graph with only
each flight's *own* delay and let the traversal spread it, then compared the result with what DOT recorded:

| Airline | Traversal | Caught | Precision | Mean error |
|---|---|---|---|---|
| Southwest | 1 hop | 1,211 of 3,087 | 95% | 33.7 min |
| Southwest | 8 hops | 1,505 of 3,087 | 95% | 30.2 min |
| Alaska | 1 hop | 96 of 314 | 85% | 23.9 min |
| Alaska | 8 hops | 119 of 314 | 87% | 26.6 min |

On both airlines, following the graph past the previous leg catches 24% more real knock-on delays at the same or better
precision. Recall is 38-49%; most misses are delay chains that started before our window opened.

We also scored the agents' own calls against what the airline did after the clock. In Denver, 7 of the 11 flights
they proposed to cancel were cancelled by Southwest too or flown 3h+ late. Their 3h+ delay calls were right 54% of
the time, against a 52% base rate downstream: at 08:00 the graph cannot see the storm's new delays still to come.
We kept that number in the app, because a model that is only right about delay already in the system should say so.

## Approvals you can trust, and undo

An approval in IROPS Brain is one atomic step from `proposed` to `executing`, so a double click or a second operator
cannot run it twice. After the write, we read the graph back and stamp the proposal APPROVED only if the graph now
shows the intended state. Then the impact is projected again on the changed graph, so the operator sees what the
approval moved: one fewer crew over its limit after a reserve swap, fewer knock-on delays and more refunds owed after a
cancel. Nothing is deleted for good: a cancelled flight just loses its `:Active` label, and a
relieved crew's connection is retyped to `CREW_NEXT_RELIEVED`. We first tried filtering every variable-length path
instead, and it was 90 times slower. "Undo all approvals" restores the graph exactly, and the test suite checks that
the counts match before and after.

## Two airlines, and your own agent

Alaska Airlines' Seattle ice storm runs on a second graph, `irops_AS`, with the same agents. The same read-only tools
are also served as an MCP server, so you can point Claude Code at the live graph:

```
claude mcp add --transport http irops-brain https://irops-brain.vercel.app/mcp/
```

Besides the tools it serves the decision log (every proposal and what a human did with it), the cited rules and past
lessons as resources, and a `disruption_briefing` prompt that walks your agent through the steps ours take.

## What we learned about FalkorDB

- `GRAPH.RO_QUERY` gives you a read-only guarantee for agent tools without any extra code.
- Each graph is a separate tenant: one graph per airline, isolated by the database rather than by a filter.
- Because writes run one at a time, a check-and-claim inside one query cannot be won twice, so we needed no lock
  service.
- Unique constraints make ids real identities, and a full-text index turns "Midway" into `MDW` for the agent.
- On the 100 MB free cloud tier, `algo.betweenness` with 256 samples ran out of memory and took the graph with it.
  64 samples fits, and the app now rebuilds the graph if it ever comes back empty.
- We pinned v4.22.1 because A\* has not been ported to 6.x yet.

Try it at https://irops-brain.vercel.app. The code, the data pipeline, the backtest and the demo video's source
(an edge-tts voice-over, a Playwright recording of the live app and a Remotion composition) are on GitHub:
https://github.com/PugarHuda/irops-brain
