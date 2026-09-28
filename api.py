"""FastAPI app: incident board, recommendations, resolutions, post-mortems, service memory, memory controls.

Two "brains" share the same code but have separate Hindsight banks and board state:
  * ``trained``: the main bank, taught by a month of on-call resolutions (the replay),
  * ``fresh``: an empty bank the demo story teaches live, so judges see learning happen.

Run:  uvicorn app.api:app --port 8020
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import llm
from .config import ROOT, get_settings
from .memory import create_memory_service
from .service import WarRoom
from .store import Store
from .triage import SEV_MATRIX

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

Brain = Literal["trained", "fresh"]
brains: dict[str, WarRoom] = {}
active: dict[str, str] = {"brain": "trained"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    s = get_settings()
    for name, bank, state in (("trained", s.bank_id, "state.json"), ("fresh", s.fresh_bank_id, "state-fresh.json")):
        memory = create_memory_service(bank)
        await memory.setup()
        brains[name] = WarRoom(Store(s.data_dir, s.var_dir / state), memory)
    yield


app = FastAPI(title="WarRoom", version="1.1", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5190", "http://127.0.0.1:5190"],
                   allow_methods=["*"], allow_headers=["*"])


def service() -> WarRoom:
    assert brains, "app not started"
    return brains[active["brain"]]


def _bank(brain: str) -> str:
    s = get_settings()
    return s.bank_id if brain == "trained" else s.fresh_bank_id


def _incident_or_404(incident_id: str) -> None:
    if incident_id not in service().store.by_id:
        raise HTTPException(404, f"unknown incident {incident_id}")


def _service_or_404(service_id: str) -> dict:
    s = service().store.services.get(service_id)
    if not s:
        raise HTTPException(404, f"unknown service {service_id}")
    return s


Action = Literal["rollback", "restart", "scale", "failover", "feature_flag", "vendor", "monitor", "suppress",
                 "merge", "escalate"]


class ResolveIn(BaseModel):
    action: Action
    note: str = Field("", max_length=2000)
    cause: str | None = Field(None, max_length=120)


class ToggleIn(BaseModel):
    enabled: bool


class BrainIn(BaseModel):
    brain: Brain


class ReplayIn(BaseModel):
    through_day: int = Field(30, ge=1, le=30)


class ResetIn(BaseModel):
    memory: bool = True
    queue: bool = True


@app.get("/api/health")
async def health() -> dict:
    s = get_settings()
    return {"memory_backend": service().memory.backend.name, "memory_enabled": service().memory.enabled,
            "brain": active["brain"], "bank_id": _bank(active["brain"]),
            "llm_configured": bool(s.groq_api_key), "models": llm.models()}


@app.post("/api/brain")
async def set_brain(body: BrainIn) -> dict:
    """Switch which brain (bank + board state) the whole UI talks to."""
    active["brain"] = body.brain
    return await health()


@app.get("/api/severity-matrix")
async def severity_matrix() -> dict:
    return SEV_MATRIX


@app.get("/api/incidents")
async def list_incidents() -> list[dict]:
    return service().board()


@app.get("/api/incidents/{incident_id}")
async def get_incident(incident_id: str) -> dict:
    _incident_or_404(incident_id)
    return service().store.context(incident_id)


@app.post("/api/incidents/{incident_id}/suggest")
async def suggest(incident_id: str) -> dict:
    _incident_or_404(incident_id)
    return await service().suggest(incident_id)


@app.post("/api/incidents/{incident_id}/resolve")
async def resolve(incident_id: str, body: ResolveIn) -> dict:
    _incident_or_404(incident_id)
    try:
        return await service().resolve(incident_id, body.action, body.note, body.cause)
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.post("/api/incidents/{incident_id}/postmortem")
async def postmortem(incident_id: str) -> dict:
    _incident_or_404(incident_id)
    try:
        return {"postmortem": await service().postmortem(incident_id)}
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/api/story")
async def story() -> dict:
    """Everything the guided demo needs: key incidents' state in both brains + the trained month's metrics."""
    def pick(brain: str, iid: str) -> dict | None:
        st = brains[brain].store
        return st.context(iid) if iid in st.by_id else None

    return {
        "fresh": {i: pick("fresh", i) for i in STORY_FRESH},
        "trained": {i: pick("trained", i) for i in STORY_TRAINED},
        "metrics": brains["trained"].metrics(),
        "brain": active["brain"],
    }


# The incidents the story walks through (see README "Demo story").
STORY_FRESH = ["INC-0902-02", "INC-0909-01"]          # checkout-api bad deploy, then the same again a week later
STORY_TRAINED = ["INC-0922-01", "INC-0926-03", "INC-0929-01", "INC-0916-02"]


@app.get("/api/services")
async def services() -> list[dict]:
    st = service().store
    counts: dict[str, dict] = {}
    for i in st.incidents:
        n = counts.setdefault(i["service_id"], {"incidents": 0, "resolved": 0})
        n["incidents"] += 1
        n["resolved"] += i["id"] in st.state["resolutions"]
    return [{**s, **counts.get(s["id"], {})} for s in st.services.values()]


@app.get("/api/services/{service_id}/memories")
async def service_memories(service_id: str) -> list[dict]:
    s = _service_or_404(service_id)
    return [m.to_dict() for m in await service().memory.list_service_memories(s)]


@app.get("/api/services/{service_id}/profile")
async def service_profile(service_id: str) -> dict:
    s = _service_or_404(service_id)
    return {"service": s, "profile": await service().memory.build_service_profile(s),
            "backend": service().memory.backend.name}


@app.get("/api/insights")
async def insights() -> dict:
    return {"insights": await service().memory.org_insights(), "backend": service().memory.backend.name}


@app.get("/api/memory")
async def memory_state() -> dict:
    return {"enabled": service().memory.enabled, "backend": service().memory.backend.name}


@app.post("/api/memory/toggle")
async def memory_toggle(body: ToggleIn) -> dict:
    for b in brains.values():  # one switch for the whole demo
        b.memory.enabled = body.enabled
    return await memory_state()


@app.post("/api/memory/seed")
async def memory_seed() -> dict:
    return {"retained": await service().seed_history()}


@app.post("/api/reset")
async def reset(body: ResetIn) -> dict:
    if service().replay_status["running"]:
        raise HTTPException(409, "replay in progress")
    if body.memory:
        await service().memory.reset()
    if body.queue:
        service().store.reset()
    return {"ok": True}


@app.post("/api/replay")
async def replay(body: ReplayIn) -> dict:
    return service().start_replay(body.through_day)


@app.get("/api/replay")
async def replay_status() -> dict:
    return service().replay_status


@app.get("/api/metrics")
async def metrics() -> dict:
    return service().metrics()


# Serve the built frontend (npm run build) from the same process for demos.
_dist = ROOT / "frontend" / "dist"
if _dist.exists():
    app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        return FileResponse(_dist / "index.html")
