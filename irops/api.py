"""HTTP API + static UI.   uv run uvicorn irops.api:app --port 8000"""
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from irops import agents, llm
from irops.scenario import SCENARIOS
from irops.tools import Tools

ROOT = Path(__file__).resolve().parent.parent
TENANT = os.getenv("IROPS_GRAPH", "irops_WN")
app = FastAPI(title="IROPS Brain")
app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")


class Run(BaseModel):
    scenario: str = "den-elliott"


class Ask(BaseModel):
    question: str


@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/api/scenarios")
def scenarios():
    return {"scenarios": [{"id": k, "title": v["title"], "story": v["story"]} for k, v in SCENARIOS.items()],
            "claude": llm.available()}


@app.post("/api/run")
def run(body: Run):
    if body.scenario not in SCENARIOS:
        raise HTTPException(404, "unknown scenario")
    return agents.run(body.scenario, TENANT)


@app.post("/api/decisions/{decision_id}/approve")
def approve(decision_id: str):
    return agents.approve(decision_id, TENANT)


@app.post("/api/decisions/{decision_id}/reject")
def reject(decision_id: str):
    return agents.reject(decision_id, TENANT)


@app.post("/api/ask")
def ask(body: Ask):
    if not body.question.strip() or len(body.question) > 500:
        raise HTTPException(400, "ask a question of up to 500 characters")
    return llm.ask(body.question, TENANT)


@app.get("/api/compare")
def compare(flight: str = "2022-12-22-WN2100-DEN", delay: int = 180):
    """The same question asked of a flat record lookup and of the graph."""
    t = Tools(TENANT)
    rec = t.resolve_flight(flight)
    if rec["outcome"] != "answered" or len(rec["rows"]) != 1:
        raise HTTPException(404, "flight not found")
    graph = t.propagate_delay([{"flight": rec["rows"][0]["id"], "delay": delay}], 8)
    return {"flight": rec["rows"][0], "delay": delay,
            "flat": {"flights_affected": 1, "pax_misconnecting": "unknown", "crews_illegal": "unknown",
                     "why": "A record lookup sees one row: this flight is late."},
            "graph": graph["summary"], "paths": graph["paths"][:8], "rows": graph["rows"][:8]}


@app.get("/api/backtest")
def backtest():
    p = ROOT / "eval" / "results.json"
    return json.loads(p.read_text()) if p.exists() else {"results": []}
