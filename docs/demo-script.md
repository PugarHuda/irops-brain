# Demo video script (about 3:00)

Record at 1440x900, browser zoom 100%. Voice-over reads the quoted lines.

| Time | Screen | Voice-over |
|---|---|---|
| 0:00 | Title card: news headline "Southwest cancels nearly 17,000 flights", then the app header | "December 2022. Southwest cancelled nearly 17,000 flights in a week and was later fined 140 million dollars. The storm started it. What made it a meltdown was that the airline lost track of how its operation connects: which crew is on which aircraft, which passengers connect where." |
| 0:20 | Story section, clock at 08:00 Denver | "IROPS Brain replays that week from public DOT data. Every flight, aircraft rotation, crew pairing and passenger connection is a node or an edge in FalkorDB. We stop the clock at 8 a.m. in Denver on December 22. The graph only knows what was known then." |
| 0:40 | Click **Run the agents**. Impact numbers fill in | "Three agents work the storm. Ops traces how 16 late flights spread through aircraft and crew connections, using up the spare time at each turn: 50 more flights, 741 broken connections, 21 crews pushed past their legal duty limit." |
| 1:00 | Evidence canvas: hover one path | "Each number has a path behind it. This aircraft's delay reaches this flight two turns later. The agent shows this path as its explanation. Nothing is generated after the fact." |
| 1:15 | Proposal strips: a crew strip, then a cancellation strip | "Crew scheduling finds reserves. If Denver has none on call, it uses A* over the route network to find the nearest base that does, and claims that reserve atomically, so two agents can never take the same pilot. Each proposal cites 14 CFR 117." |
| 1:35 | Click **Approve** on a strip. Stamp appears, verification line shows | "Nothing changes until a human approves. When you approve, the change is written to the graph and read back as proof." |
| 1:50 | Trace lanes: expand one Cypher block | "The agents never write Cypher. They pick from a fixed set of queries written by humans. Every argument is checked against the graph, and every step is logged here." |
| 2:05 | Ask box: "What breaks if WN2262 is 2 hours late?" | "You can ask questions. The answer uses the same tools, and any flight id that did not come from the graph is withheld." |
| 2:20 | Proof section: backtest table | "Does it work? We seeded only each flight's own delay and let the graph propagate it, then compared with what DOT recorded. 95 percent precision. Going multi-hop catches 24 percent more knock-on delays than looking one leg back." |
| 2:35 | Run again: a lesson is recalled in the Ops lane | "Run it again and the agents remember what happened last time at Denver. Memory is just more nodes in the same graph." |
| 2:42 | Switch scenario to "Ice storm reaches Seattle · AS", run; then scroll to the MCP panel | "Another airline runs on its own graph with the same agents. The same tools are also an MCP server, so you can plug your own agent into the live graph." |
| 2:55 | Repo URL plus "Built on FalkorDB" | "IROPS Brain. The connections are the database." |
