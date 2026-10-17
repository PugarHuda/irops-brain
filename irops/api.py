"""HTTP API + static UI + MCP endpoint.   uv run uvicorn irops.api:app --port 8000"""
import contextlib
import json
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

from irops import agents, llm
from irops.mcp_server import mcp
from irops.scenario import SCENARIOS
from irops.tools import Tools

ROOT = Path(__file__).resolve().parent.parent
TENANTS = sorted({s["tenant"] for s in SCENARIOS.values()})  # one isolated graph per airline
# ponytail: one global lock. Scenarios and approvals rewrite the shared operational state, so two visitors
# running at once would interleave; per-tenant locks if throughput ever matters.
WRITE = threading.Lock()

# read-only tools, public, no cookies: DNS-rebinding protection guards localhost servers, not this one
mcp_app = mcp.streamable_http_app(streamable_http_path="/", stateless_http=True, json_response=True,
                                  transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))


def ensure_graphs():
    # The FalkorDB Cloud free tier keeps no data across a restart; rebuild any graph that came back empty.
    for tenant in TENANTS:
        try:
            empty = not Tools(tenant).g.ro_query("MATCH (f:Flight) RETURN f LIMIT 1").result_set
        except Exception:  # graph key missing altogether
            empty = True
        if empty:
            from irops import ingest
            ingest.load(tenant)


@contextlib.asynccontextmanager
async def lifespan(_app):
    ensure_graphs()
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="IROPS Brain", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")
app.mount("/mcp", mcp_app)


def tenant_of(t: str):
    if t not in TENANTS:
        raise HTTPException(404, f"unknown airline graph; one of {TENANTS}")
    return t


class Run(BaseModel):
    scenario: str = "den-elliott"


class Ask(BaseModel):
    question: str = Field(max_length=500)
    tenant: str = "irops_WN"


@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/api/scenarios")
def scenarios():
    return {"scenarios": [{"id": k, "title": v["title"], "story": v["story"], "tenant": v["tenant"]}
                          for k, v in SCENARIOS.items()], "claude": llm.available()}


@app.post("/api/run")
def run(body: Run):
    if body.scenario not in SCENARIOS:
        raise HTTPException(404, "unknown scenario")
    with WRITE:
        ensure_graphs()  # the database may have restarted (and come back empty) since this process started
        return agents.run(body.scenario)


@app.post("/api/decisions/{decision_id}/approve")
def approve(decision_id: str, tenant: str = "irops_WN"):
    with WRITE:
        return agents.approve(decision_id, tenant_of(tenant))


@app.post("/api/decisions/{decision_id}/reject")
def reject(decision_id: str, tenant: str = "irops_WN"):
    with WRITE:
        return agents.reject(decision_id, tenant_of(tenant))


@app.post("/api/reset")
def reset(tenant: str = "irops_WN"):
    """Undo every approved change in one airline's graph (memory is kept)."""
    with WRITE:
        Tools(tenant_of(tenant)).reset_ops()
    return {"ok": True}


@app.post("/api/ask")
def ask(body: Ask):
    if not body.question.strip():
        raise HTTPException(400, "ask a question of up to 500 characters")
    return llm.ask(body.question, tenant_of(body.tenant))


@app.get("/api/compare")
def compare(flight: str = "2022-12-22-WN2100-DEN", delay: int = 180, tenant: str = "irops_WN"):
    """The same question asked of a flat record lookup and of the graph."""
    t = Tools(tenant_of(tenant))
    rec = t.resolve_flight(flight)
    if rec["outcome"] != "answered" or len(rec["rows"]) != 1:
        raise HTTPException(404, "flight not found")
    graph = t.propagate_delay([{"flight": rec["rows"][0]["id"], "delay": max(0, min(delay, 1440))}], 8)
    return {"flight": rec["rows"][0], "delay": delay,
            "flat": {"flights_affected": 1, "pax_misconnecting": "unknown", "crews_illegal": "unknown",
                     "why": "A record lookup sees one row: this flight is late."},
            "graph": graph["summary"], "paths": graph["paths"][:8], "rows": graph["rows"][:8]}


@app.get("/api/backtest")
def backtest():
    p = ROOT / "eval" / "results.json"
    return json.loads(p.read_text()) if p.exists() else {"results": []}
