"""Nabd's HTTP surface: the War Room UI, a Server-Sent Events stream, and the
small JSON API behind the chaos panel, approvals, postmortems and benchmark."""

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config_store, logs, memory, postmortem, schema, topology
from .agent import STAGES, agent
from .benchmark import benchmark
from .bus import bus
from .chaos import FAULTS, SCENARIOS, chaos
from .learned import learner
from .mizan import mizan
from .settings import runtime
from .telemetry import telemetry

UI_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "ui")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    config_store.init()
    await asyncio.to_thread(schema.migrate)
    existing = [int(m.group(1)) for pm in postmortem.load_all() if (m := re.match(r"INC-(\d+)", pm["id"] or ""))]
    agent.counter = max(existing, default=0)
    tasks = [asyncio.create_task(telemetry.run()), asyncio.create_task(agent.run()),
             asyncio.create_task(mizan.run()), asyncio.create_task(learner.run(_healthy))]
    bus.publish("info", title="Nabd is watching Dukkan")
    yield
    for task in tasks:
        task.cancel()


def _healthy() -> bool:
    """Laws are only learned from traffic nobody is breaking."""
    snap = telemetry.latest or {}
    return (agent.incident is None and not chaos.active and not snap.get("alerts")
            and (mizan.latest or {}).get("balanced", True))


app = FastAPI(title="Nabd", lifespan=lifespan)
app.mount("/ui", StaticFiles(directory=UI_DIR), name="ui")


@app.middleware("http")
async def fresh_ui(request, call_next):
    """The War Room must never run yesterday's script from the browser cache (it did, in a rehearsal)."""
    resp = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/ui/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/")
def index():
    return FileResponse(os.path.join(UI_DIR, "index.html"))


@app.get("/api/state")
def state():
    return {
        "settings": runtime.as_dict(),
        "agent": agent.state(),
        "chaos": chaos.snapshot(),
        "faults": {k: {kk: v.get(kk) for kk in ("title", "title_ar", "blurb", "held_out", "expect", "silent", "unseen", "benign", "blind")}
                   for k, v in FAULTS.items()},
        "scenarios": {k: {"title": v["title"], "faults": v["faults"]} for k, v in SCENARIOS.items()},
        "topology": topology.as_dict(),
        "stages": STAGES,
        "telemetry": (bus.latest_telemetry or {}).get("data"),
        "log_stats": logs.stats,
        "memory_size": len(memory.store.items),
    }


@app.get("/api/stream")
async def stream(request: Request):
    queue = bus.subscribe()

    async def events():
        try:
            for event in bus.history():
                yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
            if bus.latest_telemetry:
                yield f"data: {json.dumps(bus.latest_telemetry, ensure_ascii=False, default=str)}\n\n"
            yield 'data: {"type": "replay_done"}\n\n'
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- chaos

@app.post("/api/chaos/inject")
async def inject(body: dict):
    fault = body.get("fault")
    if fault not in FAULTS:
        raise HTTPException(404, f"unknown fault {fault}")
    return await chaos.inject(fault, hidden=bool(body.get("hidden")))


@app.post("/api/chaos/scenario")
async def scenario(body: dict):
    name = body.get("scenario")
    if name not in SCENARIOS:
        raise HTTPException(404, f"unknown scenario {name}")
    return await chaos.scenario(name)


@app.post("/api/chaos/mystery")
async def mystery():
    return await chaos.mystery()


@app.post("/api/chaos/reset")
async def reset():
    await chaos.reset()
    return {"reset": True}


# ---------------------------------------------------------------- agent

@app.post("/api/approve")
async def approve(body: dict):  # async: it sets an asyncio.Event, which must happen on the event loop
    decision = "approve" if body.get("decision") == "approve" else "reject"
    ok = agent.decide(str(body.get("approval_id")), decision, str(body.get("via", "button")))
    if not ok:
        raise HTTPException(404, "no pending approval with that id")
    return {"ok": True}


@app.post("/api/settings")
def update_settings(body: dict):
    if body.get("mode") in ("autopilot", "supervised"):
        runtime.mode = body["mode"]
    if body.get("engine") in ("auto", "core", "claude"):
        runtime.engine = body["engine"]
    for key in ("business_invariants", "learned_invariants"):
        if isinstance(body.get(key), bool):
            setattr(runtime, key, body[key])
    bus.publish("settings", settings=runtime.as_dict(), title=f"Settings: {runtime.mode}, engine {runtime.engine}")
    return runtime.as_dict()


@app.get("/api/learned")
def learned_laws():
    return {**learner.summary(), "list": [{"law": law.sentence(), "x": law.x, "y": law.y, "k": round(law.k, 3),
                                           "status": law.status, "threshold": round(law.threshold(), 3),
                                           "e": round(law.e, 3)} for law in learner.laws]}


@app.post("/api/learned/relearn")
def relearn():
    learner.relearn()
    return learner.summary()


@app.get("/api/postmortems")
def postmortems():
    return postmortem.load_all()


@app.get("/api/postmortems/{incident_id}")
def get_postmortem(incident_id: str):
    pm = postmortem.load(incident_id)
    if pm is None:
        raise HTTPException(404, "no such postmortem")
    return pm


@app.get("/api/postmortems/{incident_id}/markdown")
def postmortem_markdown(incident_id: str, lang: str = "en"):
    pm = postmortem.load(incident_id)
    if pm is None:
        raise HTTPException(404, "no such postmortem")
    text = pm["markdown"].get("ar" if lang == "ar" else "en", "")
    return PlainTextResponse(text, media_type="text/markdown; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{pm["id"]}-{lang}.md"'})


@app.get("/api/incidents/{incident_id}/events")
def incident_events(incident_id: str):
    return bus.history(incident_id)


@app.get("/api/memory")
def get_memory():
    return memory.store.items


@app.post("/api/memory/clear")
def clear_memory():
    memory.store.clear()
    bus.publish("info", title="Incident memory cleared")
    return {"cleared": True}


@app.get("/api/benchmark")
def get_benchmark():
    return benchmark.view()


@app.post("/api/benchmark/start")
async def start_benchmark(body: dict):  # async: it schedules a task on the event loop
    ok = benchmark.start(body.get("scenarios"), int(body.get("runs", 1)), bool(body.get("baseline", True)),
                         body.get("variants"))
    if not ok:
        raise HTTPException(409, "a benchmark is already running")
    return benchmark.view()


@app.post("/api/benchmark/stop")
async def stop_benchmark():
    benchmark.stop()
    return {"stopping": True}
