import asyncio
import sys
import types
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from harness import api


class FakeHTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail


class FakeFastAPI:
    def __init__(self, **kwargs):
        self.options = kwargs
        self.routes = {}

    def get(self, path, **_kwargs):
        return self._register("GET", path)

    def post(self, path, **_kwargs):
        return self._register("POST", path)

    def _register(self, method, path):
        def decorator(function):
            self.routes[(method, path)] = function
            return function

        return decorator


def install_fake_fastapi(monkeypatch):
    module = types.ModuleType("fastapi")
    module.FastAPI = FakeFastAPI
    module.Header = lambda default=None, **_kwargs: default
    module.Query = lambda default=None, **_kwargs: default
    module.HTTPException = FakeHTTPException
    monkeypatch.setitem(sys.modules, "fastapi", module)


def test_create_app_requires_optional_dependencies(monkeypatch):
    monkeypatch.setitem(sys.modules, "fastapi", None)
    with pytest.raises(RuntimeError, match=r"install md-harness\[api\]"):
        api.create_app("config.yaml")


@pytest.mark.parametrize("host", ["0.0.0.0", "not-an-ip"])
def test_non_loopback_bind_requires_token(monkeypatch, host):
    install_fake_fastapi(monkeypatch)
    monkeypatch.delenv("HARNESS_API_TOKEN", raising=False)
    with pytest.raises(ValueError, match="HARNESS_API_TOKEN"):
        api.create_app("config.yaml", host=host)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
def test_loopback_bind_does_not_require_token(monkeypatch, host):
    install_fake_fastapi(monkeypatch)
    assert isinstance(api.create_app("config.yaml", host=host), FakeFastAPI)


def test_api_search_detail_and_run_routes(monkeypatch):
    install_fake_fastapi(monkeypatch)
    task = {"id": 4, "title": "test"}
    task_store = Mock()
    task_store.search.return_value = ([task], 1)
    task_store.get.return_value = task
    orchestrator = Mock(task_store=task_store)
    orchestrator.run.return_value = SimpleNamespace(
        status=SimpleNamespace(value="waiting"), message="needs review"
    )
    connection = Mock()
    monkeypatch.setattr(
        api,
        "build_orchestrator",
        lambda path, *, api_mode: (orchestrator, connection),
    )
    app = api.create_app("config.yaml")

    async def _exercise():
        async with app.options["lifespan"](app):
            rows = app.routes[("GET", "/tasks")]("needle", "ready", 3, 10, 5, None)
            assert rows == {"items": [task], "total": 1}
            task_store.search.assert_called_once_with(
                query="needle", status="ready", project_id=3, limit=10, offset=5
            )
            assert app.routes[("GET", "/tasks/{task_id}")](4, None) == task
            assert app.routes[("POST", "/tasks/{task_id}/run")](4, None) == {
                "task_id": 4,
                "status": "waiting",
                "message": "needs review",
            }
            orchestrator.run.assert_called_once_with(4)

    asyncio.run(_exercise())
    connection.close.assert_called_once_with()


def test_routes_reject_missing_tasks(monkeypatch):
    install_fake_fastapi(monkeypatch)
    task_store = Mock()
    task_store.get.return_value = None
    orchestrator = Mock(task_store=task_store)
    monkeypatch.setattr(
        api,
        "build_orchestrator",
        lambda *_args, **_kwargs: (orchestrator, Mock()),
    )
    app = api.create_app("config.yaml")

    async def _exercise():
        async with app.options["lifespan"](app):
            with pytest.raises(FakeHTTPException) as detail_error:
                app.routes[("GET", "/tasks/{task_id}")](99, None)
            assert detail_error.value.status_code == 404
            with pytest.raises(FakeHTTPException) as run_error:
                app.routes[("POST", "/tasks/{task_id}/run")](99, None)
            assert run_error.value.status_code == 404

    asyncio.run(_exercise())
    orchestrator.run.assert_not_called()


def test_api_authentication_and_not_ready(monkeypatch):
    install_fake_fastapi(monkeypatch)
    app = api.create_app("config.yaml", bearer_token="secret")
    listing = app.routes[("GET", "/tasks")]
    with pytest.raises(FakeHTTPException) as error:
        listing(None, None, None, 50, 0, "Basic secret")
    assert error.value.status_code == 401
    with pytest.raises(FakeHTTPException) as not_ready:
        listing(None, None, None, 50, 0, "Bearer secret")
    assert not_ready.value.status_code == 503


def test_auth_accepts_case_insensitive_bearer(monkeypatch):
    install_fake_fastapi(monkeypatch)
    store = Mock()
    store.search.return_value = ([], 0)
    monkeypatch.setattr(
        api,
        "build_orchestrator",
        lambda *_args, **_kwargs: (
            SimpleNamespace(task_store=store),
            Mock(),
        ),
    )
    app = api.create_app("config.yaml", bearer_token="secret")

    async def _exercise():
        async with app.options["lifespan"](app):
            return app.routes[("GET", "/tasks")](
                None, None, None, 50, 0, "bEaReR secret"
            )

    assert asyncio.run(_exercise()) == {"items": [], "total": 0}


def test_task_dict_and_host_validation():
    class Row:
        def keys(self):
            return ["id"]

        def __getitem__(self, _key):
            return 2

    assert api._task_dict(Row()) == {"id": 2}
    assert api._is_loopback("localhost")
    assert api._is_loopback("127.0.0.1")
    assert api._is_loopback("::1")
    assert not api._is_loopback("invalid-host")


def test_api_lifespan_closes_application_when_endpoint_raises(monkeypatch):
    install_fake_fastapi(monkeypatch)
    connection = Mock()
    monkeypatch.setattr(
        api,
        "build_orchestrator",
        lambda *_args, **_kwargs: (Mock(), connection),
    )
    app = api.create_app("config.yaml")

    async def _exercise():
        with pytest.raises(RuntimeError, match="boom"):
            async with app.options["lifespan"](app):
                raise RuntimeError("boom")

    asyncio.run(_exercise())
    connection.close.assert_called_once_with()
