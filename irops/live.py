"""Live context: what the FAA and the weather say right now, written into each airline's graph.

Sources (public, no key):
  FAA NAS Status   https://nasstatus.faa.gov/api/airport-status-information   ground stops, ground delay
                   programs, arrival/departure delays, closures (XML)
  aviationweather  https://aviationweather.gov/api/data/metar                 current METAR, flight category
                   https://aviationweather.gov/api/data/taf                   TAF forecast: IFR/LIFR, thunderstorms
                                                                              and freezing precipitation in the next 12h
  adsb.lol         https://api.adsb.lol/v2/point/{lat}/{lon}/{nm}            live ADS-B around each flagged airport:
                                                                              the airline's aircraft on the ground there
  OpenSky Network  https://opensky-network.org/api/states/all               national ADS-B totals, airborne and on the
                                                                              ground (it refuses many cloud IPs; the totals
                                                                              appear when it answers)

Each refresh replaces the (:Live) nodes of one graph and links them to the airports they affect, so agents and
MCP clients read live constraints with the same graph queries as everything else. The flight schedule is the
December 2022 replay; the live layer shows how today's constraints would land on that network.
"""
import contextlib
import csv
import math
import time

import httpx
from defusedxml import ElementTree

from irops.ingest import AIRPORTS

FAA_URL = "https://nasstatus.faa.gov/api/airport-status-information"
METAR_URL = "https://aviationweather.gov/api/data/metar"
MAX_AGE_S = 300   # the FAA feed updates every few minutes; don't hammer it
CONSTRAINED = {"IFR", "LIFR"}
TAF_URL = "https://aviationweather.gov/api/data/taf"
HORIZON_S = 12 * 3600
OPENSKY_URL = "https://opensky-network.org/api/states/all"
OPENSKY_BOX = {"lamin": 17, "lomin": -170, "lamax": 72, "lomax": -60}  # the US, Alaska, Hawaii, Puerto Rico
# ponytail: anonymous OpenSky allows 400 credits a day and this box costs 4, so one fetch every 15 min serves
# every airline graph in this process. A free account (OPENSKY_USER) would allow far more.
OPENSKY_AGE_S = 900
CALLSIGN = {"WN": "SWA", "AS": "ASA"}  # ICAO airline designators in ADS-B callsigns
NEAR_DEG = 0.05                         # ~5 km: an aircraft on the ground this close is at the airport
_sky = {"t": 0.0, "states": None, "time": None}
ADSBLOL_URL = "https://api.adsb.lol/v2/point/{lat}/{lon}/{nm}"
GROUND_NM = 3  # nautical miles around the airport reference point


def ground_at(client, prefix, airports):
    """The airline's aircraft reporting on the ground within GROUND_NM of each airport, from adsb.lol."""
    at = {}
    for a in airports:
        r = client.get(ADSBLOL_URL.format(lat=round(a["lat"], 4), lon=round(a["lon"], 4), nm=GROUND_NM), timeout=15)
        r.raise_for_status()
        at[a["code"]] = sum(1 for x in r.json().get("ac") or []
                            if (x.get("flight") or "").strip().startswith(prefix) and x.get("alt_baro") == "ground")
    return at


def fetch_opensky(client):
    """Every ADS-B state vector over the box, cached for OPENSKY_AGE_S across graphs."""
    if _sky["states"] is None or time.time() - _sky["t"] >= OPENSKY_AGE_S:
        r = client.get(OPENSKY_URL, params=OPENSKY_BOX, timeout=30)
        r.raise_for_status()
        d = r.json()
        _sky.update(t=time.time(), states=d.get("states") or [], time=d.get("time"))
    return _sky["states"], _sky["time"]


def adsb(states, prefix, airports):
    """The airline's aircraft (by callsign prefix): how many are airborne, how many on the ground, and how many on
    the ground at each airport (within NEAR_DEG). `airports` is [{code, lat, lon}]."""
    mine = [x for x in states if (x[1] or "").strip().startswith(prefix) and x[5] is not None and x[6] is not None]
    ground = [x for x in mine if x[8]]
    at = {}
    for a in airports:
        if a["lat"] is None:
            continue
        k = NEAR_DEG / max(0.2, math.cos(math.radians(a["lat"])))
        n = sum(1 for x in ground if abs(x[6] - a["lat"]) <= NEAR_DEG and abs(x[5] - a["lon"]) <= k)
        at[a["code"]] = n
    return {"airborne": len(mine) - len(ground), "on_ground": len(ground), "at": at}


def _vis(v):
    """TAF visibility in statute miles: 6, "6+", "1/2", "1 1/2", or None."""
    if v is None:
        return None
    try:
        return sum(float(a) / float(b) if "/" in p else float(p) for p in str(v).rstrip("+").split()
                   for a, b in [p.split("/") if "/" in p else (p, 1)])
    except ValueError:
        return None


def category(f):
    """FAA flight category of one forecast period: ceiling is the lowest broken/overcast layer or vertical visibility."""
    ceil = min([c["base"] for c in f.get("clouds") or [] if c.get("cover") in ("BKN", "OVC", "OVX") and c.get("base") is not None]
               + ([f["vertVis"]] if f.get("vertVis") is not None else []), default=None)
    vis = _vis(f.get("visib"))
    lo = lambda c, v: (ceil is not None and ceil < c) or (vis is not None and vis < v)
    return "LIFR" if lo(500, 1) else "IFR" if lo(1000, 3) else "MVFR" if lo(3001, 5.01) else "VFR"


def taf_hazards(taf, now, horizon_s=HORIZON_S):
    """The forecast periods of one TAF, starting before now + horizon and not yet over, that would constrain an
    airline: IFR or LIFR, thunderstorms, freezing rain or drizzle (de-icing)."""
    out = []
    for f in taf.get("fcsts") or []:
        if f["timeTo"] <= now or f["timeFrom"] >= now + horizon_s:
            continue
        wx = f.get("wxString") or ""
        bits = [c for c in [category(f)] if c in CONSTRAINED] + (["thunderstorms"] if "TS" in wx else [])             + (["freezing precipitation"] if "FZ" in wx else [])
        if bits:
            change = f.get("fcstChange") or "base"
            out.append({"from": f["timeFrom"], "to": f["timeTo"], "what": bits,
                        "change": f"{change} {f['probability']}%" if f.get("probability") else change})
    return out


def fetch_taf(client, icaos):
    rows = []
    for i in range(0, len(icaos), 80):
        r = client.get(TAF_URL, params={"ids": ",".join(icaos[i:i + 80]), "format": "json"})
        rows += r.json() if r.status_code == 200 and r.content.strip() else []
    return rows


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
    aps = g("MATCH (a:Airport) RETURN a.code AS code, a.lat AS lat, a.lon AS lon")
    codes = [r["code"] for r in aps]
    icao = icao_map()
    ids = sorted({icao[c] for c in codes if c in icao})
    errors = {}
    with httpx.Client(timeout=15, headers={"User-Agent": "irops-brain (graph hackathon demo)"}) as client:
        faa, faa_time = fetch_faa(client)  # the FAA status is the layer's core: if it is down, keep the last layer
        try:
            metar = fetch_metar(client, ids)
        except httpx.HTTPError as e:  # weather is extra: a slow aviationweather.gov must not drop the FAA status
            metar, errors["metar"] = [], f"{type(e).__name__}: {e}"[:120]
        try:
            tafs = fetch_taf(client, ids)
        except httpx.HTTPError as e:
            tafs, errors["taf"] = [], f"{type(e).__name__}: {e}"[:120]
        prefix = CALLSIGN.get(tools.carrier, tools.carrier)
        sky = None
        try:
            states, sky_time = fetch_opensky(client)
            sky = {**adsb(states, prefix, aps), "time": sky_time}
        except (httpx.HTTPError, ValueError) as e:  # refused, rate-limited or down: no national totals this round
            errors["opensky"] = f"{type(e).__name__}: {e}"[:120]
    by_icao = {v: k for k, v in icao.items() if k in set(codes)}
    wx = [{"code": by_icao[m["icaoId"]], "cat": m.get("fltCat") or "", "raw": m.get("rawOb", ""),
           "obs": m.get("reportTime", "")} for m in metar if m.get("icaoId") in by_icao]
    mine = [x for x in faa if x["code"] in set(codes)]
    now = time.time()
    fc = []
    for t in tafs:
        hz = taf_hazards(t, now) if t.get("icaoId") in by_icao else []
        if hz:
            z = lambda s: time.strftime("%d %H:%MZ", time.gmtime(s))
            fc.append({"code": by_icao[t["icaoId"]], "raw": t.get("rawTAF", ""), "as_of": t.get("issueTime", ""),
                       "first": hz[0]["from"], "kind": "Forecast " + ", ".join(dict.fromkeys(w for h in hz for w in h["what"])),
                       "detail": "; ".join(f"{', '.join(h['what'])} {z(max(h['from'], now))} to {z(h['to'])} ({h['change']})"
                                           for h in hz[:4])})
    # the airline's aircraft on the ground at every airport the layer flags, the ones an ops controller looks at
    flagged = {x["code"] for x in mine} | {x["code"] for x in wx if x["cat"] in CONSTRAINED} | {x["code"] for x in fc}
    ground = None
    try:
        with httpx.Client(timeout=15, headers={"User-Agent": "irops-brain (graph hackathon demo)"}) as client:
            ground = ground_at(client, prefix, [a for a in aps if a["code"] in flagged and a["lat"] is not None])
    except (httpx.HTTPError, ValueError) as e:  # down: the rest of the layer still lands, without ground counts
        errors["adsb"] = f"{type(e).__name__}: {e}"[:120]
    with lock or contextlib.nullcontext():
        out = {**_write(g, mine, wx, faa, faa_time, fc), "tafs": len(tafs), **({"errors": errors} if errors else {})}
        if ground is not None:
            g("MATCH (a:Airport) REMOVE a.adsb_ground", write=True)
            g("UNWIND $rows AS r MATCH (a:Airport {code: r.code}) SET a.adsb_ground = r.n",
              {"rows": [{"code": c, "n": n} for c, n in ground.items()]}, write=True)
            out["adsb_ground_at"] = ground
        g("MATCH (f:LiveFeed {id: 'feed'}) SET f.adsb_airborne = $a, f.adsb_ground = $gr, f.adsb_time = $t",
          {"a": sky and sky["airborne"], "gr": sky and sky["on_ground"], "t": sky and sky["time"]}, write=True)
        if sky:
            out["adsb"] = {k: v for k, v in sky.items() if k != "at"}
        return out


def _write(g, mine, wx, faa, faa_time, fc=()):
    g("MATCH (l:Live) DETACH DELETE l", write=True)
    g("""UNWIND $rows AS r MATCH (a:Airport {code: r.code})
         CREATE (l:Live {source: 'FAA', kind: r.kind, reason: r.reason, detail: r.detail, as_of: $t})-[:AT]->(a)""",
      {"rows": mine, "t": faa_time}, write=True)
    g("""UNWIND $rows AS r MATCH (a:Airport {code: r.code})
         SET a.wx_cat = r.cat, a.wx_raw = r.raw, a.wx_at = r.obs""", {"rows": wx}, write=True)
    g("""UNWIND $rows AS r MATCH (a:Airport {code: r.code}) WHERE r.cat IN $bad
         CREATE (l:Live {source: 'METAR', kind: 'Flight category ' + r.cat, reason: r.raw, detail: '', as_of: r.obs})-[:AT]->(a)""",
      {"rows": wx, "bad": sorted(CONSTRAINED)}, write=True)
    g("""UNWIND $rows AS r MATCH (a:Airport {code: r.code})
         CREATE (l:Live {source: 'TAF', kind: r.kind, reason: r.raw, detail: r.detail, as_of: r.as_of, starts: r.first})-[:AT]->(a)""",
      {"rows": list(fc)}, write=True)
    now = time.time()
    g("MERGE (f:LiveFeed {id: 'feed'}) SET f.fetched = $t, f.faa_time = $ft, f.faa_entries = $n, f.metars = $m",
      {"t": now, "ft": faa_time, "n": len(faa), "m": len(wx)}, write=True)
    return {"refreshed": True, "fetched": now, "faa_entries_national": len(faa), "faa_in_network": len(mine),
            "metars": len(wx), "forecast_hazards": len(fc)}
