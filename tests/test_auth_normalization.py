from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from chutes.middleware.auth_normalization import AuthNormalizationMiddleware

async def echo_auth(request):
    return JSONResponse({"auth": request.headers.get("authorization")})

def make_app():
    app = Starlette(routes=[Route("/v1/models", echo_auth), Route("/healthz", echo_auth)])
    app.add_middleware(AuthNormalizationMiddleware, enabled_paths_prefix=("/v1/",))
    return app

def test_bearer_passthrough():
    with TestClient(make_app()) as c:
        r = c.get("/v1/models", headers={"Authorization": "Bearer abc"})
        assert r.status_code == 200
        assert r.json()["auth"] == "Bearer abc"
        assert "deprecation" not in r.headers

def test_x_api_key_alias_normalized():
    with TestClient(make_app()) as c:
        r = c.get("/v1/models", headers={"x-api-key": "abc"})
        assert r.status_code == 200
        assert r.json()["auth"] == "Bearer abc"
        assert r.headers.get("deprecation") == "true"

def test_basic_alias_normalized():
    with TestClient(make_app()) as c:
        r = c.get("/v1/models", headers={"Authorization": "Basic abc"})
        assert r.status_code == 200
        assert r.json()["auth"] == "Bearer abc"
        assert r.headers.get("deprecation") == "true"

def test_raw_authorization_alias_normalized():
    with TestClient(make_app()) as c:
        r = c.get("/v1/models", headers={"Authorization": "abc"})
        assert r.status_code == 200
        assert r.json()["auth"] == "Bearer abc"
        assert r.headers.get("deprecation") == "true"

def test_non_v1_path_untouched():
    with TestClient(make_app()) as c:
        r = c.get("/healthz", headers={"x-api-key": "abc"})
        assert r.status_code == 200
        assert r.json()["auth"] is None
