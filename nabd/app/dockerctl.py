"""Thin wrapper over the Docker API for the compose project's containers."""

import json
import threading
from datetime import datetime, timezone

import docker
from docker.errors import DockerException, NotFound

from .settings import COMPOSE_PROJECT

_client = None
_lock = threading.Lock()


def client():
    global _client
    with _lock:
        if _client is None:
            _client = docker.from_env(timeout=20)
        return _client


def container(service: str):
    found = client().containers.list(all=True, filters={"label": [
        f"com.docker.compose.project={COMPOSE_PROJECT}",
        f"com.docker.compose.service={service}",
    ]})
    return found[0] if found else None


def _summarise(c) -> dict:
    state = c.attrs.get("State", {})
    host = c.attrs.get("HostConfig", {})
    return {
        "status": state.get("Status"),
        "running": bool(state.get("Running")),
        "exit_code": state.get("ExitCode"),
        "oom_killed": bool(state.get("OOMKilled")),
        "started_at": state.get("StartedAt"),
        "finished_at": state.get("FinishedAt"),
        "restart_count": c.attrs.get("RestartCount", 0),
        "memory_limit_mb": round((host.get("Memory") or 0) / 1048576) or None,
    }


def states() -> dict[str, dict]:
    try:
        found = client().containers.list(all=True, filters={"label": f"com.docker.compose.project={COMPOSE_PROJECT}"})
    except DockerException as exc:
        return {"_error": {"status": f"docker unavailable: {exc}"}}
    return {c.labels.get("com.docker.compose.service", c.name): _summarise(c) for c in found}


def inspect(service: str) -> dict:
    c = container(service)
    if c is None:
        return {"service": service, "error": "no such container"}
    out = {"service": service, **_summarise(c)}
    if out["running"]:
        try:
            stats = c.stats(stream=False)
            mem = stats.get("memory_stats", {})
            out["memory_usage_mb"] = round(mem.get("usage", 0) / 1048576, 1)
            cpu, pre = stats.get("cpu_stats", {}), stats.get("precpu_stats", {})
            cpu_delta = cpu.get("cpu_usage", {}).get("total_usage", 0) - pre.get("cpu_usage", {}).get("total_usage", 0)
            sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
            if cpu_delta > 0 and sys_delta > 0:
                out["cpu_percent_of_one_core"] = round(cpu_delta / sys_delta * cpu.get("online_cpus", 1) * 100, 1)
        except (DockerException, KeyError, TypeError):
            pass
    return out


def restart(service: str) -> None:
    c = container(service)
    if c is None:
        raise RuntimeError(f"no container for {service}")
    c.restart(timeout=3)


def start(service: str) -> None:
    c = container(service)
    if c is None:
        raise RuntimeError(f"no container for {service}")
    c.start()


def kill(service: str) -> None:
    c = container(service)
    if c is not None and c.status == "running":
        c.kill()


def logs(service: str, since_ts: float, tail: int = 20000) -> list[str]:
    c = container(service)
    if c is None:
        return []
    try:
        raw = c.logs(since=datetime.fromtimestamp(since_ts, tz=timezone.utc), tail=tail, stdout=True, stderr=True)
    except (DockerException, NotFound):
        return []
    return raw.decode("utf-8", errors="replace").splitlines()


def exec_probe(service: str, target: str) -> dict:
    c = container(service)
    if c is None or c.status != "running":
        return {"error": f"{service} is not running"}
    result = c.exec_run(["python", "/app/probe.py", target], demux=False)
    text = (result.output or b"").decode("utf-8", errors="replace").strip().splitlines()
    try:
        return json.loads(text[-1]) if text else {"error": "probe produced no output"}
    except json.JSONDecodeError:
        return {"error": "probe output unreadable", "raw": text[-3:]}
