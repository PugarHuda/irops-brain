"""Claude over the fixed tool catalogue. The model picks tools and fills arguments;
it never writes Cypher. Its answer is checked: every flight, crew, reserve or booking
id it mentions must have come back from a tool, or the answer is replaced by the rows.

Without ANTHROPIC_API_KEY, questions go through a small intent router to the same tools.
"""
import json
import os
import re

from irops.tools import Tools, tool_specs

MODEL = "claude-opus-5-5"
IDS = re.compile(r"\b(2022-12-\d\d-WN\d+-[A-Z]{3}|R-[A-Z]{3}-\d{3}|C\d{4}|B\d{5})\b")

SYSTEM = """You are the duty manager's analyst in an airline operations control centre during
irregular operations. The airline's live state is a FalkorDB graph you can only reach through the tools.

Rules:
- Every fact you state must come from a tool result in this conversation. Quote flight, crew,
  reserve and booking ids exactly as tools returned them.
- When a tool's outcome is "not_in_graph", say the graph has no record of it. That is different
  from "nothing is affected". When it is "unknown_entity", say the id was not found.
- Explain *why* using the returned paths (e.g. "same aircraft N8642Q", "crew C0464 continues on").
- Be brief: a duty manager reads this between phone calls."""


def available():
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def _client():
    import anthropic
    return anthropic.Anthropic()


def _create(client, **kw):
    # Server-side fallback: if a safety classifier declines, the API re-runs on a fallback model.
    return client.beta.messages.create(model=MODEL, betas=["server-side-fallback-2026-07-01"],
                                       fallbacks="default", output_config={"effort": "medium"}, **kw)


def _compact(res):
    """What the model sees from a tool: rows and a readable rendering of each path."""
    out = {k: v for k, v in res.items() if k not in ("cypher", "paths")}
    out["rows"] = res.get("rows", [])[:25]
    out["paths"] = [" -> ".join(f"{n['key']}" for n in p["nodes"]) + "  via " +
                    ", ".join(f"{e['type']}({e.get('tail') or e.get('crew') or ''})" for e in p["edges"])
                    for p in res.get("paths", [])[:10]]
    return json.dumps(out, default=str)[:12000]


def ask(question, tenant="irops_WN"):
    t = Tools(tenant)
    if not available():
        return route(question, t)
    client = _client()
    messages = [{"role": "user", "content": question}]
    calls, seen = [], set()
    for _ in range(8):
        r = _create(client, max_tokens=16000, system=SYSTEM, tools=tool_specs(), messages=messages)
        if r.stop_reason == "refusal":
            return {"by": "claude", "answer": "The model declined this request.", "calls": calls}
        if r.stop_reason != "tool_use":
            break
        messages.append({"role": "assistant", "content": r.content})
        results = []
        for b in r.content:
            if b.type != "tool_use":
                continue
            try:
                res = getattr(t, b.name)(**b.input)
                body, err = _compact(res), False
                seen |= set(IDS.findall(json.dumps(res, default=str)))
                calls.append({"tool": b.name, "args": b.input, "outcome": res["outcome"], "rows": res["rows"][:12],
                              "paths": res["paths"][:12], "cypher": res["cypher"]})
            except Exception as e:  # bad arguments come back to the model as an error, not a crash
                body, err = f"{type(e).__name__}: {e}", True
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": body, "is_error": err})
        messages.append({"role": "user", "content": results})
    text = "".join(b.text for b in r.content if b.type == "text")
    invented = sorted(set(IDS.findall(text)) - seen)
    if invented:  # the model named something no tool returned: do not show it as fact
        text = ("[answer withheld: it referenced ids no tool returned: " + ", ".join(invented) + "]\n" +
                "\n".join(json.dumps(c["rows"][:5], default=str) for c in calls))
    return {"by": "claude", "answer": text, "calls": calls, "unverified_ids": invented}


def briefing(facts):
    try:
        r = _create(_client(), max_tokens=4000, messages=[{"role": "user", "content":
                    "Write a 4-6 sentence briefing for the duty manager from these facts only. "
                    "Lead with the impact, then what the agents propose, then anything remembered from "
                    "earlier sessions. No numbers that are not in the facts.\n\n" + json.dumps(facts, default=str)}])
        return "".join(b.text for b in r.content if b.type == "text") if r.stop_reason != "refusal" else None
    except Exception:
        return None  # the template briefing still works


def route(q, t: Tools):
    """Keyless mode: map common questions to one tool each."""
    u = q.upper()
    fl = re.search(r"(2022-12-\d\d-WN\d+-[A-Z]{3}|WN\s?\d{1,4}(?:\s+2022-12-\d\d)?(?:\s+[A-Z]{3})?)", u)
    mins = re.search(r"(\d{2,4})\s*(MIN|M\b)", u)
    hrs = re.search(r"(\d+(?:\.\d+)?)\s*(H|HOUR)", u)
    ap = [a for a in re.findall(r"\b([A-Z]{3})\b", u) if a not in {"WHO", "THE", "AND", "FOR", "CAN", "HOW", "WHY",
                                                                   "MIN", "ARE", "OUT", "ANY", "OUR"}]
    bk = re.search(r"\bB\d{5}\b", u)
    if fl and (mins or hrs):
        d = int(mins.group(1)) if mins else int(float(hrs.group(1)) * 60)
        res, tool, args = t.propagate_delay([{"flight": fl.group(1), "delay": d}], 8), "propagate_delay", {"flight": fl.group(1), "delay": d}
    elif bk:
        res, tool, args = t.rebook_options(bk.group(0), 3), "rebook_options", {"booking_id": bk.group(0)}
    elif re.search(r"RESERVE|COVER|CREW AT", u) and ap:
        res, tool, args = t.find_reserve(ap[-1], minutes_needed=300), "find_reserve", {"airport": ap[-1]}
    elif re.search(r"CRITICAL|LOAD.BEARING|MOST IMPORTANT", u):
        res, tool, args = t.critical_flights("2022-12-22", 10), "critical_flights", {}
    elif re.search(r"CUT OFF|NETWORK|ISOLATED|CANCELLED", u):
        res, tool, args = t.network_health(), "network_health", {}
    elif re.search(r"REMEMBER|LAST TIME|BEFORE|HISTORY", u) and (ap or fl):
        k = fl.group(1) if fl else ap[-1]
        res, tool, args = t.recall(k), "recall", {"about": k}
    elif re.search(r"STRANDED|BROKEN|MISCONNECT|REBOOK", u):
        res, tool, args = t.disrupted_bookings(20), "disrupted_bookings", {}
    else:
        return {"by": "router", "answer": "Try: 'What breaks if WN2262 2022-12-22 DEN is 2h late?', "
                "'Who can cover a crew at SEA?', 'Rebook B09455', 'Which flights are load-bearing?', "
                "'Which airports are cut off?', 'What do we remember about DEN?'", "calls": []}
    return {"by": "router", "answer": _say(tool, res),
            "calls": [{"tool": tool, "args": args, "outcome": res["outcome"], "rows": res["rows"][:12],
                       "paths": res["paths"][:12], "cypher": res["cypher"]}]}


def _say(tool, r):
    if r["outcome"] == "unknown_entity":
        return "That id is not in the graph."
    if r["outcome"] == "not_in_graph":
        return "The graph has no record that answers this (which is not the same as 'nothing is affected')."
    rows = r["rows"]
    if tool == "propagate_delay":
        s = r["summary"]
        top = ", ".join(f"{x['fno']} {x['origin']}->{x['dest']} +{x['delay']}m" for x in sorted(rows, key=lambda x: -x["delay"])[:4])
        return (f"{s['downstream_flights']} downstream flights delayed ({s['pax_delayed']} pax), "
                f"{int(s['pax_misconnecting'])} pax misconnect, {s['crews_illegal']} crews over duty limit. Worst: {top}.")
    if tool == "find_reserve":
        x = rows[0]
        return (f"{x['reserve']} from {x['base']}" + (f", {int(x['deadhead_minutes'])} min deadhead (A*)." if x["deadhead_minutes"] else ", on call locally."))
    if tool == "rebook_options":
        return "; ".join(f"{' + '.join(o['flights'])} arriving {o['arrives_local']}" for o in rows)
    if tool == "critical_flights":
        return "Most load-bearing: " + ", ".join(f"{x['id']} ({x['pax']} pax)" for x in rows[:6])
    if tool == "network_health":
        return f"{r['components']} network component(s); cut off: {', '.join(r['cut_off_airports']) or 'none'}."
    if tool == "recall":
        return "; ".join(x["summary"] for x in rows[:3]) or "; ".join(x["lesson"] for x in r.get("lessons", [])[:3])
    if tool == "disrupted_bookings":
        return f"{len(rows)} broken itineraries, e.g. " + ", ".join(f"{x['booking']} ({x['reason']})" for x in rows[:4])
    return json.dumps(rows[:5], default=str)
