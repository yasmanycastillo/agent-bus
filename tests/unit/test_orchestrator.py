from __future__ import annotations

import json
from pathlib import Path
import httpx
import pytest

from agent_bus.orchestrator.client import (
    InferenceClient,
    InferenceTimeoutError,
)
from agent_bus.orchestrator.config import (
    DEFAULT_BASE_URLS,
    OrchestratorConfig,
)
from agent_bus.orchestrator.hermes import HermesOrchestrator
from agent_bus.orchestrator.schema import (
    PlanValidationError,
    TaskBreakdownPlan,
    parse_and_validate_plan,
    validate_breakdown_dict,
)


# =========================================================================
# 1. Secret-free configuration tests
# =========================================================================

def test_config_from_env_all_providers(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HERMES_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    cfg_openai = OrchestratorConfig.from_env()
    assert cfg_openai.provider == "openai"
    assert cfg_openai.base_url == DEFAULT_BASE_URLS["openai"]
    assert cfg_openai.api_key == "sk-openai-test"

    monkeypatch.setenv("HERMES_PROVIDER", "openrouter")
    monkeypatch.delenv("HERMES_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    cfg_or = OrchestratorConfig.from_env()
    assert cfg_or.provider == "openrouter"
    assert cfg_or.base_url == DEFAULT_BASE_URLS["openrouter"]
    assert cfg_or.api_key == "sk-or-test"

    monkeypatch.setenv("HERMES_PROVIDER", "vllm")
    monkeypatch.setenv("VLLM_API_KEY", "token-vllm")
    cfg_vllm = OrchestratorConfig.from_env()
    assert cfg_vllm.provider == "vllm"
    assert cfg_vllm.base_url == DEFAULT_BASE_URLS["vllm"]
    assert cfg_vllm.api_key == "token-vllm"

    monkeypatch.setenv("HERMES_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_API_KEY", "token-ollama")
    cfg_ollama = OrchestratorConfig.from_env()
    assert cfg_ollama.provider == "ollama"
    assert cfg_ollama.base_url == DEFAULT_BASE_URLS["ollama"]


def test_hermes_api_key_precedence(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HERMES_API_KEY", "hermes-master-key")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    cfg = OrchestratorConfig.from_env(provider="openai")
    assert cfg.api_key == "hermes-master-key"


def test_custom_endpoint_and_model_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HERMES_ENDPOINT", "http://custom-vllm:9000/v1")
    monkeypatch.setenv("HERMES_MODEL", "custom-hermes-3-70b")
    monkeypatch.setenv("HERMES_TIMEOUT", "120")
    monkeypatch.setenv("HERMES_TEMPERATURE", "0.7")
    cfg = OrchestratorConfig.from_env(provider="custom")
    assert cfg.base_url == "http://custom-vllm:9000/v1"
    assert cfg.model == "custom-hermes-3-70b"
    assert cfg.timeout == 120.0
    assert cfg.temperature == 0.7


def test_reject_secrets_in_tracked_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Simulate a file that is tracked by git
    config_file = tmp_path / "tracked_orchestrator.yaml"
    config_file.write_text("provider: openai\napi_key: sk-secret-leak\nmodel: gpt-4o\n")

    # Monkeypatch is_tracked_by_git to return True
    monkeypatch.setattr("agent_bus.orchestrator.config.is_tracked_by_git", lambda p: True)

    with pytest.raises(ValueError) as excinfo:
        OrchestratorConfig.from_file(config_file)
    assert "Refusing to load secrets from git-tracked file" in str(excinfo.value)
    assert "Secrets must be loaded from environment variables" in str(excinfo.value)


def test_allow_tracked_config_without_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_file = tmp_path / "tracked_safe.yaml"
    config_file.write_text("provider: vllm\nmodel: hermes-3\nendpoint: http://localhost:8000/v1\n")

    monkeypatch.setattr("agent_bus.orchestrator.config.is_tracked_by_git", lambda p: True)
    monkeypatch.setenv("VLLM_API_KEY", "env-secret-token")

    cfg = OrchestratorConfig.from_file(config_file)
    assert cfg.provider == "vllm"
    assert cfg.model == "hermes-3"
    assert cfg.api_key == "env-secret-token"


# =========================================================================
# 2. Strict JSON schema validation tests
# =========================================================================

def test_schema_valid_breakdown():
    valid_data = {
        "objective": "Build user auth system",
        "summary": "Implement database schema and login endpoints",
        "operation_key": "op-auth-1",
        "tasks": [
            {
                "task_id": "auth-db",
                "title": "Create users table",
                "description": "SQL migration for users",
                "acceptance_criteria": ["Table exists", "Email is unique"],
                "test_cmd": ["pytest", "tests/unit/test_db.py"],
                "depends_on": [],
            },
            {
                "task_id": "auth-api",
                "title": "Create login endpoint",
                "description": "POST /login endpoint returning JWT",
                "acceptance_criteria": ["JWT returned on 200", "401 on invalid pass"],
                "test_cmd": ["pytest", "tests/integration/test_auth.py"],
                "depends_on": ["auth-db"],
            },
        ],
    }

    plan = validate_breakdown_dict(valid_data)
    assert isinstance(plan, TaskBreakdownPlan)
    assert plan.objective == "Build user auth system"
    assert len(plan.tasks) == 2
    assert plan.tasks[0].task_id == "auth-db"
    assert plan.tasks[1].depends_on == ["auth-db"]


def test_extract_markdown_json():
    raw_markdown = """```json
{
  "objective": "Test objective",
  "tasks": [
    {
      "task_id": "task-1",
      "title": "Task 1",
      "acceptance_criteria": ["done"],
      "depends_on": []
    }
  ]
}
```"""
    plan = parse_and_validate_plan(raw_markdown)
    assert plan.objective == "Test objective"
    assert len(plan.tasks) == 1
    assert plan.tasks[0].task_id == "task-1"


def test_schema_rejects_malformed_json():
    with pytest.raises(PlanValidationError) as exc:
        parse_and_validate_plan("{ bad json: missing quotes }")
    assert "Malformed JSON output" in str(exc.value)


def test_schema_rejects_missing_required_fields():
    # Missing tasks
    with pytest.raises(PlanValidationError) as exc:
        validate_breakdown_dict({"objective": "Missing tasks"})
    assert "JSON schema validation failed" in str(exc.value)

    # Missing task_id
    invalid_task = {
        "objective": "Missing task_id",
        "tasks": [
            {
                "title": "No ID",
                "acceptance_criteria": [],
                "depends_on": [],
            }
        ],
    }
    with pytest.raises(PlanValidationError):
        validate_breakdown_dict(invalid_task)


def test_schema_rejects_empty_tasks_array():
    with pytest.raises(PlanValidationError) as exc:
        validate_breakdown_dict({"objective": "Empty tasks", "tasks": []})
    assert "JSON schema validation failed" in str(exc.value)


def test_schema_rejects_additional_properties():
    data = {
        "objective": "Strict validation",
        "tasks": [
            {
                "task_id": "t-1",
                "title": "Task 1",
                "acceptance_criteria": [],
                "depends_on": [],
                "unwanted_extra_field": 123,
            }
        ],
    }
    with pytest.raises(PlanValidationError):
        validate_breakdown_dict(data)


def test_schema_rejects_cyclic_dependencies():
    data = {
        "objective": "Cycle test",
        "tasks": [
            {
                "task_id": "node-a",
                "title": "Node A",
                "acceptance_criteria": [],
                "depends_on": ["node-b"],
            },
            {
                "task_id": "node-b",
                "title": "Node B",
                "acceptance_criteria": [],
                "depends_on": ["node-a"],
            },
        ],
    }
    with pytest.raises(PlanValidationError) as exc:
        validate_breakdown_dict(data)
    assert "Cycle detected" in str(exc.value)


def test_schema_rejects_self_dependency():
    data = {
        "objective": "Self loop",
        "tasks": [
            {
                "task_id": "node-self",
                "title": "Node Self",
                "acceptance_criteria": [],
                "depends_on": ["node-self"],
            }
        ],
    }
    with pytest.raises(PlanValidationError) as exc:
        validate_breakdown_dict(data)
    assert "cannot depend on itself" in str(exc.value)


def test_schema_rejects_duplicate_task_id():
    data = {
        "objective": "Duplicate IDs",
        "tasks": [
            {"task_id": "dup", "title": "First", "acceptance_criteria": [], "depends_on": []},
            {"task_id": "dup", "title": "Second", "acceptance_criteria": [], "depends_on": []},
        ],
    }
    with pytest.raises(PlanValidationError) as exc:
        validate_breakdown_dict(data)
    assert "Duplicate task_id" in str(exc.value)


# =========================================================================
# 3. Observable error state on inference failure tests
# =========================================================================

@pytest.mark.asyncio
async def test_observable_error_on_network_failure():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused by host", request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = InferenceClient(http_client=http_client)
        orchestrator = HermesOrchestrator(client=client)

        result = await orchestrator.breakdown_objective("Build API")
        assert not result.success
        assert result.plan is None
        assert result.failure is not None
        assert result.failure.status == "blocked"
        assert result.failure.error_type == "network_error"
        assert "Network error" in result.failure.reason


@pytest.mark.asyncio
async def test_observable_error_on_timeout():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("Read timeout", request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = InferenceClient(http_client=http_client)
        orchestrator = HermesOrchestrator(client=client)

        result = await orchestrator.breakdown_objective("Build complex feature")
        assert not result.success
        assert result.failure is not None
        assert result.failure.status == "blocked"
        assert result.failure.error_type == "timeout"
        assert "timed out" in result.failure.reason


@pytest.mark.asyncio
async def test_observable_error_on_http_500():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "Internal server error in vLLM cluster"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = InferenceClient(http_client=http_client)
        orchestrator = HermesOrchestrator(client=client)

        result = await orchestrator.breakdown_objective("Build cache layer")
        assert not result.success
        assert result.failure is not None
        assert result.failure.status == "blocked"
        assert result.failure.error_type == "status_error"
        assert "HTTP 500" in result.failure.reason


@pytest.mark.asyncio
async def test_observable_error_on_invalid_format():
    async def handler(request: httpx.Request) -> httpx.Response:
        # Returns 200 but message choices are malformed
        return httpx.Response(200, json={"id": "chat-1", "choices": []})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = InferenceClient(http_client=http_client)
        orchestrator = HermesOrchestrator(client=client)

        result = await orchestrator.breakdown_objective("Build search")
        assert not result.success
        assert result.failure is not None
        assert result.failure.status == "blocked"
        assert result.failure.error_type == "format_error"


@pytest.mark.asyncio
async def test_observable_error_on_schema_violation_from_model():
    async def handler(request: httpx.Request) -> httpx.Response:
        bad_plan = {
            "objective": "Invalid plan",
            "tasks": [
                {
                    "task_id": "no-criteria",
                    "title": "Missing acceptance_criteria and depends_on",
                }
            ],
        }
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(bad_plan)}}]
        })

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = InferenceClient(http_client=http_client)
        orchestrator = HermesOrchestrator(client=client)

        result = await orchestrator.breakdown_objective("Build feature")
        assert not result.success
        assert result.failure is not None
        assert result.failure.status == "blocked"
        assert result.failure.error_type == "schema_validation_error"
        assert len(result.failure.details) > 0


# =========================================================================
# 4. Multi-provider support tests
# =========================================================================

def test_inference_client_headers_by_provider():
    cfg_openai = OrchestratorConfig(provider="openai", api_key="sk-test-key")
    client_openai = InferenceClient(config=cfg_openai)
    headers = client_openai._get_headers()
    assert headers["Authorization"] == "Bearer sk-test-key"
    assert headers["Content-Type"] == "application/json"
    assert "HTTP-Referer" not in headers

    cfg_or = OrchestratorConfig(provider="openrouter", api_key="sk-or-key")
    client_or = InferenceClient(config=cfg_or)
    headers_or = client_or._get_headers()
    assert headers_or["Authorization"] == "Bearer sk-or-key"
    assert "HTTP-Referer" in headers_or
    assert "X-Title" in headers_or

    cfg_ollama = OrchestratorConfig(provider="ollama")
    client_ollama = InferenceClient(config=cfg_ollama)
    headers_ollama = client_ollama._get_headers()
    assert "Authorization" not in headers_ollama
    assert client_ollama.config.base_url == "http://localhost:11434/v1"

    cfg_vllm = OrchestratorConfig(provider="vllm", base_url="http://gpu-cluster:8000/v1")
    client_vllm = InferenceClient(config=cfg_vllm)
    assert client_vllm.config.base_url == "http://gpu-cluster:8000/v1"


# =========================================================================
# 5. CLI commands tests
# =========================================================================

def test_cli_breakdown_dry_run(monkeypatch: pytest.MonkeyPatch):
    from click.testing import CliRunner
    from agent_bus.cli.main import app

    valid_response = json.dumps({
        "objective": "Build CLI feature",
        "summary": "Step by step breakdown",
        "tasks": [
            {
                "task_id": "cli-task-1",
                "title": "Initial setup",
                "description": "Create base files",
                "acceptance_criteria": ["Files created"],
                "test_cmd": ["pytest"],
                "depends_on": [],
            }
        ],
    })

    async def mock_completion(*args, **kwargs):
        return valid_response

    monkeypatch.setattr("agent_bus.orchestrator.client.InferenceClient.chat_completion", mock_completion)

    runner = CliRunner()
    result = runner.invoke(app, ["breakdown", "Build CLI feature", "--dry-run"])
    assert result.exit_code == 0
    assert "cli-task-1" in result.output
    assert "Modo dry-run: tareas no publicadas al bus" in result.output


def test_cli_breakdown_json_output(monkeypatch: pytest.MonkeyPatch):
    from click.testing import CliRunner
    from agent_bus.cli.main import app

    valid_response = json.dumps({
        "objective": "Build JSON feature",
        "tasks": [
            {
                "task_id": "json-task-1",
                "title": "JSON title",
                "acceptance_criteria": ["ok"],
                "depends_on": [],
            }
        ],
    })

    async def mock_completion(*args, **kwargs):
        return valid_response

    monkeypatch.setattr("agent_bus.orchestrator.client.InferenceClient.chat_completion", mock_completion)

    runner = CliRunner()
    result = runner.invoke(app, ["breakdown", "Build JSON feature", "--dry-run", "--json-output"])
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["plan"]["objective"] == "Build JSON feature"
    assert parsed["plan"]["tasks"][0]["task_id"] == "json-task-1"
    assert parsed["published"] is False


def test_cli_breakdown_inference_failure(monkeypatch: pytest.MonkeyPatch):
    from click.testing import CliRunner
    from agent_bus.cli.main import app

    async def mock_completion(*args, **kwargs):
        raise InferenceTimeoutError("LLM cluster timed out")

    monkeypatch.setattr("agent_bus.orchestrator.client.InferenceClient.chat_completion", mock_completion)

    runner = CliRunner()
    result = runner.invoke(app, ["breakdown", "Fail feature", "--dry-run"])
    assert result.exit_code == 1
    assert "Error de Desglose" in result.output
    assert "LLM cluster timed out" in result.output

