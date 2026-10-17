"""An LLM over the fixed tool catalogue, through OpenRouter's free models. The model picks tools
and fills arguments; it never writes Cypher. Its answer is checked: every flight, crew, reserve
or booking id it mentions must have come back from a tool, or the answer is replaced by the rows.

Without OPENROUTER_API_KEY (or when every model is down), questions go through a small intent
router to the same tools, and the answer says so.
"""
import json
import os
import re
from datetime import timedelta

import httpx

from irops.ingest import EPOCH
from irops.tools import CATALOGUE, Tools, hhmm

URL = "https://openrouter.ai/api/v1/chat/completions"
# Free models that support tool calling. OpenRouter tries them in order (its `models` fallback routing),
# so a rate-limited or offline free model does not fail the request. Override with OPENROUTER_MODELS.
MODELS = os.getenv("OPENROUTER_MODELS", "poolside/laguna-s-2.1:free,nvidia/nemotron-3-super-120b-a12b:free,"
                   "nvidia/nemotron-3-ultra-550b-a55b:free").split(",")
IDS = re.compile(r"\b(2022-12-\d\d-[A-Z0-9]{2}\d+-[A-Z]{3}|R-[A-Z]{3}-\d{3}|C\d{4}|B\d{5})\b")

SYSTEM = """You are the duty manager's analyst in an airline operations control centre during
irregular operations. The airline's live state is a FalkorDB graph you can only reach through the tools.

Rules:
- Every fact you state must come from a tool result in this conversation. Quote flight, crew,
  reserve and booking ids exactly as tools returned them. Never guess an id.
- When a tool's outcome is "not_in_graph", say the graph has no record of it. That is different
  from "nothing is affected". When it is "unknown_entity", say the id was not found.
- Explain *why* using the returned paths (e.g. "same aircraft N8642Q", "crew C0464 continues on").
- Be brief: a duty manager reads this between phone calls. Plain text, no markdown tables."""


def available():
    return bool(os.getenv("OPENROUTER_API_KEY"))


def tool_specs():
    return [{"type": "function", "function": {"name": n, "description": d, "parameters": {
        "type": "object", "properties": p, "required": r, "additionalProperties": False}}}
        for n, (d, p, r) in CATALOGUE.items()]


def _chat(messages, tools=None):
    body = {"models": MODELS, "messages": messages, "max_tokens": 2000, "temperature": 0.2,
            "reasoning": {"exclude": True}}  # free reasoning models: keep their thinking out of the answer
    if tools:
        body["tools"] = tools
    r = httpx.post(URL, json=body, timeout=90, headers={
        "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
        "HTTP-Referer": "https://irops-brain.onrender.com", "X-Title": "IROPS Brain"})
    r.raise_for_status()
    data = r.json()
    if "error" in data:  # OpenRouter can return 200 with an error body when every model failed
        raise RuntimeError(str(data["error"])[:200])
    return data


TIME_KEYS = {"dep", "arr", "arrives", "on_call_to", "on_call_from", "duty_start", "at", "planned_arr"}
DASHES = str.maketrans({c: "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2212"})


def _readable(row):
    # times are minutes since 2022-12-21 00:00 UTC in the graph; a model must never see (or repeat) the raw number
    return {k: (hhmm(v) + " UTC" if k in TIME_KEYS and isinstance(v, (int, float)) and v > 0 else v)
            for k, v in row.items()} if isinstance(row, dict) else row


def _compact(res):
    """What the model sees from a tool: rows with readable times, and a readable rendering of each path."""
    out = {k: v for k, v in res.items() if k not in ("cypher", "paths")}
    out["rows"] = [_readable(r) for r in res.get("rows", [])[:25]]
    out["paths"] = [" -> ".join(f"{n['key']}" for n in p["nodes"]) + "  via " +
                    ", ".join(f"{e['type']}({e.get('tail') or e.get('crew') or ''})" for e in p["edges"])
                    for p in res.get("paths", [])[:10]]
    return json.dumps(out, default=str)[:12000]


def _text(msg):
    # strip reasoning, and normalise typographic hyphens so ids like 2022-12-22-WN2262-DEN cannot dodge the validator
    return re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S).translate(DASHES).strip()


def ask(question, tenant="irops_WN"):
    t = Tools(tenant)
    if not available():
        return route(question, t)
    try:
        return _ask_llm(question, t)
    except Exception as e:  # rate limit, every free model busy, bad key: answer from the same tools, and say why
        out = route(question, t)
        out["note"] = f"LLM unavailable ({type(e).__name__}: {str(e)[:120]}); answered by the intent router."
        return out


def _ask_llm(question, t: Tools):
    now = t.now()
    clock = (f"\nSimulation clock: {hhmm(now)} UTC ({(EPOCH + timedelta(minutes=now)):%Y-%m-%d}). 'Now' and 'today' mean "
             f"this moment, not the real date. Airline graph: {t.tenant} (flight numbers start with {t.carrier}).")
    messages = [{"role": "system", "content": SYSTEM + clock}, {"role": "user", "content": question}]
    calls, model, msg = [], None, {}
    seen = set(IDS.findall(question.upper().translate(DASHES)))  # ids the user typed may be named back to them
    for _ in range(8):
        data = _chat(messages, tool_specs())
        model = data.get("model", model)
        msg = data["choices"][0]["message"]
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            break
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": tool_calls})
        for tc in tool_calls:
            name = tc["function"]["name"]
            try:
                if name not in CATALOGUE:
                    raise ValueError(f"no tool named {name}")
                args = json.loads(tc["function"].get("arguments") or "{}")
                res = getattr(t, name)(**args)
                body = _compact(res)
                seen |= set(IDS.findall(json.dumps(res, default=str)))
                calls.append({"tool": name, "args": args, "outcome": res["outcome"], "rows": res["rows"][:12],
                              "paths": res["paths"][:12], "cypher": res["cypher"]})
            except Exception as e:  # bad arguments go back to the model as an error, not a crash
                body = f"error: {type(e).__name__}: {e}"
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": body})
    text = _text(msg)
    if not text:
        raise RuntimeError("the model returned no answer")
    invented = sorted(set(IDS.findall(text)) - seen)
    if invented:  # the model named something no tool returned: do not show it as fact
        rows = [r for c in calls for r in c["rows"][:5]]
        text = ("The model's answer named ids that no tool returned (" + ", ".join(invented) + "), so it is not shown. "
                + ("What the graph returned: " + json.dumps(rows, default=str)[:1500] if rows else "The tools returned no rows."))
    return {"by": model or "llm", "answer": text, "calls": calls, "unverified_ids": invented}


def briefing(facts):
    """LLM briefing, checked: every number in it must appear in the facts. One rewrite with the offending numbers
    named; if it still invents or rounds a number, the caller uses the template briefing."""
    known = set(re.findall(r"\d+", json.dumps(facts, default=str).replace(",", "")))  # "1,026" in the story
    messages = [{"role": "user", "content":
                 "Write a 4-6 sentence briefing for the duty manager from these facts only. Plain text. "
                 "Lead with the impact, then what the agents propose, then anything remembered from earlier "
                 "sessions. Copy every number exactly as it appears in the facts: never round, estimate or add one."
                 "\n\n" + json.dumps(facts, default=str)}]
    try:
        for _ in range(2):
            data = _chat(messages)
            text = _text(data["choices"][0]["message"])
            extra = set(re.findall(r"\d+", text.replace(",", ""))) - known
            if text and not extra:
                return {"text": text, "model": data.get("model")}
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": f"These numbers are not in the facts: {', '.join(sorted(extra))}. "
                                                     "Rewrite using only numbers that appear in the facts, exactly."}]
        return None
    except Exception:
        return None  # the template briefing still works


def route(q, t: Tools):
    """Keyless mode: map common questions to one tool each."""
    u, c = q.upper(), t.carrier
    fl = re.search(rf"(2022-12-\d\d-{c}\d+-[A-Z]{{3}}|{c}\s?\d{{1,4}}(?:\s+2022-12-\d\d)?(?:\s+[A-Z]{{3}})?)", u)
    mins = re.search(r"(\d{2,4})\s*(MIN|M\b)", u)
    hrs = re.search(r"(\d+(?:\.\d+)?)\s*(H|HOUR)", u)
    ap = [a for a in re.findall(r"\b([A-Z]{3})\b", u) if a not in {"WHO", "THE", "AND", "FOR", "CAN", "HOW", "WHY",
                                                                   "MIN", "ARE", "OUT", "ANY", "OUR"}]
    bk = re.search(r"\bB\d{5}\b", u)
    if not ap:  # "Denver", "Midway": resolve names through the airport full-text index
        named = re.search(r"\b(?:at|in|from|about|to|for)\s+([A-Za-z][A-Za-z .'-]{2,40})", q, re.I)
        hit = t.resolve_airport(named.group(1)) if named else None
        if hit and hit["outcome"] == "answered":
            ap = [hit["rows"][0]["code"]]
    if fl and (mins or hrs):
        d = int(mins.group(1)) if mins else int(float(hrs.group(1)) * 60)
        res, tool, args = t.propagate_delay([{"flight": fl.group(1), "delay": d}], 8), "propagate_delay", {"flight": fl.group(1), "delay": d}
    elif bk:
        res, tool, args = t.rebook_options(bk.group(0), 3), "rebook_options", {"booking_id": bk.group(0)}
    elif re.search(r"RESERVE|COVER|CREW AT", u) and ap:
        res, tool, args = t.find_reserve(ap[-1], minutes_needed=300), "find_reserve", {"airport": ap[-1]}
    elif re.search(r"RIGHT NOW|LIVE|TODAY|FAA|GROUND STOP|WEATHER|METAR", u):
        from irops import live
        live.refresh(t)
        res, tool, args = t.live_conditions(), "live_conditions", {}
    elif re.search(r"\bC\d{4}\b|\bR-[A-Z]{3}-\d{3}\b", u):
        cid = re.search(r"\bC\d{4}\b|\bR-[A-Z]{3}-\d{3}\b", u).group(0)
        res, tool, args = t.crew_duty(cid), "crew_duty", {"crew_id": cid}
    elif fl:
        res, tool, args = t.resolve_flight(fl.group(1)), "resolve_flight", {"ref": fl.group(1)}
    elif re.search(r"CRITICAL|LOAD.BEARING|MOST IMPORTANT", u):
        day = (EPOCH + timedelta(minutes=t.now())).strftime("%Y-%m-%d")  # the simulation clock's day
        res, tool, args = t.critical_flights(day, 10), "critical_flights", {"date": day}
    elif re.search(r"CUT OFF|NETWORK|ISOLATED|CANCELLED", u):
        res, tool, args = t.network_health(), "network_health", {}
    elif re.search(r"REMEMBER|LAST TIME|BEFORE|HISTORY", u) and (ap or fl):
        k = fl.group(1) if fl else ap[-1]
        res, tool, args = t.recall(k), "recall", {"about": k}
    elif re.search(r"STRANDED|BROKEN|MISCONNECT|REBOOK", u):
        res, tool, args = t.disrupted_bookings(20), "disrupted_bookings", {}
    else:
        return {"by": "router", "answer": f"Try: 'What breaks if {c}2262 2022-12-22 DEN is 2h late?', "
                "'Who can cover a crew at Denver?', 'Rebook B09455', 'Which flights are load-bearing?', "
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
    if tool == "live_conditions":
        f = r.get("feed", {})
        bits = [f"{x['airport']} ({'; '.join(l['kind'] for l in x['live'])}; {int(x['daily_flights'] or 0)} flights/day touch it)"
                for x in rows[:5]]
        return f"Right now (FAA {f.get('faa_time', '?')}): " + ", ".join(bits) + "."
    if tool == "crew_duty":
        last = rows[-1]
        return (f"{len(rows)} legs; duty limit {last['fdp_limit']} min, projected use {last['fdp_used']} min "
                f"({'over' if last['fdp_used'] > last['fdp_limit'] else 'within'} the limit).")
    if tool == "resolve_flight":
        return "; ".join(f"{x['id']} {x['origin']}->{x['dest']} {x['status']}" for x in rows[:6])
    if tool == "disrupted_bookings":
        return f"{len(rows)} broken itineraries, e.g. " + ", ".join(f"{x['booking']} ({x['reason']})" for x in rows[:4])
    return json.dumps(rows[:5], default=str)
