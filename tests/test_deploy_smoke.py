"""deploy/steps.py smoke: the public URL must be this service's MCP endpoint, and the
registration must come from the process this deploy started. HTTP and docker are fakes."""
import email.message
import importlib.util
import io
import json
import types
import urllib.error
from pathlib import Path

import pytest

import boring_deploy

ROOT = Path(__file__).resolve().parents[1]
URL = "https://crocodile-url.example.test/services/canvas/mcp"
METADATA = "https://crocodile-url.example.test/.well-known/oauth-protected-resource/services/canvas/mcp"
STARTED = "2026-10-18T10:00:00.123456789Z"


def load_steps():
    spec = importlib.util.spec_from_file_location("service_steps_under_test", ROOT / "deploy" / "steps.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def headers(**values):
    message = email.message.Message()
    for key, value in values.items():
        message[key.replace("_", "-")] = value
    return message


class Answer:
    def __init__(self, status, body=b"", **head):
        self.status, self.body, self.headers = status, body, headers(**head)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, limit=-1):
        return self.body


def challenge(resource_metadata):
    return f'Bearer error="invalid_token", error_description="Authentication required", resource_metadata="{resource_metadata}"'


def web(answers):
    """urlopen fake: url -> Answer; 4xx/5xx are raised as urllib does."""
    def urlopen(url, timeout=None):
        answer = answers.get(url, Answer(404, b"not found"))
        if answer.status >= 400:
            raise urllib.error.HTTPError(url, answer.status, "fake", answer.headers, io.BytesIO(answer.body))
        return answer
    return urlopen


RIGHT = {URL: Answer(401, WWW_Authenticate=challenge(METADATA)),
         METADATA: Answer(200, json.dumps({"resource": URL, "authorization_servers": ["https://x.example.test"]}).encode())}


@pytest.fixture
def steps(monkeypatch):
    module = load_steps()
    monkeypatch.setattr(module, "SMOKE_SECONDS", 0)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    return module


def run_smoke(steps, monkeypatch, answers, logs="", pond=True):
    calls = []
    config = {"url": URL, **({"pond": {"present": True}} if pond else {})}

    def compose(ctx, *args, **kwargs):
        calls.append(args)
        if args[0] == "exec":
            return types.SimpleNamespace(stdout=json.dumps(config))
        if args[0] == "ps":
            return types.SimpleNamespace(stdout="0123abcd\n")
        if args[0] == "logs":
            return types.SimpleNamespace(stdout=logs)
        raise AssertionError(args)

    def run(*command, **kwargs):
        assert command[:2] == ("docker", "inspect") and command[-1] == "0123abcd", command
        return types.SimpleNamespace(stdout=STARTED + "\n")

    monkeypatch.setattr(steps, "_service", lambda ctx: ("canvas", {"command": ["--config", "/config/config.json"]}))
    monkeypatch.setattr(steps, "_compose", compose)
    monkeypatch.setattr(steps.urllib.request, "urlopen", web(answers))
    said = []
    steps.smoke(types.SimpleNamespace(home=Path("/opt/boring-canvas"), run=run, log=said.append))
    return said, calls


FRESH = f"2026-10-18T10:00:03.000000000Z 2026-10-18 10:00:03,000 boring_service_kit.pond INFO MCP registration: registered, sources []\n"


def test_right_endpoint_and_fresh_registration_pass(steps, monkeypatch):
    said, calls = run_smoke(steps, monkeypatch, RIGHT, FRESH)
    assert any("is this service's MCP endpoint" in line for line in said)
    assert any(line.startswith("Pond: canvas") for line in said)
    logs_call = next(c for c in calls if c[0] == "logs")
    assert logs_call[1:3] == ("--since", STARTED) and "--timestamps" in logs_call and logs_call[-1] == "canvas"


def test_wrong_route_404_fails(steps, monkeypatch):
    # the reviewer's probe: a missing proxy route plus a registration line
    with pytest.raises(boring_deploy.Failure, match="answers 404"):
        run_smoke(steps, monkeypatch, {}, FRESH)


def test_generic_html_200_fails(steps, monkeypatch):
    page = {URL: Answer(200, b"<html><body>Welcome</body></html>", Content_Type="text/html")}
    with pytest.raises(boring_deploy.Failure, match="answers 200"):
        run_smoke(steps, monkeypatch, page, FRESH)


def test_401_for_another_resource_fails(steps, monkeypatch):
    other = "https://crocodile-url.example.test/.well-known/oauth-protected-resource/knowledge/mcp"
    with pytest.raises(boring_deploy.Failure, match="another resource"):
        run_smoke(steps, monkeypatch, {**RIGHT, URL: Answer(401, WWW_Authenticate=challenge(other))}, FRESH)


def test_401_without_challenge_fails(steps, monkeypatch):
    with pytest.raises(boring_deploy.Failure, match="another resource"):
        run_smoke(steps, monkeypatch, {**RIGHT, URL: Answer(401)}, FRESH)


def test_metadata_naming_another_resource_fails(steps, monkeypatch):
    lying = Answer(200, json.dumps({"resource": "https://crocodile-url.example.test/knowledge/mcp"}).encode())
    with pytest.raises(boring_deploy.Failure, match="without resource"):
        run_smoke(steps, monkeypatch, {**RIGHT, METADATA: lying}, FRESH)


def test_registration_before_this_start_fails(steps, monkeypatch):
    stale = FRESH.replace("2026-10-18T10:00:03", "2026-10-18T09:59:59")
    with pytest.raises(boring_deploy.Failure, match="no Pond registration by canvas since its start"):
        run_smoke(steps, monkeypatch, RIGHT, stale)


def test_latest_outcome_since_start_decides(steps, monkeypatch):
    lost = FRESH + FRESH.replace("10:00:03", "10:00:09").replace("registered, sources []", "refused with HTTP 403")
    with pytest.raises(boring_deploy.Failure, match="refused with HTTP 403"):
        run_smoke(steps, monkeypatch, RIGHT, lost)


def test_no_pond_needs_no_registration(steps, monkeypatch):
    said, calls = run_smoke(steps, monkeypatch, RIGHT, "", pond=False)
    assert not any(c[0] in ("logs", "ps") for c in calls)


def test_metadata_url_follows_the_sdk():
    steps = load_steps()
    assert steps.metadata_url(URL) == METADATA
    assert steps.metadata_url("https://knowledge-url.example.test/") == \
        "https://knowledge-url.example.test/.well-known/oauth-protected-resource"
