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
  agents can never win the same pilot.
- **Passenger** finds re-routings with `algo.SPpaths` (k-shortest paths) over the connection network. Seats,
  cancellations and projected delays are checked inside the query.

Every proposal cites a rule node and waits for a human to approve it. Decisions, handoffs and lessons are stored as
nodes too. So when you run the scenario again, the agents remember what happened at Denver last time.

## Does it actually predict anything?

DOT records how many minutes of each flight's delay came from its aircraft arriving late. We seeded the graph with only
each flight's *own* delay and let the traversal spread it, then compared the result with what DOT recorded:

| Traversal | Caught | Precision | Mean error |
|---|---|---|---|
| 1 hop | 1,211 of 3,087 | 95% | 33.7 min |
| 8 hops | 1,505 of 3,087 | 95% | 30.2 min |

Following the graph past the previous leg catches 24% more real knock-on delays at the same precision. Recall is 49%.
Most of the misses are delay chains that started before our window opened.

## What we learned about FalkorDB

- `GRAPH.RO_QUERY` gives you a read-only guarantee for agent tools without any extra code.
- Each graph is a separate tenant: one graph per airline, isolated by the database rather than by a filter.
- Because writes run one at a time, a check-and-claim inside one query cannot be won twice, so we needed no lock
  service.
- We pinned v4.22.1 because A\* has not been ported to 6.x yet.

The code, the data pipeline and the backtest are on GitHub: https://github.com/PugarHuda/irops-brain
