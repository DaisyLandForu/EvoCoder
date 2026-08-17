from __future__ import annotations

from pathlib import Path

from agents.agent import Agent
from agents.prompt import build_system_prompt
from agents.runtime_config import BudgetTracker, RuntimeConfig, clamp_permission, workspace_id


def test_generation_kwargs_pass_temperature_and_seed(tmp_path: Path) -> None:
    config = RuntimeConfig(
        provider="openai",
        api_key="k",
        workspace=tmp_path,
        temperature=0.0,
        seed=7,
    )
    agent = Agent(config=config, is_sub_agent=True)
    assert agent._generation_kwargs() == {"temperature": 0.0, "seed": 7}


def test_disabled_memory_keeps_the_local_index_out_of_the_prompt(tmp_path: Path, monkeypatch) -> None:
    marker = "PRIVATE_BENCHMARK_MEMORY_MARKER"
    monkeypatch.setattr(
        "agents.prompt.build_memory_prompt_section",
        lambda: f"# Memory System\n{marker}",
    )
    assert marker in build_system_prompt()
    assert marker not in build_system_prompt(memory_enabled=False)

    enabled = Agent(config=RuntimeConfig(provider="openai", api_key="k", workspace=tmp_path))
    disabled = Agent(
        config=RuntimeConfig(provider="openai", api_key="k", workspace=tmp_path, memory_enabled=False)
    )
    assert marker in enabled._system_prompt
    assert marker not in disabled._system_prompt


def test_budget_delta_reports_one_run_not_the_session() -> None:
    budget = BudgetTracker()
    budget.add_usage(2, 1, known=True)
    baseline = budget.snapshot()
    budget.add_usage(5, 3, known=True)

    delta = budget.delta_since(baseline)
    assert (delta["input_tokens"], delta["output_tokens"]) == (5, 3)
    assert delta["usage_status"] == "complete"
    assert delta["estimated_cost_usd"] < budget.cost_usd()

    budget.add_usage(None, None, known=False)
    later = budget.delta_since(baseline)
    assert later["unknown_usage_count"] == 1
    assert later["estimated_cost_usd"] is None
    assert budget.delta_since(budget.snapshot())["unknown_usage_count"] == 0


def test_allowed_tools_filters_definitions(tmp_path: Path) -> None:
    config = RuntimeConfig(
        provider="openai",
        api_key="k",
        workspace=tmp_path,
        allowed_tools=("read_file", "write_file"),
    )
    agent = Agent(config=config, is_sub_agent=True)
    assert {tool["name"] for tool in agent.tools} == {"read_file", "write_file"}


def test_child_inherits_openai_base_url(tmp_path: Path) -> None:
    parent = RuntimeConfig(
        provider="openai",
        model="gpt-4o",
        api_key="sk-test",
        base_url="https://example.test/v1",
        workspace=tmp_path,
        permission_mode="default",
    )
    child = parent.derive_child(agent_type="general")
    assert child.provider == "openai"
    assert child.base_url == "https://example.test/v1"
    assert child.api_key == "sk-test"
    assert child.workspace == tmp_path.resolve()
    assert child.budget is parent.budget


def test_child_inherits_anthropic_config(tmp_path: Path) -> None:
    parent = RuntimeConfig(
        provider="anthropic",
        model="claude-sonnet-4-6",
        api_key="sk-ant",
        base_url="https://api.anthropic.example",
        workspace=tmp_path,
    )
    child = parent.derive_child(agent_type="explore")
    assert child.provider == "anthropic"
    assert child.base_url == "https://api.anthropic.example"
    assert child.permission_mode == "plan"


def test_general_child_does_not_auto_bypass() -> None:
    parent = RuntimeConfig(permission_mode="default")
    child = parent.derive_child(agent_type="general")
    assert child.permission_mode == "default"
    assert clamp_permission("default", "bypassPermissions") == "default"


def test_workspace_id_uses_normalized_path(tmp_path: Path) -> None:
    nested = tmp_path / "proj"
    nested.mkdir()
    assert workspace_id(nested / ".") == workspace_id(nested.resolve())
