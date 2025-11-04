import scillm
import httpx


def test_models_probe_success(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        return httpx.Response(200, json={"data": []}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    out = scillm.models_probe("https://api.example.com", api_key="k")
    assert out["ok"] is True
    assert out["status"] == 200


def test_chat_probe_json_failure(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        return httpx.Response(401, text="unauthorized", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    out = scillm.chat_probe_json("https://api.example.com", api_key="bad", model="gpt-x")
    assert out["ok"] is False
    assert out["status"] == 401
