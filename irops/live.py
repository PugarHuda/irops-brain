"""Live context: what the FAA and the weather say right now, written into each airline's graph.

Sources (public, no key):
  FAA NAS Status   https://nasstatus.faa.gov/api/airport-status-information   ground stops, ground delay
                   programs, arrival/departure delays, closures (XML)
  aviationweather  https://aviationweather.gov/api/data/metar                 current METAR, flight category

Each refresh replaces the (:Live) nodes of one graph and links them to the airports they affect, so agents and
MCP clients read live constraints with the same graph queries as everything else. The flight schedule is the
December 2022 replay; the live layer shows how today's constraints would land on that network.
"""
import contextlib
import csv
import time

import httpx
from defusedxml import ElementTree

from irops.ingest import AIRPORTS

FAA_URL = "https://nasstatus.faa.gov/api/airport-status-information"
METAR_URL = "https://aviationweather.gov/api/data/metar"
MAX_AGE_S = 300   # the FAA feed updates every few minutes; don't hammer it
CONSTRAINED = {"IFR", "LIFR"}


def icao_map():
    na = chr(92) + "N"
    return {r[4]: r[5] for r in csv.reader(open(AIRPORTS, encoding="utf-8")) if r[4] != na and r[5] != na}


def fetch_faa(client):
    """Every airport entry in every FAA delay type, as flat dicts."""
    root = ElementTree.fromstring(client.get(FAA_URL).content)
    out = []
    for dt in root.iter("Delay_type"):
        kind = (dt.findtext("Name") or "").strip()
        for entry in dt.iter():
            code = entry.findtext("ARPT")
            if not code or entry.tag in ("Delay_type",):
                continue
            leaf = {e.tag: (e.text or "").strip() for e in entry.iter() if len(e) == 0 and e.tag != "ARPT"}
            for e in entry.iter("Arrival_Departure"):
                leaf["direction"] = e.get("Type", "")
            reason = leaf.get("Reason", "")
            # ponytail: drop standing NOTAM closures for general aviation only; they don't constrain airlines
            if "Closure" in kind and ("TRANSIENT GA" in reason or "NON SKED" in reason):
                continue
            out.append({"code": code.strip().upper(), "kind": kind, "reason": reason,
                        "detail": "; ".join(f"{k}: {v}" for k, v in leaf.items() if k != "Reason" and v)})
    return out, (root.findtext("Update_Time") or "").strip()


def fetch_metar(client, icaos):
    rows = []
    for i in range(0, len(icaos), 80):
        r = client.get(METAR_URL, params={"ids": ",".join(icaos[i:i + 80]), "format": "json"})
        rows += r.json() if r.status_code == 200 and r.content.strip() else []
    return rows


def refresh(tools, force=False, lock=None):
    """Pull both feeds and rewrite this graph's live layer. Cached for MAX_AGE_S. `lock` (a context manager) is held
    only while writing, not during the network fetch, so a slow FAA response never blocks an agent run."""
    g = tools._run
    last = g("MATCH (f:LiveFeed {id: 'feed'}) RETURN f.fetched AS t")
    if last and not force and time.time() - last[0]["t"] < MAX_AGE_S:
        return {"refreshed": False, "fetched": last[0]["t"]}
    codes = [r["code"] for r in g("MATCH (a:Airport) RETURN a.code AS code")]
    icao = icao_map()
    with httpx.Client(timeout=15, headers={"User-Agent": "irops-brain (graph hackathon demo)"}) as client:
        faa, faa_time = fetch_faa(client)
        metar = fetch_metar(client, sorted({icao[c] for c in codes if c in icao}))
    by_icao = {v: k for k, v in icao.items() if k in set(codes)}
    wx = [{"code": by_icao[m["icaoId"]], "cat": m.get("fltCat") or "", "raw": m.get("rawOb", ""),
           "obs": m.get("reportTime", "")} for m in metar if m.get("icaoId") in by_icao]
    mine = [x for x in faa if x["code"] in set(codes)]
    with lock or contextlib.nullcontext():
        return _write(g, mine, wx, faa, faa_time)


def _write(g, mine, wx, faa, faa_time):
    g("MATCH (l:Live) DETACH DELETE l", write=True)
    g("""UNWIND $rows AS r MATCH (a:Airport {code: r.code})
         CREATE (l:Live {source: 'FAA', kind: r.kind, reason: r.reason, detail: r.detail, as_of: $t})-[:AT]->(a)""",
      {"rows": mine, "t": faa_time}, write=True)
    g("""UNWIND $rows AS r MATCH (a:Airport {code: r.code})
         SET a.wx_cat = r.cat, a.wx_raw = r.raw, a.wx_at = r.obs""", {"rows": wx}, write=True)
    g("""UNWIND $rows AS r MATCH (a:Airport {code: r.code}) WHERE r.cat IN $bad
         CREATE (l:Live {source: 'METAR', kind: 'Flight category ' + r.cat, reason: r.raw, detail: '', as_of: r.obs})-[:AT]->(a)""",
      {"rows": wx, "bad": sorted(CONSTRAINED)}, write=True)
    now = time.time()
    g("MERGE (f:LiveFeed {id: 'feed'}) SET f.fetched = $t, f.faa_time = $ft, f.faa_entries = $n, f.metars = $m",
      {"t": now, "ft": faa_time, "n": len(faa), "m": len(wx)}, write=True)
    return {"refreshed": True, "fetched": now, "faa_entries_national": len(faa), "faa_in_network": len(mine),
            "metars": len(wx)}
