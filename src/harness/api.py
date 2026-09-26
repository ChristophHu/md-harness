"""HTTP API exposing task discovery and orchestration with Swagger UI."""

from __future__ import annotations

import hmac
import ipaddress
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from harness.application import Application, build_orchestrator


def create_app(
    config_path: str | Path,
    *,
    bearer_token: str | None = None,
    host: str = "127.0.0.1",
) -> Any:
    """Create a FastAPI app; FastAPI remains an optional installation extra."""
    try:
        from fastapi import FastAPI, Header, HTTPException, Query
    except ImportError as error:
        raise RuntimeError(
            "The API requires the optional dependencies; install md-harness[api]."
        ) from error

    token = (
        bearer_token
        if bearer_token is not None
        else os.environ.get("HARNESS_API_TOKEN")
    )
    if not _is_loopback(host) and not token:
        raise ValueError("HARNESS_API_TOKEN is required when binding beyond localhost")

    application: Application | None = None
    access_lock = threading.RLock()

    @asynccontextmanager
    async def lifespan(_app: Any):
        nonlocal application
        orchestrator, connection = build_orchestrator(config_path, api_mode=True)
        application = Application(orchestrator, connection)
        try:
            yield
        finally:
            with access_lock:
                application.close()
                application = None

    app = FastAPI(
        title="MD Harness API",
        description="Inspect, search, and run harness tasks.",
        version="0.1.0",
        lifespan=lifespan,
    )

    def require_auth(authorization: str | None) -> None:
        if token is None:
            return
        supplied = authorization or ""
        scheme, separator, credential = supplied.partition(" ")
        if (
            not separator
            or scheme.lower() != "bearer"
            or not hmac.compare_digest(credential, token)
        ):
            raise HTTPException(status_code=401, detail="invalid bearer token")

    def get_application(authorization: str | None) -> Application:
        require_auth(authorization)
        if application is None:
            raise HTTPException(status_code=503, detail="application is not ready")
        return application

    @app.get("/tasks", tags=["tasks"])
    def search_tasks(
        q: str | None = Query(default=None, max_length=200),
        status: str | None = Query(default=None, max_length=32),
        project_id: int | None = Query(default=None, ge=1),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        app_instance = get_application(authorization)
        with access_lock:
            rows, total = app_instance.orchestrator.task_store.search(
                query=q,
                status=status,
                project_id=project_id,
                limit=limit,
                offset=offset,
            )
            return {"items": [_task_dict(row) for row in rows], "total": total}

    @app.get("/tasks/{task_id}", tags=["tasks"])
    def get_task(
        task_id: int,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        app_instance = get_application(authorization)
        with access_lock:
            task = app_instance.orchestrator.task_store.get(task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="task not found")
            return _task_dict(task)

    @app.post("/tasks/{task_id}/run", tags=["tasks"])
    def run_task(
        task_id: int,
        authorization: str | None = Header(default=None),
    ) -> dict[str, str | int | None]:
        app_instance = get_application(authorization)
        with access_lock:
            task = app_instance.orchestrator.task_store.get(task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="task not found")
            result = app_instance.orchestrator.run(task_id)
            return {
                "task_id": task_id,
                "status": result.status.value,
                "message": result.message,
            }

    return app


def _task_dict(task: Any) -> dict[str, Any]:
    return {key: task[key] for key in task.keys()}  # noqa: SIM118


def _is_loopback(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False
