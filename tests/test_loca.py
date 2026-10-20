from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(autouse=True)
def loca_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("LOCA_HOME", str(home))
    import importlib
    import loca.config, loca.session, loca.memory, loca.cli  # noqa: E401
    for mod in (loca.config, loca.session, loca.memory, loca.cli):
        importlib.reload(mod)
    yield home


from fake_ollama import FakeOllama  # noqa: E402


def make_ctx(tmp_path, mode="yolo", **cfg_over):
    from loca.config import DEFAULTS
    from loca.permissions import Permissions
    from loca.tools import ToolContext
    from loca.ui import UI
    cfg = {**DEFAULTS, "permission_mode": mode, **cfg_over}
    return ToolContext(cwd=tmp_path, cfg=cfg, ui=UI(quiet=True), permissions=Permissions(cfg, interactive=False))


# ---------------------------------------------------------------- tools
def test_edit_requires_read_and_fuzzy(tmp_path):
    from loca.tools.fs import EditFile, ReadFile
    from loca.tools.base import ToolError
    f = tmp_path / "a.py"
    f.write_text("def f():\n    x = 1\n    return x\n")
    ctx = make_ctx(tmp_path)
    with pytest.raises(ToolError):
        EditFile().run({"path": "a.py", "old_string": "x = 1", "new_string": "x = 2"}, ctx)
    out = ReadFile().run({"path": "a.py"}, ctx)
    assert "1\tdef f():" in out
    EditFile().run({"path": "a.py", "old_string": "x = 1", "new_string": "x = 2"}, ctx)
    assert "x = 2" in f.read_text()
    # model lost indentation -> fuzzy match with re-indent
    EditFile().run({"path": "a.py", "old_string": "x = 2\nreturn x",
                    "new_string": "x = 3\nreturn x * 2"}, ctx)
    assert f.read_text() == "def f():\n    x = 3\n    return x * 2\n"
    with pytest.raises(ToolError, match="not found"):
        EditFile().run({"path": "a.py", "old_string": "zzz", "new_string": "y"}, ctx)


def test_edit_ambiguous(tmp_path):
    from loca.tools.fs import EditFile, ReadFile
    from loca.tools.base import ToolError
    (tmp_path / "b.txt").write_text("a\na\n")
    ctx = make_ctx(tmp_path)
    ReadFile().run({"path": "b.txt"}, ctx)
    with pytest.raises(ToolError, match="2 times"):
        EditFile().run({"path": "b.txt", "old_string": "a", "new_string": "b"}, ctx)
    EditFile().run({"path": "b.txt", "old_string": "a", "new_string": "b", "replace_all": True}, ctx)
    assert (tmp_path / "b.txt").read_text() == "b\nb\n"


def test_bash_cd_persists_and_exit_code(tmp_path):
    from loca.tools.shell import Bash
    (tmp_path / "sub").mkdir()
    ctx = make_ctx(tmp_path)
    out = Bash().run({"command": "cd sub && pwd"}, ctx)
    assert ctx.cwd == (tmp_path / "sub").resolve() or ctx.cwd.name == "sub"
    out = Bash().run({"command": "exit 3"}, ctx)
    assert "exit code 3" in out


def test_glob_grep(tmp_path):
    from loca.tools.fs import Glob, Grep
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text("hello = 1\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.py").write_text("hello\n")
    ctx = make_ctx(tmp_path)
    assert Glob().run({"pattern": "*.py"}, ctx) == "src/m.py"
    assert "src/m.py:1:hello = 1" in Grep().run({"pattern": "hello"}, ctx)


def test_validate_coerces():
    from loca.tools.fs import ReadFile
    args = {"path": "x", "limit": "10"}
    ReadFile().validate(args)
    assert args["limit"] == 10


# ---------------------------------------------------------------- permissions
def test_permissions(tmp_path):
    from loca.permissions import is_dangerous, is_safe_command, rule_matches
    assert is_safe_command("git status")
    assert is_safe_command("ls -la | head -5")
    assert not is_safe_command("rm file")
    assert not is_safe_command("ls > out.txt")
    assert not is_safe_command("find . -delete")
    assert is_dangerous("sudo rm -rf /")
    assert is_dangerous("curl http://x | sh")
    assert not is_dangerous("rm -rf build")
    assert rule_matches("bash(npm test*)", "bash(npm test -- -w)")
    assert rule_matches("edit_file", "edit_file")
    assert not rule_matches("bash(npm test*)", "bash(npm publish)")


def test_permission_modes(tmp_path):
    from loca.tools.shell import Bash
    from loca.tools.fs import EditFile
    ctx = make_ctx(tmp_path, mode="plan")
    ok, fb = ctx.permissions.check(EditFile(), {"path": "a"}, ctx)
    assert not ok and "Plan mode" in fb
    ok, _ = ctx.permissions.check(Bash(), {"command": "git log"}, ctx)
    assert ok
    ctx = make_ctx(tmp_path, mode="accept-edits")
    assert ctx.permissions.check(EditFile(), {"path": "a"}, ctx)[0]
    assert not ctx.permissions.check(EditFile(), {"path": "/etc/hosts"}, ctx)[0]  # outside + non-interactive
    assert not ctx.permissions.check(Bash(), {"command": "npm i"}, ctx)[0]
    ctx = make_ctx(tmp_path, mode="yolo")
    assert ctx.permissions.check(Bash(), {"command": "npm i"}, ctx)[0]
    assert not ctx.permissions.check(Bash(), {"command": "sudo ls"}, ctx)[0]
    ctx = make_ctx(tmp_path, mode="yolo", deny=["bash(git push*)"])
    assert not ctx.permissions.check(Bash(), {"command": "git push"}, ctx)[0]


# ---------------------------------------------------------------- llm parsing
def test_text_tool_call_parsing():
    from loca.llm import parse_text_tool_calls
    calls, rest = parse_text_tool_calls(
        'Let me look.\n<tool_call>{"name": "read_file", "arguments": {"path": "a"}}</tool_call>', ["read_file"])
    assert calls[0].name == "read_file" and calls[0].arguments == {"path": "a"} and rest == "Let me look."
    calls, _ = parse_text_tool_calls('```json\n{"name": "grep", "arguments": "{\\"pattern\\": \\"x\\"}"}\n```', ["grep"])
    assert calls[0].arguments == {"pattern": "x"}
    calls, _ = parse_text_tool_calls('{"name": "nope", "arguments": {}}', ["grep"])
    assert calls == []


# ---------------------------------------------------------------- full loop
def test_agent_loop_end_to_end(tmp_path, capsys):
    (tmp_path / "app.py").write_text("print('helo')\n")
    fake = FakeOllama([
        {"tool_calls": [("plan_write", {"tasks": [
            {"title": "fix typo", "files": ["app.py"],
             "accept": {"kind": "cmd", "command": "python3 app.py"}}]})]},
        {"tool_calls": [("glob", {"pattern": "*.py"})]},
        {"tool_calls": [("read_file", {"path": "app.py"})]},
        {"tool_calls": [("edit_file", {"path": "app.py", "old_string": "helo", "new_string": "hello"})]},
        # text-form tool call (fallback parser)
        {"content": '<tool_call>{"name": "bash", "arguments": {"command": "python3 app.py"}}</tool_call>'},
        {"tool_calls": [("no_such_tool", {})]},
        {"content": "Исправил опечатку в `app.py:1`."},
    ])
    from loca.cli import main
    main(["-p", "--yolo", "--host", fake.url, "-m", "fake:1b", "--cwd", str(tmp_path), "fix the typo"])
    fake.close()
    out = capsys.readouterr().out
    assert (tmp_path / "app.py").read_text() == "print('hello')\n"
    assert "Исправил" in out
    msgs = fake.requests[-1]["messages"]
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert any("hello" in m["content"] for m in tool_msgs if m["tool_name"] == "bash")
    assert "Unknown tool" in tool_msgs[-1]["content"]
    assert fake.requests[0]["options"]["num_ctx"] == 16384
    assert fake.requests[0]["tools"]
    # session was saved
    from loca.session import list_sessions
    assert list_sessions(tmp_path)[0]["title"] == "fix the typo"


def test_non_interactive_denies(tmp_path, capsys):
    fake = FakeOllama([
        {"tool_calls": [("write_file", {"path": "x.txt", "content": "hi"})]},
        {"content": "Не удалось."},
    ])
    from loca.cli import main
    main(["-p", "--host", fake.url, "-m", "fake:1b", "--cwd", str(tmp_path), "write"])
    fake.close()
    assert not (tmp_path / "x.txt").exists()
    assert "not granted" in fake.requests[-1]["messages"][-1]["content"]


def test_subagent(tmp_path, capsys):
    (tmp_path / "secret.txt").write_text("42\n")
    fake = FakeOllama([
        {"tool_calls": [("task", {"description": "find secret", "prompt": "read secret.txt"})]},
        {"tool_calls": [("read_file", {"path": "secret.txt"})]},   # subagent
        {"content": "The secret is 42."},                           # subagent report
        {"content": "Ответ: 42"},
    ])
    from loca.cli import main
    main(["-p", "--host", fake.url, "-m", "fake:1b", "--cwd", str(tmp_path), "find"])
    fake.close()
    sub_req = fake.requests[1]
    assert {t["function"]["name"] for t in sub_req["tools"]} == {
        "read_file", "read_symbol", "repo_map", "list_dir", "glob", "grep", "set_var", "get_var"}
    assert "research subagent" in sub_req["messages"][0]["content"]
    main_last = fake.requests[-1]["messages"]
    assert main_last[-1]["content"] == "The secret is 42."


def test_compaction(tmp_path):
    from loca.agent import Agent
    from loca.llm import OllamaClient
    from loca.tools import main_tools
    from loca.ui import UI
    fake = FakeOllama([{"content": "SUMMARY: user wants X"}])
    ctx = make_ctx(tmp_path, num_ctx=2000)
    ctx.cfg["host"], ctx.cfg["model"] = fake.url, "fake:1b"
    agent = Agent(OllamaClient(ctx.cfg), main_tools(), ctx, UI(quiet=True))
    for i in range(6):
        agent.messages += [{"role": "user", "content": f"q{i} " + "x" * 800},
                           {"role": "assistant", "content": "", "tool_calls": [
                               {"function": {"name": "grep", "arguments": {"pattern": "p"}}}]},
                           {"role": "tool", "tool_name": "grep", "content": "r" * 3000},
                           {"role": "assistant", "content": f"a{i}"}]
    agent.maybe_compact()
    fake.close()
    roles = [m["role"] for m in agent.messages]
    assert "SUMMARY" in agent.messages[1]["content"]
    assert agent.messages[-4]["content"].startswith("q5")
    assert roles[0] == "system" and len(agent.messages) == 7


def test_memory_and_prompt(tmp_path):
    from loca.memory import add_memory, system_prompt
    from loca.config import DEFAULTS
    (tmp_path / ".git").mkdir()
    (tmp_path / "AGENTS.md").write_text("Use tabs.")
    sub = tmp_path / "pkg"
    sub.mkdir()
    add_memory(sub, "run make test")
    prompt = system_prompt(sub, DEFAULTS, "default")
    assert "Use tabs." in prompt and "run make test" in prompt
    assert prompt.index("Use tabs.") < prompt.index("run make test")


def test_mcp(tmp_path):
    from loca.mcp import MCPManager
    from loca.ui import UI
    mgr = MCPManager({"calc": {"command": sys.executable,
                               "args": [str(Path(__file__).parent / "fake_mcp_server.py")]}}, UI(quiet=True))
    tools = mgr.tools()
    assert [t.name for t in tools] == ["mcp__calc__add"]
    ctx = make_ctx(tmp_path)
    assert tools[0].run({"a": 2, "b": 3}, ctx) == "5"
    mgr.stop()


# ---------------------------------------------------------------- config
def test_config_merges_and_dedupes(tmp_path, loca_home):
    import json
    from loca.config import load_config
    (loca_home).mkdir(parents=True, exist_ok=True)
    (loca_home / "config.json").write_text(json.dumps(
        {"model": "qwen3.5:9b", "num_ctx": 8192, "allow": ["run_tests", "bash(pytest*)"]}))
    (tmp_path / ".loca").mkdir()
    (tmp_path / ".loca" / "config.json").write_text(json.dumps(
        {"num_ctx": 4096, "allow": ["bash(pytest*)", "edit_file"]}))
    cfg = load_config(tmp_path, {"model": None, "num_ctx": 2048})
    assert cfg["model"] == "qwen3.5:9b"            # None-переопределения игнорируются
    assert cfg["num_ctx"] == 2048                  # флаг CLI важнее файлов
    assert cfg["allow"] == ["run_tests", "bash(pytest*)", "edit_file"]   # без дублей


# ---------------------------------------------------------------- variables
def test_vars_basics():
    from loca.state import VarError, Vars
    v = Vars(limit=20, max_vars=2)
    v.set("test_cmd", "pytest -q")
    assert v.get("test_cmd") == "pytest -q"
    assert v.expand("run {{test_cmd}} now") == "run pytest -q now"
    assert v.expand("{{ test_cmd }}") == "pytest -q"
    assert v.expand("{{unknown}}") == "{{unknown}}"          # left alone, never blows up
    assert v.set("long", "x" * 50).endswith("chars)") and len(v.get("long")) < 60
    with pytest.raises(VarError):
        v.set("2bad", "x")
    with pytest.raises(VarError):
        v.set("third", "x")                                   # max_vars reached
    assert "test_cmd" in v.summary() and "pytest -q" in v.summary()
    v2 = Vars()
    v2.set("data", {"a": 1})
    assert '"a": 1' in v2.get("data")
    assert v2.delete("data") and not v2.delete("data")


def test_vars_expand_args_whole_only():
    from loca.state import Vars
    v = Vars()
    v.set("plan", "1. do it\n2. test it")
    v.set("name", "REPLACED")
    # content is expanded only when it is exactly one placeholder -> {{...}} templates survive
    args = v.expand_args({"path": "out/{{name}}.md", "content": "hello {{name}}"},
                         ("path",), ("content",))
    assert args["path"] == "out/REPLACED.md" and args["content"] == "hello {{name}}"
    args = v.expand_args({"path": "p", "content": "  {{plan}} "}, ("path",), ("content",))
    assert args["content"] == "1. do it\n2. test it"
    # nested structures
    assert v.expand_args({"todos": [{"content": "{{name}}"}]})["todos"][0]["content"] == "REPLACED"


def test_vars_tools_and_expansion_end_to_end(tmp_path):
    (tmp_path / "app.py").write_text("print('helo')\n")
    fake = FakeOllama([
        {"tool_calls": [("set_var", {"name": "target", "value": "app.py"})]},
        {"tool_calls": [("read_file", {"path": "{{target}}"})]},          # expanded by the agent
        {"tool_calls": [("edit_file", {"path": "{{target}}", "old_string": "helo",
                                       "new_string": "hello"})]},
        {"tool_calls": [("run_tests", {"command": "echo '1 passed'"})]},
        {"tool_calls": [("get_var", {"name": "target"})]},
        {"content": "Готово."},
    ])
    from loca.cli import main
    main(["-p", "--yolo", "--host", fake.url, "-m", "fake:1b", "--cwd", str(tmp_path), "fix"])
    fake.close()
    assert (tmp_path / "app.py").read_text() == "print('hello')\n"
    tool_msgs = [m for m in fake.requests[-1]["messages"] if m["role"] == "tool"]
    assert "print('helo')" in tool_msgs[0]["content"] or "helo" in tool_msgs[1]["content"]
    assert tool_msgs[-1]["content"] == "app.py"                 # get_var
    assert any("tests PASSED" in m["content"] for m in tool_msgs)
    # variables are stored in the session and come back on resume
    from loca.session import list_sessions, Session
    sid = list_sessions(tmp_path)[0]["id"]
    assert Session.load(sid)[3] == {"target": "app.py"}


# ---------------------------------------------------------------- run_tests
def test_detect_test_command(tmp_path):
    from loca.tools.tests import detect_test_command
    assert detect_test_command(tmp_path, pytest_ok=True) is None
    (tmp_path / "tests").mkdir()
    assert detect_test_command(tmp_path, pytest_ok=True) == ("python3 -m pytest -q", "pytest")
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    assert detect_test_command(tmp_path) == ("cargo test", "cargo")
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}')
    assert detect_test_command(tmp_path)[1] == "node"


def test_run_tests_pass_fail_and_target(tmp_path):
    from loca.tools.tests import RunTests
    from loca.tools.base import ToolError
    ctx = make_ctx(tmp_path)
    with pytest.raises(ToolError, match="No test setup"):
        RunTests().run({}, ctx)
    out = RunTests().run({"command": "echo '3 passed'"}, ctx)
    assert "tests PASSED" in out and "3 passed" in out and ctx.tests_run == 1 and ctx.tests_passed
    out = RunTests().run({"command": "echo '1 failed'; exit 1"}, ctx)
    assert "tests FAILED" in out and "run run_tests again" in out and not ctx.tests_passed
    (tmp_path / "tests").mkdir()
    cmd, _ = RunTests().build({"target": "tests/test_x.py", "pattern": "smoke"}, ctx)
    assert cmd == "python3 -m pytest -q tests/test_x.py -k 'smoke'"
    ctx.cfg["test_command"] = "make test"
    assert RunTests().build({}, ctx)[0] == "make test"


def test_run_tests_is_allowed_by_default(tmp_path):
    from loca.tools.tests import RunTests
    ctx = make_ctx(tmp_path, mode="default")
    assert ctx.permissions.check(RunTests(), {"command": "pytest"}, ctx)[0]


# ---------------------------------------------------------------- decomposition
def test_plan_write_validates_the_graph(tmp_path):
    from loca.tools.agentic import PlanWrite
    from loca.tools.base import ToolError
    ctx = make_ctx(tmp_path)
    tool = PlanWrite()

    # подзадача без проверки не принимается — её результат нечем подтвердить
    with pytest.raises(ToolError, match="accept"):
        tool.run({"tasks": [{"title": "починить"}, {"title": "ещё"}]}, ctx)
    # текстовый критерий превращается в команду, а не в «поверю на слово»
    out = tool.run({"tasks": [{"title": "починить", "accept": "python3 app.py"}]}, ctx)
    assert ctx.plan.nodes[0]["accept"] == {"kind": "cmd", "command": "python3 app.py"}
    assert "run_plan" in out and ctx.plan.nodes[0]["id"] == "n1"        # id проставлен сам

    with pytest.raises(ToolError, match="n9"):                          # висячая зависимость
        tool.run({"tasks": [{"id": "n1", "title": "a", "accept": {"kind": "tests"},
                             "depends_on": ["n9"]}]}, ctx)
    with pytest.raises(ToolError, match="зациклен"):
        tool.run({"tasks": [{"id": "a", "title": "a", "accept": {"kind": "tests"},
                             "depends_on": ["b"]},
                            {"id": "b", "title": "b", "accept": {"kind": "tests"},
                             "depends_on": ["a"]}]}, ctx)
    with pytest.raises(ToolError, match="уже занят"):
        tool.run({"tasks": [{"id": "n1", "title": "a", "accept": {"kind": "tests"}},
                            {"id": "n1", "title": "b", "accept": {"kind": "tests"}}]}, ctx)

    # нормальный план: порядок, зависимости, замечание про отсутствие files
    out = tool.run({"tasks": [
        {"id": "n1", "title": "создать validators.py", "files": ["validators.py"],
         "accept": {"kind": "symbol", "path": "validators.py", "name": "validate_user"}},
        {"id": "n2", "title": "переписать store.py", "files": ["store.py"], "depends_on": ["n1"],
         "accept": {"kind": "tests", "target": "tests/test_store.py"}},
        {"id": "n3", "title": "прогнать всё", "accept": {"kind": "tests"}}]}, ctx)
    assert "3 подзадач" in out and "без files: n3" in out
    assert [n["id"] for n in ctx.plan.ready()] == ["n1", "n3"]          # n2 ждёт n1


def test_exit_plan_mode(tmp_path):
    from loca.tools.agentic import ExitPlanMode
    ctx = make_ctx(tmp_path, mode="plan")
    out = ExitPlanMode().run({"plan": "1. fix\n2. test"}, ctx)
    assert "non-interactive" in out and ctx.vars.get("plan").startswith("1. fix")
    assert ctx.permissions.mode == "plan"
    ctx = make_ctx(tmp_path, mode="yolo")
    assert "no approval needed" in ExitPlanMode().run({"plan": "x"}, ctx)


def test_code_subagent_shares_vars(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    fake = FakeOllama([
        {"tool_calls": [("task", {"description": "bump x", "prompt": "set x to 2",
                                  "subagent_type": "code"})]},
        {"tool_calls": [("read_file", {"path": "app.py"})]},                       # subagent
        {"tool_calls": [("edit_file", {"path": "app.py", "old_string": "x = 1",
                                       "new_string": "x = 2"})]},                  # subagent
        {"tool_calls": [("set_var", {"name": "changed", "value": "app.py:1"})]},    # subagent
        {"content": "Изменил app.py:1."},                                          # subagent report
        {"tool_calls": [("get_var", {"name": "changed"})]},                        # parent
        {"tool_calls": [("run_tests", {"command": "echo '1 passed'"})]},           # parent verifies
        {"content": "Готово."},
    ])
    from loca.cli import main
    main(["-p", "--yolo", "--host", fake.url, "-m", "fake:1b", "--cwd", str(tmp_path), "bump"])
    fake.close()
    assert (tmp_path / "app.py").read_text() == "x = 2\n"
    sub_req = fake.requests[1]
    names = {t["function"]["name"] for t in sub_req["tools"]}
    assert {"edit_file", "run_tests", "set_var"} <= names and "task" not in names
    assert "coding subagent" in sub_req["messages"][0]["content"]
    parent_tools = [m for m in fake.requests[-1]["messages"] if m["role"] == "tool"]
    assert any(m["content"] == "app.py:1" for m in parent_tools)   # variable crossed over


def test_agent_nudges_to_run_tests(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    fake = FakeOllama([
        {"tool_calls": [("read_file", {"path": "app.py"})]},
        {"tool_calls": [("edit_file", {"path": "app.py", "old_string": "x = 1",
                                       "new_string": "x = 2"})]},
        {"content": "Исправил."},                          # tries to finish without tests
        {"tool_calls": [("run_tests", {"command": "echo '1 passed'"})]},
        {"content": "Исправил, тесты зелёные."},
    ])
    from loca.cli import main
    main(["-p", "--yolo", "--host", fake.url, "-m", "fake:1b", "--cwd", str(tmp_path), "bump"])
    fake.close()
    msgs = fake.requests[-1]["messages"]
    assert any("never ran the tests" in m.get("content", "") for m in msgs if m["role"] == "user")
    assert sum(1 for m in msgs if "never ran the tests" in (m.get("content") or "")) == 1


def test_plan_reminder_is_reinjected(tmp_path):
    from loca.agent import Agent
    from loca.llm import OllamaClient
    from loca.tools import main_tools
    from loca.ui import UI
    ctx = make_ctx(tmp_path, reminder_every=2)
    fake = FakeOllama([
        {"tool_calls": [("plan_write", {"tasks": [
            {"id": "n1", "title": "шаг 1", "files": ["a.py"], "accept": {"kind": "cmd",
                                                                        "command": "true"}},
            {"id": "n2", "title": "прогнать тесты", "files": ["b.py"], "depends_on": ["n1"],
             "accept": {"kind": "tests"}}]})]},
        {"tool_calls": [("list_dir", {"path": "."})]},
        {"content": "Готово."},
    ])
    ctx.cfg["host"], ctx.cfg["model"] = fake.url, "fake:1b"
    agent = Agent(OllamaClient(ctx.cfg), main_tools(), ctx, UI(quiet=True))
    agent.run("сделай")
    fake.close()
    reminder = next((m["content"] for m in agent.messages if "[reminder]" in (m.get("content") or "")), "")
    assert "n1 шаг 1" in reminder and "ждёт n1" in reminder


def test_compaction_keeps_vars(tmp_path):
    from loca.agent import Agent
    from loca.llm import OllamaClient
    from loca.tools import main_tools
    from loca.ui import UI
    fake = FakeOllama([{"content": "SUMMARY: user wants X"}])
    ctx = make_ctx(tmp_path, num_ctx=2000)
    ctx.cfg["host"], ctx.cfg["model"] = fake.url, "fake:1b"
    ctx.vars.set("test_cmd", "pytest -q")
    agent = Agent(OllamaClient(ctx.cfg), main_tools(), ctx, UI(quiet=True))
    for i in range(6):
        agent.messages += [{"role": "user", "content": f"q{i} " + "x" * 800},
                           {"role": "assistant", "content": "a"},
                           {"role": "tool", "tool_name": "grep", "content": "r" * 3000}]
    agent.compact()
    fake.close()
    assert "test_cmd" in agent.messages[1]["content"]
    assert "{{name}}" in agent.messages[0]["content"]      # prompt explains the syntax


# ---------------------------------------------------------------- hooks
def test_hooks_run_and_refresh_mtime(tmp_path):
    from loca import hooks
    from loca.tools.fs import ReadFile
    f = tmp_path / "a.py"
    f.write_text("x=1\n")
    ctx = make_ctx(tmp_path, hooks={"after_edit": ["printf '# formatted\\n' >> {file}",
                                                   "grep -q formatted {file}"],
                                    "stop": ["exit 3"]})
    ReadFile().run({"path": "a.py"}, ctx)
    mtime_before = ctx.read_files[str(f)]
    text = hooks.run("after_edit", ctx, file=f)
    assert "hook after_edit" in text and "→ ok" in text
    assert "изменён хуком" in text
    assert ctx.read_files[str(f)] != mtime_before        # иначе edit_file упрётся в «changed on disk»
    assert "formatted" in f.read_text()
    fail = hooks.run("stop", ctx)
    assert "→ exit 3" in fail
    assert hooks.run("after_tests", ctx) == ""           # событие не настроено
    assert hooks.enabled(ctx.cfg) and not hooks.enabled({})


def test_hook_output_reaches_model(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    fake = FakeOllama([
        {"tool_calls": [("read_file", {"path": "app.py"})]},
        {"tool_calls": [("edit_file", {"path": "app.py", "old_string": "x = 1",
                                       "new_string": "x = 2"})]},
        {"tool_calls": [("run_tests", {"command": "echo '1 passed'"})]},
        {"content": "Готово."},
    ])
    cfg_dir = tmp_path / ".loca"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(
        '{"hooks": {"after_edit": ["echo LINTED {file}"]}}', encoding="utf-8")
    from loca.cli import main
    main(["-p", "--yolo", "--host", fake.url, "-m", "fake:1b", "--cwd", str(tmp_path), "fix"])
    fake.close()
    tool_msgs = [m["content"] for m in fake.requests[-1]["messages"] if m["role"] == "tool"]
    assert any("LINTED" in c for c in tool_msgs)


def test_stop_hook_nudges_once(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    fake = FakeOllama([
        {"content": "Ничего не делал."},          # сразу финальный ответ
        {"content": "Теперь разобрался."},
    ])
    cfg_dir = tmp_path / ".loca"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text('{"hooks": {"stop": ["echo broken; exit 1"]}}',
                                         encoding="utf-8")
    from loca.cli import main
    main(["-p", "--yolo", "--host", fake.url, "-m", "fake:1b", "--cwd", str(tmp_path), "привет"])
    fake.close()
    users = [m["content"] for m in fake.requests[-1]["messages"] if m["role"] == "user"]
    assert sum(1 for c in users if "Проверка после ответа не прошла" in c) == 1


# ---------------------------------------------------------------- snapshots / undo
def test_snapshots_undo_edit_and_create(tmp_path):
    from loca.snapshots import Snapshots
    root = tmp_path / "snaps"
    f = tmp_path / "a.txt"
    f.write_text("original\n")
    snaps = Snapshots(root, keep=5)
    snaps.record(f, "edit_file")
    f.write_text("broken\n")
    path, what = snaps.undo()
    assert path == f and f.read_text() == "original\n" and "a.txt" in what
    with pytest.raises(LookupError):
        snaps.undo()
    # файл, созданный агентом, откат удаляет
    new = tmp_path / "new.txt"
    snaps.record(new, "write_file")
    new.write_text("hi\n")
    snaps.undo()
    assert not new.exists()
    # журнал переживает перезапуск процесса (нужно для /resume)
    snaps.record(f, "edit_file")
    f.write_text("again\n")
    assert Snapshots(root).undo()[0] == f
    assert f.read_text() == "original\n"


def test_snapshots_prune(tmp_path):
    from loca.snapshots import Snapshots
    f = tmp_path / "a.txt"
    f.write_text("v0\n")
    snaps = Snapshots(tmp_path / "s", keep=3)
    for i in range(6):
        snaps.record(f, "edit_file")
        f.write_text(f"v{i}\n")
    assert len(snaps.journal) == 3


def test_agent_records_snapshot_before_edit(tmp_path):
    from loca.agent import Agent
    from loca.llm import OllamaClient
    from loca.snapshots import Snapshots
    from loca.tools import main_tools
    from loca.ui import UI
    (tmp_path / "app.py").write_text("x = 1\n")
    fake = FakeOllama([
        {"tool_calls": [("read_file", {"path": "app.py"})]},
        {"tool_calls": [("edit_file", {"path": "app.py", "old_string": "x = 1",
                                       "new_string": "x = 999"})]},
        {"content": "Готово."},
    ])
    ctx = make_ctx(tmp_path)
    ctx.cfg["host"], ctx.cfg["model"] = fake.url, "fake:1b"
    ctx.snapshots = Snapshots(tmp_path / "snaps")
    agent = Agent(OllamaClient(ctx.cfg), main_tools(), ctx, UI(quiet=True))
    agent.run("правь")
    fake.close()
    assert (tmp_path / "app.py").read_text() == "x = 999\n"
    ctx.snapshots.undo()
    assert (tmp_path / "app.py").read_text() == "x = 1\n"


# ---------------------------------------------------------------- repo_map / read_symbol
def test_repo_map(tmp_path):
    from loca.tools.codemap import RepoMap
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "core.py").write_text(
        "LIMIT = 5\n\n\ndef helper(x):\n    return x\n\n\n"
        "class Engine:\n    def start(self):\n        pass\n\n"
        "    def stop(self):\n        pass\n")
    (tmp_path / "web.ts").write_text(
        "export function render(x: string) {\n  return x\n}\n\nexport class View {}\n")
    (tmp_path / "notes.md").write_text("# not code\n")
    ctx = make_ctx(tmp_path)
    out = RepoMap().run({}, ctx)
    assert "pkg/core.py" in out and "class Engine" in out and ".start()" in out and ".stop()" in out
    assert "def helper(x)" in out and "const LIMIT" in out
    names = RepoMap().run({"detail": "names"}, ctx)
    assert "start, stop" in names and "def helper\n" in names
    assert "web.ts" in out and "function render" in out and "class View" in out
    assert "notes.md" not in out
    one = RepoMap().run({"path": "pkg/core.py"}, ctx)
    assert "web.ts" not in one and "class Engine" in one
    assert "ещё" not in RepoMap().run({"pattern": "*.py"}, ctx)


def test_repo_map_signatures_tests_and_cache(tmp_path):
    from loca.tools import codemap
    from loca.tools.codemap import RepoMap
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    core = tmp_path / "pkg" / "core.py"
    core.write_text(
        'def total(items: list[dict], *, tax: float = 0.2) -> float:\n'
        '    """Сумма заказа с налогом."""\n    return 0.0\n\n\n'
        'class Cart(Base):\n    def add(self, sku: str, qty=1) -> None:\n        pass\n\n'
        '    @classmethod\n    def empty(cls) -> "Cart":\n        pass\n')
    (tmp_path / "tests" / "test_core.py").write_text("def test_total():\n    pass\n")
    (tmp_path / "app.js").write_text("export function render(x) {\n  return x\n}\n")
    ctx = make_ctx(tmp_path)
    out = RepoMap().run({}, ctx)
    # что принимает и что отдаёт — без self/cls; первая строка docstring; тесты файла
    assert "def total(items: list[dict], *, tax: float=0.2) -> float  # Сумма заказа с налогом." in out
    assert "class Cart(Base)" in out and ".add(sku: str, qty=1) -> None" in out
    assert ".empty() -> 'Cart'" in out
    assert "pkg/core.py (" in out and "тесты: tests/test_core.py" in out
    assert "1: export function render(x)" in out
    # кэш: неизменённый файл не перечитывается, изменённый — перечитывается
    calls = []
    real = codemap._read_text
    codemap._read_text = lambda p: calls.append(p) or real(p)
    try:
        RepoMap().run({"path": "pkg/core.py"}, ctx)
        assert calls == []
        core.write_text("def total(items) -> int:\n    return 0\n")
        again = RepoMap().run({"path": "pkg/core.py"}, ctx)
        assert calls and "def total(items) -> int" in again
    finally:
        codemap._read_text = real
    # большая карта — автоматически без сигнатур и с подсказкой
    ctx.cfg["max_tool_output"] = 300
    for i in range(12):
        (tmp_path / f"m{i}.py").write_text(f"def f{i}(a: int, b: str = 'x') -> bool:\n    return True\n")
    big = RepoMap().run({}, ctx)
    assert "Сигнатуры скрыты" in big


def test_read_symbol(tmp_path):
    from loca.tools.base import ToolError
    from loca.tools.codemap import ReadSymbol
    from loca.tools.fs import EditFile
    (tmp_path / "a.py").write_text(
        "def outside():\n    return 0\n\n\n"
        "class Engine:\n    def start(self):\n        return 'go'\n\n"
        "    def stop(self):\n        return 'halt'\n")
    (tmp_path / "b.py").write_text("def outside():\n    return 1\n")
    ctx = make_ctx(tmp_path)
    out = ReadSymbol().run({"name": "Engine.start", "path": "a.py"}, ctx)
    assert out.startswith("a.py:6-7") and "return 'go'" in out and "stop" not in out
    assert "class Engine" in ReadSymbol().run({"name": "Engine", "path": "a.py"}, ctx)
    with pytest.raises(ToolError, match="нескольких"):
        ReadSymbol().run({"name": "outside"}, ctx)
    with pytest.raises(ToolError, match="не найден"):
        ReadSymbol().run({"name": "nope"}, ctx)
    # read_symbol считается чтением файла: править можно сразу
    EditFile().run({"path": "a.py", "old_string": "return 'go'", "new_string": "return 'run'"}, ctx)
    assert "return 'run'" in (tmp_path / "a.py").read_text()


def test_read_symbol_non_python(tmp_path):
    from loca.tools.codemap import ReadSymbol
    (tmp_path / "app.js").write_text(
        "const x = 1\n\nfunction greet(name) {\n  return `hi ${name}`\n}\n\nconst y = 2\n")
    ctx = make_ctx(tmp_path)
    out = ReadSymbol().run({"name": "greet"}, ctx)
    assert "function greet" in out and "hi ${name}" in out and "const y" not in out


# ---------------------------------------------------------------- точечный think
def test_think_smart(tmp_path):
    from loca.agent import Agent, needs_thought
    from loca.llm import OllamaClient
    from loca.tools import main_tools
    from loca.ui import UI
    assert needs_thought("$ pytest\n[tests FAILED: 1 failed · exit code 1]")
    assert not needs_thought("Wrote 12 lines to app.py")
    ctx = make_ctx(tmp_path)
    fake = FakeOllama([{"content": "ok"}])
    ctx.cfg["host"], ctx.cfg["model"] = fake.url, "fake:1b"
    agent = Agent(OllamaClient(ctx.cfg), main_tools(), ctx, UI(quiet=True))
    assert agent._think_for(1) is True                       # планирование
    assert agent._think_for(2) is None
    agent.messages.append({"role": "tool", "tool_name": "run_tests",
                           "content": "[tests FAILED: 1 failed · exit code 1]"})
    assert agent._think_for(2) is True                       # разбор падения
    agent.messages[-1] = {"role": "tool", "tool_name": "glob", "content": "a.py"}
    assert agent._think_for(2) is None
    ctx.cfg["think_smart"] = False
    assert agent._think_for(1) is None
    fake.close()


def test_think_smart_reaches_ollama(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    fake = FakeOllama([
        {"tool_calls": [("glob", {"pattern": "*.py"})]},
        {"content": "Нашёл app.py."},
    ])
    from loca.cli import main
    main(["-p", "--yolo", "--host", fake.url, "-m", "fake:1b", "--cwd", str(tmp_path), "найди"])
    fake.close()
    assert fake.requests[0].get("think") is True      # первый шаг — планирование
    assert fake.requests[1].get("think") is False     # дальше без размышлений


# ---------------------------------------------------------------- статистика и бенчмарк
def test_stats_file(tmp_path):
    import json
    (tmp_path / "app.py").write_text("x = 1\n")
    stats = tmp_path / "stats.json"
    fake = FakeOllama([
        {"tool_calls": [("glob", {"pattern": "*.py"})]},
        {"tool_calls": [("set_var", {"name": "f", "value": "app.py"})]},
        {"content": "app.py"},
    ])
    from loca.cli import main
    main(["-p", "--yolo", "--host", fake.url, "-m", "fake:1b", "--cwd", str(tmp_path),
          "--stats", str(stats), "какие файлы"])
    fake.close()
    data = json.loads(stats.read_text(encoding="utf-8"))
    assert data["model"] == "fake:1b" and data["requests"] == 3 and data["steps"] == 3
    assert data["tools"] == {"glob": 1, "set_var": 1}
    assert data["tokens_in"] > 0 and data["vars"] == ["f"] and data["edited"] is False


def test_bench_tasks_are_well_formed():
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench"))
    import tasks as bench_tasks
    assert len(bench_tasks.TASKS) >= 10
    names = [t["name"] for t in bench_tasks.TASKS]
    assert len(names) == len(set(names))
    for task in bench_tasks.TASKS:
        assert task["prompt"] and task["files"], task["name"]
        assert task.get("checks") or task.get("expect_output"), task["name"]
        for rel, content in task["files"].items():
            if rel.endswith(".py"):
                compile(content, rel, "exec")          # задача не должна быть сломана изначально
        for rel, content in (task.get("verify_files") or {}).items():
            compile(content, rel, "exec")
    assert [t["name"] for t in bench_tasks.by_name(["fix_typo"])] == ["fix_typo"]
    with pytest.raises(SystemExit):
        bench_tasks.by_name(["нет-такой"])


def test_bench_runner_end_to_end(tmp_path, monkeypatch):
    """Прогон одной задачи через раннер, но с поддельной моделью вместо Ollama."""
    import argparse
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench"))
    import run as bench_run
    import tasks as bench_tasks
    fake = FakeOllama([
        {"tool_calls": [("read_file", {"path": "app.py"})]},
        {"tool_calls": [("edit_file", {"path": "app.py", "old_string": "helo", "new_string": "hello"})]},
        {"tool_calls": [("run_tests", {"command": "python3 app.py"})]},
        {"content": "Исправил."},
    ])
    task = bench_tasks.by_name(["fix_typo"])[0]
    args = argparse.Namespace(loca=f"{sys.executable} -m loca", model="fake:1b", ctx=None,
                              host=fake.url, think=False, timeout=120, margin=45, keep=False,
                              show_output=False, verbose=False)
    result = bench_run.run_task(task, args)
    fake.close()
    assert result["passed"], result["reason"]
    assert result["steps"] == 4 and result["seconds"] > 0
    assert result["tools"] == {"read_file": 1, "edit_file": 1, "run_tests": 1}
    assert result["tests_run"] == 1 and result["tests_passed"] is True
    cmd = bench_run.agent_command(args, Path("/tmp"), Path("/tmp/s.json"), "p", 75)
    assert "--max-seconds" in cmd and cmd[cmd.index("--max-seconds") + 1] == "75"
    assert "--max-seconds" not in bench_run.agent_command(args, Path("/tmp"), Path("/tmp/s.json"), "p", 0)


def _bench_module(name):
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench"))
    return __import__(name)


def test_bench_groups_and_hard_tasks():
    bench_tasks = _bench_module("tasks")
    hard = bench_tasks.by_name(["hard"])
    assert len(hard) >= 4
    assert {t["name"] for t in hard} == set(bench_tasks.GROUPS["hard"])
    assert not set(bench_tasks.GROUPS["hard"]) & set(bench_tasks.GROUPS["core"])
    for task in hard:
        goals = task.get("partial") or []
        assert len(goals) >= 3, task["name"]        # иначе не будет градиента, только да/нет
        labels = [label for label, _cmd in goals]
        assert len(labels) == len(set(labels)), task["name"]
        assert task.get("timeout", 0) >= 600, task["name"]   # многофайловой задаче нужно время
    # группу можно смешивать с именами; порядок сохраняется как в TASKS
    mixed = [t["name"] for t in bench_tasks.by_name(["hard", "fix_typo"])]
    assert mixed[0] == "fix_typo" and len(mixed) == len(hard) + 1


def test_bench_partial_scoring(tmp_path):
    bench_run = _bench_module("run")
    (tmp_path / "done.txt").write_text("ok")
    task = {"partial": [("файл есть", "test -f done.txt"),
                        ("файла нет", "test -f missing.txt")]}
    assert bench_run.score_partial(task, tmp_path) == (1, 2, ["файла нет"])
    assert bench_run.score_partial({}, tmp_path) == (0, 0, [])


def test_bench_unittest_tasks_ship_tests_package():
    """С 3.11 unittest discover требует пакет: без tests/__init__.py задача нерешаема."""
    bench_tasks = _bench_module("tasks")
    for task in bench_tasks.TASKS:
        texts = [(task.get("config") or {}).get("test_command", ""), *task.get("checks", []),
                 *[str(v) for v in (task.get("verify_files") or {}).values()],
                 *[cmd for _label, cmd in (task.get("partial") or [])]]
        if any("discover" in t for t in texts):
            assert "tests/__init__.py" in task["files"], task["name"]


# ---------------------------------------------------------------- граф подзадач
def plan_ctx(tmp_path, spawn=None, **cfg_over):
    """Контекст с планом, снимками и поддельным субагентом."""
    from loca.snapshots import Snapshots
    ctx = make_ctx(tmp_path, **cfg_over)
    ctx.snapshots = Snapshots(tmp_path / "snaps")
    ctx.spawn_subagent = spawn
    return ctx


def set_plan(ctx, tasks):
    from loca.plan import normalize
    ctx.plan.replace(normalize(tasks))
    return ctx.plan


def test_plan_normalize_ready_and_replace():
    from loca.plan import Plan, PlanError, normalize
    nodes = normalize([
        {"title": "a", "accept": {"kind": "cmd", "command": "true"}},
        {"title": "b", "accept": {"kind": "tests"}, "depends_on": "n1"},   # строка вместо списка
    ])
    assert [n["id"] for n in nodes] == ["n1", "n2"] and nodes[1]["depends_on"] == ["n1"]
    plan = Plan(nodes)
    assert [n["id"] for n in plan.ready()] == ["n1"]
    plan.get("n1")["status"] = "done"
    assert [n["id"] for n in plan.ready()] == ["n2"] and plan.counts() == (1, 2)
    assert plan.waiting_for(plan.get("n2")) == []

    # перепланирование сохраняет сделанное с тем же id и title
    notes = plan.replace(normalize([
        {"id": "n1", "title": "a", "files": ["a.py"], "accept": {"kind": "cmd", "command": "true"}},
        {"id": "n2", "title": "b иначе", "files": ["b.py"], "accept": {"kind": "tests"}},
    ]))
    assert plan.get("n1")["status"] == "done" and plan.get("n2")["status"] == "pending"
    assert any("n1" in n for n in notes)
    with pytest.raises(PlanError, match="максимум"):
        normalize([{"title": f"t{i}", "accept": {"kind": "tests"}} for i in range(9)])
    assert "n1" in plan.render() and "[done] n1" in plan.summary()


def test_acceptance_checks_are_executable(tmp_path):
    from loca.executor import check_acceptance
    ctx = make_ctx(tmp_path)
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")

    assert check_acceptance({"accept": {"kind": "cmd", "command": "true"}}, ctx)[:2] == (True, False)
    ok, broken, out = check_acceptance({"accept": {"kind": "cmd", "command": "echo no; exit 1"}}, ctx)
    assert not ok and not broken and "exit code 1" in out          # провал работы
    ok, broken, _ = check_acceptance({"accept": {"kind": "cmd", "command": "python3 nope.py"}}, ctx)
    assert not ok and not broken            # файла ещё нет — это невыполненная работа, не поломка
    ok, broken, _ = check_acceptance({"accept": {"kind": "cmd", "command": "нетtakoy --flag"}}, ctx)
    assert not ok and broken                # нечем запускать проверку — виноват план
    # до работы субагента «сломанной» проверка не считается: файла и не должно быть
    assert check_acceptance({"accept": {"kind": "tests"}}, ctx, strict=False)[1] is False
    assert check_acceptance({"accept": {"kind": "symbol", "path": "a.py", "name": "f"}}, ctx)[0]
    ok, broken, _ = check_acceptance({"accept": {"kind": "symbol", "path": "a.py", "name": "zz"}}, ctx)
    assert not ok and not broken                                    # символа ещё нет — это не поломка
    ok, broken, out = check_acceptance({"accept": {"kind": "tests"}}, ctx)
    assert not ok and broken and "No test setup" in out
    # тесты проекта считаются прогоном тестов — нудж «прогони тесты» не сработает зря
    (tmp_path / "tests").mkdir()
    ctx.cfg["test_command"] = "echo '1 passed'"
    assert check_acceptance({"accept": {"kind": "tests"}}, ctx)[0] and ctx.tests_run == 1


def test_snapshots_mark_and_rollback(tmp_path):
    from loca.snapshots import Snapshots
    f, g = tmp_path / "a.txt", tmp_path / "b.txt"
    f.write_text("v0\n")
    snaps = Snapshots(tmp_path / "s")
    snaps.record(f, "edit_file")
    f.write_text("v1\n")
    mark = snaps.mark()                       # метка после первой правки
    snaps.record(f, "edit_file")
    f.write_text("v2\n")
    snaps.record(g, "write_file")
    g.write_text("новый\n")
    restored = snaps.rollback_to(mark)
    assert f.read_text() == "v1\n" and not g.exists()
    assert {p.name for p in restored} == {"a.txt", "b.txt"}
    assert snaps.rollback_to(mark) == [] and snaps.mark() == mark
    snaps.undo()                              # прежний /undo не сломался
    assert f.read_text() == "v0\n"


def test_executor_runs_graph_in_order(tmp_path):
    from loca.executor import Executor
    prompts = []

    def spawn(description, prompt, kind):
        prompts.append((description, prompt, kind))
        if description.startswith("n1"):
            (tmp_path / "a.py").write_text("def f():\n    return 1\n")
        else:
            (tmp_path / "b.py").write_text("print('b')\n")
        return f"сделал {description}"

    ctx = plan_ctx(tmp_path, spawn)
    set_plan(ctx, [
        {"id": "n1", "title": "создать a.py", "files": ["a.py"], "result_var": "n1_out",
         "accept": {"kind": "symbol", "path": "a.py", "name": "f"}},
        {"id": "n2", "title": "создать b.py", "files": ["b.py"], "depends_on": ["n1"],
         "accept": {"kind": "cmd", "command": "python3 b.py"}},
    ])
    report = Executor(ctx).run()
    assert [n["status"] for n in ctx.plan] == ["done", "done"]
    assert "2/2 подзадач готово" in report and "☑ n1" in report
    assert ctx.plan_stats["subagents"] == 2 and ctx.plan_stats["rollbacks"] == 0
    assert [d for d, _p, _k in prompts] == ["n1: создать a.py", "n2: создать b.py"]
    assert all(kind == "code" for _d, _p, kind in prompts)
    # контракт второй подзадачи знает про первую и про свою проверку
    second = prompts[1][1]
    assert "Уже сделано до тебя" in second and "n1" in second
    assert "python3 b.py" in second and "Менять нужно эти файлы: b.py" in second
    assert ctx.vars.get("n1_out")                        # результат положен в переменную


def test_executor_retries_and_rolls_back(tmp_path):
    from loca.executor import Executor
    (tmp_path / "a.py").write_text("print(1)\n")
    prompts = []

    def spawn(description, prompt, kind):
        prompts.append(prompt)
        ctx.snapshots.record(tmp_path / "a.py", "edit_file")
        (tmp_path / "a.py").write_text("print(broken)\n")     # ломает файл каждый раз
        return "готово"

    ctx = plan_ctx(tmp_path, spawn)
    set_plan(ctx, [{"id": "n1", "title": "поправить a.py", "files": ["a.py"],
                    "accept": {"kind": "cmd", "command": "python3 a.py | grep -q '^2$'"}}])
    report = Executor(ctx).run()
    node = ctx.plan.get("n1")
    assert node["status"] == "failed" and node["attempts"] == 2
    assert len(prompts) == 2 and "Попытка 2" in prompts[1] and "NameError" in prompts[1]
    assert (tmp_path / "a.py").read_text() == "print(1)\n"    # файл вернулся в исходное
    assert ctx.plan_stats["rollbacks"] == 2
    assert "✗ n1" in report and "перестройте план" in report or "plan_write" in report


def test_executor_rejects_vacuous_acceptance(tmp_path):
    from loca.executor import Executor
    calls = []
    ctx = plan_ctx(tmp_path, lambda d, p, k: calls.append(d) or "")
    set_plan(ctx, [{"id": "n1", "title": "что-то", "files": ["a.py"],
                    "accept": {"kind": "cmd", "command": "true"}}])
    report = Executor(ctx).run()
    assert not calls                                   # субагента даже не звали
    assert ctx.plan.get("n1")["status"] == "blocked"
    assert "ничего не проверяет" in ctx.plan.get("n1")["error"] and "✗ n1" in report


def test_executor_stops_on_broken_check(tmp_path):
    from loca.executor import Executor
    ctx = plan_ctx(tmp_path, lambda d, p, k: "готово")
    set_plan(ctx, [{"id": "n1", "title": "что-то", "files": ["a.py"],
                    "accept": {"kind": "cmd", "command": "нетtakoy --flag"}}])
    Executor(ctx).run()
    node = ctx.plan.get("n1")
    # проверку нечем запустить — вторую попытку не тратим, это вина плана, а не субагента
    assert node["status"] == "blocked" and node["attempts"] == 1
    assert "не запускается" in node["error"]


def test_executor_stops_at_deadline(tmp_path):
    """Дедлайн хода: исполнитель не должен плодить субагентов, которым уже некогда работать."""
    import time as _t
    from loca.executor import Executor
    spawned = []

    def spawn(description, prompt, kind):
        spawned.append(description)
        (tmp_path / "a.py").write_text("def f():\n    return 1\n")
        return "сделал"

    ctx = plan_ctx(tmp_path, spawn)
    set_plan(ctx, [
        {"id": "n1", "title": "создать a.py", "files": ["a.py"],
         "accept": {"kind": "symbol", "path": "a.py", "name": "f"}},
        {"id": "n2", "title": "создать b.py", "files": ["b.py"], "depends_on": ["n1"],
         "accept": {"kind": "cmd", "command": "python3 b.py"}},
    ])
    ctx.deadline = _t.time() - 1
    report = Executor(ctx).run()
    assert spawned == [] and ctx.budget_hit
    assert "вышло время" in report and "0/2 подзадач готово" in report
    assert [n["status"] for n in ctx.plan] == ["pending", "pending"]


def test_run_plan_gate_and_errors(tmp_path):
    from loca.tools.agentic import RunPlan
    from loca.tools.base import ToolError
    ctx = plan_ctx(tmp_path, lambda d, p, k: "готово")
    with pytest.raises(ToolError, match="plan_write"):
        RunPlan().run({}, ctx)
    set_plan(ctx, [{"id": "n1", "title": "a", "files": ["a.py"], "accept": {"kind": "cmd", "command": "true"}},
                   {"id": "n2", "title": "b", "files": ["a.py"], "accept": {"kind": "cmd", "command": "true"}}])
    out = RunPlan().run({}, ctx)
    assert "слишком маленький" in out and ctx.plan.get("n1")["status"] == "pending"


def test_plan_write_and_run_plan_end_to_end(tmp_path):
    (tmp_path / "app.py").write_text("print(1)\n")
    tasks = [
        {"id": "n1", "title": "поправить app.py", "files": ["app.py"],
         "accept": {"kind": "cmd", "command": "python3 app.py | grep -q '^2$'"}},
        {"id": "n2", "title": "создать b.py", "files": ["b.py"], "depends_on": ["n1"],
         "accept": {"kind": "cmd", "command": "python3 b.py"}},
        {"id": "n3", "title": "создать c.py", "files": ["c.py"], "depends_on": ["n2"],
         "accept": {"kind": "cmd", "command": "python3 c.py"}},
    ]
    fake = FakeOllama([
        {"tool_calls": [("plan_write", {"tasks": tasks})]},
        {"tool_calls": [("run_plan", {})]},
        # субагент n1
        {"tool_calls": [("read_file", {"path": "app.py"})]},
        {"tool_calls": [("edit_file", {"path": "app.py", "old_string": "print(1)",
                                       "new_string": "print(2)"})]},
        {"content": "Поправил app.py:1."},
        # субагент n2
        {"tool_calls": [("write_file", {"path": "b.py", "content": "print('b')\n"})]},
        {"content": "Создал b.py."},
        # субагент n3
        {"tool_calls": [("write_file", {"path": "c.py", "content": "print('c')\n"})]},
        {"content": "Создал c.py."},
        # родитель довёл до конца
        {"tool_calls": [("run_tests", {"command": "echo '3 passed'"})]},
        {"content": "Три подзадачи готовы."},
    ])
    from loca.cli import main
    main(["-p", "--yolo", "--host", fake.url, "-m", "fake:1b", "--cwd", str(tmp_path), "сделай"])
    fake.close()
    assert (tmp_path / "app.py").read_text() == "print(2)\n"
    assert (tmp_path / "b.py").is_file() and (tmp_path / "c.py").is_file()

    parent_tools = [m for m in fake.requests[-1]["messages"] if m["role"] == "tool"]
    assert any("3/3 подзадач готово" in m["content"] for m in parent_tools)
    # субагенту не дают ни планировать, ни запускать план — он делает одну подзадачу
    sub_names = {t["function"]["name"] for t in fake.requests[2]["tools"]}
    assert "plan_write" not in sub_names and "run_plan" not in sub_names and "edit_file" in sub_names
    assert "Подзадача n1" in fake.requests[2]["messages"][1]["content"]
    # план уехал в сессию и вернётся после /resume
    from loca.session import Session, list_sessions
    saved = Session.load(list_sessions(tmp_path)[0]["id"])[2]
    assert [n["status"] for n in saved] == ["done", "done", "done"]


def test_plan_restored_from_session(tmp_path):
    from loca.plan import Plan
    plan = Plan()
    plan.load([{"id": "n1", "title": "a", "status": "done", "accept": {"kind": "tests"}},
               {"id": "n2", "title": "b", "status": "running", "depends_on": ["n1"]},
               {"мусор": 1}])
    assert [n["id"] for n in plan] == ["n1", "n2"]
    assert plan.get("n2")["status"] == "pending"          # прерванная подзадача снова в очереди
    assert [n["id"] for n in plan.ready()] == ["n2"]


def test_failed_edit_leaves_no_snapshot(tmp_path):
    """Неудачная правка не должна тратить /undo и считаться откатом в плане."""
    from loca.agent import Agent
    from loca.llm import OllamaClient
    from loca.snapshots import Snapshots
    from loca.tools import main_tools
    from loca.ui import UI
    (tmp_path / "app.py").write_text("x = 1\n")
    fake = FakeOllama([
        # правка без предварительного чтения — инструмент откажет
        {"tool_calls": [("edit_file", {"path": "app.py", "old_string": "x = 1",
                                       "new_string": "x = 2"})]},
        {"content": "Не вышло."},
    ])
    ctx = make_ctx(tmp_path)
    ctx.cfg["host"], ctx.cfg["model"] = fake.url, "fake:1b"
    ctx.snapshots = Snapshots(tmp_path / "snaps")
    agent = Agent(OllamaClient(ctx.cfg), main_tools(), ctx, UI(quiet=True))
    agent.run("правь")
    fake.close()
    assert (tmp_path / "app.py").read_text() == "x = 1\n"
    assert ctx.snapshots.journal == []
    with pytest.raises(LookupError):
        ctx.snapshots.undo()


# ---------------------------------------------------------------- бюджет хода и план-гейт
def test_looks_multifile():
    from loca.agent import looks_multifile
    assert looks_multifile("Переименуй calc_total в total_amount везде в проекте")
    assert looks_multifile("правь a.py, b.py и c.py")
    assert looks_multifile("вынеси общий код из core.py и report.py")
    assert not looks_multifile("почини опечатку в app.py")
    assert not looks_multifile("исправь падающий тест")
    assert not looks_multifile("")


def test_multifile_hint_before_first_call(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    fake = FakeOllama([{"content": "Понял."}])
    from loca.cli import main
    main(["-p", "--yolo", "--host", fake.url, "-m", "fake:1b", "--cwd", str(tmp_path),
          "переименуй x в y везде в проекте"])
    fake.close()
    first = fake.requests[0]["messages"]
    assert any("[reminder] This touches several files" in (m.get("content") or "")
               for m in first if m["role"] == "user")


def test_plan_gate_after_blind_edits(tmp_path):
    """Правит файлы шаг за шагом без плана — агент обязан потребовать plan_write."""
    (tmp_path / ".loca").mkdir()
    (tmp_path / ".loca" / "config.json").write_text(
        '{"plan_gate_steps": 2, "plan_gate_files": 1}', encoding="utf-8")
    (tmp_path / "a.py").write_text("x = 1\n")
    fake = FakeOllama([
        {"tool_calls": [("read_file", {"path": "a.py"})]},
        {"tool_calls": [("edit_file", {"path": "a.py", "old_string": "x = 1",
                                       "new_string": "x = 2"})]},
        {"tool_calls": [("run_tests", {"command": "echo '1 passed'"})]},
        {"content": "Готово."},
    ])
    from loca.cli import main
    main(["-p", "--yolo", "--host", fake.url, "-m", "fake:1b", "--cwd", str(tmp_path), "почини"])
    fake.close()
    users = [m["content"] for m in fake.requests[-1]["messages"] if m["role"] == "user"]
    gate = [c for c in users if "Stop improvising" in c]
    assert len(gate) == 1 and "plan_write" in gate[0] and "run_plan" in gate[0]


def test_max_seconds_stops_the_turn(tmp_path):
    from loca.agent import Agent
    from loca.llm import OllamaClient
    from loca.tools import main_tools
    from loca.ui import UI
    import time as _t
    fake = FakeOllama([{"content": "не должно вызваться"}, {"content": "успел"}])
    ctx = make_ctx(tmp_path)
    ctx.cfg["host"], ctx.cfg["model"] = fake.url, "fake:1b"
    agent = Agent(OllamaClient(ctx.cfg), main_tools(), ctx, UI(quiet=True))
    ctx.deadline = _t.time() - 1                 # дедлайн снаружи уже прошёл
    answer = agent.run("сделай что-нибудь долгое")
    assert ctx.budget_hit and "Время" in answer
    assert fake.requests == []                   # до модели дело не дошло
    # а из конфига дедлайн считается на каждый ход заново
    ctx.cfg["max_seconds"] = 600
    assert agent.run("теперь быстро") == "не должно вызваться"
    assert not ctx.budget_hit and ctx.deadline > _t.time()
    fake.close()


def test_on_step_callback_fires_every_step(tmp_path):
    from loca.agent import Agent
    from loca.llm import OllamaClient
    from loca.tools import main_tools
    from loca.ui import UI
    (tmp_path / "a.py").write_text("x = 1\n")
    fake = FakeOllama([
        {"tool_calls": [("glob", {"pattern": "*.py"})]},
        {"tool_calls": [("read_file", {"path": "a.py"})]},
        {"content": "Готово."},
    ])
    ctx = make_ctx(tmp_path)
    ctx.cfg["host"], ctx.cfg["model"] = fake.url, "fake:1b"
    agent = Agent(OllamaClient(ctx.cfg), main_tools(), ctx, UI(quiet=True))
    calls = []
    agent.on_step = lambda: calls.append(agent.usage.steps)
    agent.run("посмотри")
    fake.close()
    assert calls == [1, 2]              # после каждого шага с инструментами
    agent.on_step = lambda: 1 / 0       # сбой метрик не должен ронять ход
    fake2 = FakeOllama([{"tool_calls": [("glob", {"pattern": "*"})]}, {"content": "ок"}])
    agent.llm.host = fake2.url
    assert agent.run("ещё раз") == "ок"
    fake2.close()


def test_stats_include_touched_and_budget(tmp_path):
    import json
    (tmp_path / "a.py").write_text("x = 1\n")
    stats = tmp_path / "s.json"
    fake = FakeOllama([
        {"tool_calls": [("read_file", {"path": "a.py"})]},
        {"tool_calls": [("edit_file", {"path": "a.py", "old_string": "x = 1",
                                       "new_string": "x = 3"})]},
        {"tool_calls": [("run_tests", {"command": "echo '1 passed'"})]},
        {"content": "Готово."},
    ])
    from loca.cli import main
    main(["-p", "--yolo", "--host", fake.url, "-m", "fake:1b", "--cwd", str(tmp_path),
          "--stats", str(stats), "--max-seconds", "600", "почини a.py"])
    fake.close()
    data = json.loads(stats.read_text(encoding="utf-8"))
    assert data["touched"] == ["a.py"] and data["budget_hit"] is False
    assert data["steps"] == 4 and data["tools"]["edit_file"] == 1


def test_trace_in_stats(tmp_path):
    """Трасса вызовов нужна, чтобы после таймаута было видно, на чём агент буксовал."""
    import json
    (tmp_path / "a.py").write_text("x = 1\n")
    stats = tmp_path / "s.json"
    fake = FakeOllama([
        {"tool_calls": [("read_file", {"path": "нет-такого.py"})]},      # ошибка
        {"tool_calls": [("read_file", {"path": "a.py"})]},
        {"tool_calls": [("edit_file", {"path": "a.py", "old_string": "x = 1",
                                       "new_string": "x = 2"})]},
        {"tool_calls": [("run_tests", {"command": "echo '1 passed'"})]},
        {"content": "Готово."},
    ])
    from loca.cli import main
    main(["-p", "--yolo", "--host", fake.url, "-m", "fake:1b", "--cwd", str(tmp_path),
          "--stats", str(stats), "почини a.py"])
    fake.close()
    data = json.loads(stats.read_text(encoding="utf-8"))
    assert data["tool_errors"] == 1
    trace = data["trace"]
    assert [t["tool"] for t in trace] == ["read_file", "read_file", "edit_file", "run_tests"]
    assert trace[0]["ok"] is False and "не найден" in trace[0]["out"].lower() or "not found" in trace[0]["out"].lower()
    assert trace[1]["ok"] is True and trace[1]["step"] == 2
    assert all(len(t["out"]) <= 120 for t in trace)


def test_trace_is_capped(tmp_path):
    from loca.agent import Agent
    from loca.llm import OllamaClient
    from loca.tools import main_tools
    from loca.ui import UI
    ctx = make_ctx(tmp_path, trace_len=3)
    fake = FakeOllama([{"content": "ок"}])
    ctx.cfg["host"], ctx.cfg["model"] = fake.url, "fake:1b"
    agent = Agent(OllamaClient(ctx.cfg), main_tools(), ctx, UI(quiet=True))
    for i in range(10):
        agent._trace("glob", f"*.{i}", "No files matched.", True)
    fake.close()
    assert len(agent.trace) == 3 and agent.trace[-1]["args"] == "*.9"
    assert agent.tool_errors == 0


def test_bench_diagnose():
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench"))
    import run as bench_run
    lines = bench_run.diagnose({
        "tools": {"edit_file": 18, "read_file": 12}, "tool_errors": 15,
        "plan_stats": {"subagents": 0},
        "trace": [{"step": 28, "tool": "edit_file", "args": "a.py", "ok": False, "out": "not found"}]})
    text = "\n".join(lines)
    assert "edit_file×18" in text and "ошибок 15/30" in text
    assert "буксовал" in text and "план не использовался" in text
    assert "✗ шаг 28 edit_file(a.py)" in text
    # прогон с планом описывается иначе, и без данных диагностика молчит
    with_plan = bench_run.diagnose({"tools": {"run_plan": 1}, "plan_stats": {"subagents": 4,
                                                                            "rollbacks": 1}})
    assert "субагентов 4" in "\n".join(with_plan)
    assert bench_run.diagnose({"name": "x", "passed": False}) == []


def test_plan_normalize_never_explodes():
    """Мусор от слабой модели должен превращаться в PlanError с внятным текстом, а не в трейсбек."""
    from loca.plan import PlanError, normalize
    cases = [
        None, [], "", "план", 42, [{"title": "a"}], [{"title": "a", "accept": {"kind": "cmd"}}],
        [{"id": "n1", "title": "a", "accept": "tests", "depends_on": ["n2"]},
         {"id": "n2", "title": "b", "accept": "tests", "depends_on": ["n1"]}],
        [{"id": "n1", "title": "a", "accept": "tests"},
         {"id": "n1", "title": "b", "accept": "tests"}],
        [{"title": "a", "accept": {"kind": "symbol"}}],
        [{"title": "a", "accept": {"kind": "чушь", "command": "ls"}}],
        [{"title": "", "accept": "tests"}], [{"title": None, "accept": "tests"}],
        [{"title": "a", "accept": "tests", "depends_on": ["нет-такого"]}],
        [{"title": f"t{i}", "accept": "tests"} for i in range(50)],
        [{"title": "a", "accept": None}], [["не словарь"]],
    ]
    for case in cases:
        try:
            normalize(case)
        except PlanError as e:
            assert str(e).strip(), case
        except Exception as e:  # noqa: BLE001
            raise AssertionError(f"{type(e).__name__} на входе {case!r}: {e}")
    # а терпимые к формату входы обязаны пройти
    assert len(normalize([{"title": "a", "accept": "tests"},
                          {"title": "b", "accept": "tests", "depends_on": "n1"}])) == 2
    assert normalize([{"title": "a", "accept": "tests", "files": "a.py b.py"}])[0]["files"] == \
        ["a.py", "b.py"]
    assert normalize('[{"title": "json строкой", "accept": "tests"}]')[0]["id"] == "n1"


def test_bench_keeps_agent_state_out_of_workspace(tmp_path, monkeypatch):
    """Снимки /undo и сессии агента не должны попадать в оцениваемую копию проекта.

    Регрессия: LOCA_HOME стоял внутри рабочей копии, снимок хранил файл ДО правки, и
    проверка «старого имени в проекте не осталось» падала на переименовании, которое
    агент выполнил правильно.
    """
    import argparse
    import subprocess as _sp
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench"))
    import run as bench_run

    seen = {}
    real_run = _sp.run

    def spy(cmd, **kw):
        if isinstance(cmd, list) and any("loca" in str(c) for c in cmd) and kw.get("env"):
            seen["home"] = kw["env"].get("LOCA_HOME")
            seen["cwd"] = str(kw.get("cwd"))
        return real_run(cmd, **kw)

    monkeypatch.setattr(bench_run.subprocess, "run", spy)
    task = {"name": "probe", "prompt": "ничего", "files": {"a.py": "x = 1\n"},
            "checks": ["test -f a.py"], "timeout": 30}
    args = argparse.Namespace(loca="true", model=None, ctx=None, host=None, think=False,
                              timeout=30, margin=0, keep=False, show_output=False, verbose=False)
    bench_run.run_task(task, args)
    assert seen["home"] and seen["cwd"]
    assert not seen["home"].startswith(seen["cwd"])        # состояние агента — вне копии

    # и хеширование readonly-задач не замечает служебные каталоги
    (tmp_path / ".loca_home" / "snapshots").mkdir(parents=True)
    (tmp_path / ".loca_home" / "snapshots" / "old.py").write_text("calc_total\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.py").write_text("junk\n")
    (tmp_path / "real.py").write_text("y = 2\n")
    assert list(bench_run.snapshot(tmp_path)) == ["real.py"]


def test_bench_grep_checks_ignore_agent_dirs():
    """Проверки с рекурсивным grep должны исключать служебные каталоги."""
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench"))
    import tasks as bench_tasks
    for task in bench_tasks.TASKS:
        for check in task.get("checks", []):
            if "grep -r" in check:
                assert "--exclude-dir='.loca*'" in check, (task["name"], check)


def test_plan_write_counters(tmp_path):
    """Переписывание плана по кругу должно быть видно в метриках и названо вслух."""
    from loca.tools.agentic import PlanWrite
    from loca.tools.base import ToolError
    ctx = make_ctx(tmp_path)
    good = [{"title": "раз", "accept": "tests"}, {"title": "два", "accept": "tests"},
            {"title": "три", "accept": "tests"}]
    with pytest.raises(ToolError, match="План не принят"):
        PlanWrite().run({"tasks": [{"title": "без приёмки"}]}, ctx)
    assert ctx.plan_stats["rejected"] == 1 and ctx.plan_stats["writes"] == 0
    for _ in range(3):
        out = PlanWrite().run({"tasks": good}, ctx)
    assert ctx.plan_stats["writes"] == 3 and "переписываете план" not in out
    out = PlanWrite().run({"tasks": good}, ctx)
    assert ctx.plan_stats["writes"] == 4 and "переписываете план уже четвёртый раз" in out
    # а когда подзадачи уже запускались, упрёка нет
    ctx.plan_stats["subagents"] = 1
    assert "переписываете план" not in PlanWrite().run({"tasks": good}, ctx)


def test_diagnose_reports_plan(tmp_path):
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench"))
    import run as bench_run
    text = "\n".join(bench_run.diagnose({
        "tools": {"plan_write": 6}, "tool_errors": 0,
        "plan_stats": {"writes": 6, "rejected": 2, "subagents": 2, "rollbacks": 1, "refused": 3},
        "plan_nodes": [{"id": "n1", "status": "done", "title": "ядро"},
                       {"id": "n2", "status": "failed", "title": "реэкспорт"}]}))
    assert "планов 6 (отклонено 2)" in text and "субагентов 2" in text
    assert "отказов run_plan 3" in text and "⊘ n2 реэкспорт — failed" in text
    assert "⊘ n1" not in text                      # готовые узлы не засоряют разбор
    stuck = "\n".join(bench_run.diagnose({"tools": {"plan_write": 3},
                                          "plan_stats": {"writes": 3, "subagents": 0}}))
    assert "крутится на планировании" in stuck


def test_bench_only_failed(tmp_path, capsys):
    """Перезапуск одной упавшей задачи вместо всей группы."""
    import json
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench"))
    import run as bench_run
    previous = tmp_path / "prev.json"
    previous.write_text(json.dumps({"results": [
        {"name": "fix_typo", "passed": True}, {"name": "failing_test", "passed": False}]}),
        encoding="utf-8")
    bench_run.main(["--only-failed", str(previous), "--tasks", "fix_typo,failing_test",
                    "--loca", "true", "--timeout", "10", "--margin", "0"])
    out = capsys.readouterr().out
    assert "перезапуск упавших: failing_test" in out and "fix_typo…" not in out
    # когда всё прошло, перезапускать нечего
    previous.write_text(json.dumps({"results": [{"name": "fix_typo", "passed": True}]}),
                        encoding="utf-8")
    assert bench_run.main(["--only-failed", str(previous), "--loca", "true"]) == 0
    assert "нечего перезапускать" in capsys.readouterr().out


# ------------------------------------------------- находки из прогона hard3
def test_plan_rejects_check_only_plans():
    """Модель выдала план из четырёх «проверить тест» — это не работа, а проверки."""
    from loca.plan import PlanError, normalize
    checks = [{"title": "Проверить тест test_width_default", "accept": "tests"},
              {"title": "Проверить тест test_truncates_long_cell", "accept": "tests"},
              {"title": "Запустить все тесты test_format.py", "accept": "tests"}]
    with pytest.raises(PlanError, match="это проверки, а не работа"):
        normalize(checks)
    # а вот одинаковая приёмка «весь набор» — не отказ, а замечание: отказ на правдоподобном
    # плане стоит полного цикла перепланирования
    from loca.plan import Plan
    plan = Plan()
    notes = plan.replace(normalize([
        {"title": "добавить width", "files": ["app/config.py"], "accept": "tests"},
        {"title": "обрезать ячейку", "files": ["app/format.py"], "accept": "tests"},
        {"title": "флаг CLI", "files": ["app/cli.py"], "accept": "tests"}]))
    assert any("приёмка — весь набор тестов" in n and "Пройдёт только последняя" in n
               for n in notes)
    # есть files, но половина подзадач — «проверки»
    mixed = [{"title": "добавить width", "files": ["app/config.py"], "accept": "tests"},
             {"title": "проверить конфиг", "accept": "tests"},
             {"title": "запустить тесты", "accept": "tests"}]
    with pytest.raises(PlanError, match="это проверки, а не работа"):
        normalize(mixed)
    # одну проверочную подзадачу терпим: её отсечёт предпроверка исполнителя
    ok = normalize([{"title": "добавить width", "files": ["app/config.py"], "accept": "tests"},
                    {"title": "обрезать ячейку", "files": ["app/format.py"], "accept": "tests"},
                    {"title": "запустить тесты", "accept": "tests"}])
    assert [n["id"] for n in ok] == ["n1", "n2", "n3"]


def test_run_plan_reports_dead_end(tmp_path):
    """Всё зависит от провалившейся подзадачи: нужен разбор и два выхода, а не стена."""
    from loca.tools.agentic import RunPlan
    ctx = plan_ctx(tmp_path, lambda d, p, k: "ок")
    set_plan(ctx, [
        {"id": "n1", "title": "добавить width", "files": ["app/config.py"], "accept": "tests"},
        {"id": "n2", "title": "обрезать ячейку", "files": ["app/format.py"],
         "depends_on": ["n1"], "accept": "tests"},
        {"id": "n3", "title": "флаг CLI", "files": ["app/cli.py"], "depends_on": ["n1"],
         "accept": "tests"}])
    ctx.plan.nodes[0].update(status="failed", error="приёмка не прошла: FAILED (failures=2)")
    out = RunPlan().run({}, ctx)
    assert "Запускать нечего" in out
    assert "n1 добавить width — failed" in out and "failures=2" in out
    assert "из-за неё стоят: n2, n3" in out
    assert "новый plan_write" in out and "доделайте эту часть сами" in out
    assert "Повторный вызов" not in out
    again = RunPlan().run({}, ctx)                 # состояние не изменилось
    assert "Повторный вызов run_plan ничего не изменит" in again


def test_bash_points_to_run_tests(tmp_path):
    from loca.tools.shell import Bash
    ctx = make_ctx(tmp_path)
    assert "run_tests" in Bash().run({"command": "python3 -m pytest -q"}, ctx)
    assert "run_tests" in Bash().run({"command": "npm test"}, ctx)
    assert "run_tests" not in Bash().run({"command": "echo привет"}, ctx)


def test_deadline_answer_carries_plan_state(tmp_path):
    import time as _t
    from loca.agent import Agent
    from loca.llm import OllamaClient
    from loca.tools import main_tools
    from loca.ui import UI
    fake = FakeOllama([{"content": "не вызовется"}])
    ctx = plan_ctx(tmp_path, lambda d, p, k: "ок")
    set_plan(ctx, [{"id": "n1", "title": "сделать раз", "files": ["a.py"], "accept": "tests"},
                   {"id": "n2", "title": "сделать два", "files": ["b.py"], "accept": "tests"}])
    ctx.cfg["host"], ctx.cfg["model"] = fake.url, "fake:1b"
    agent = Agent(OllamaClient(ctx.cfg), main_tools(), ctx, UI(quiet=True))
    ctx.deadline = _t.time() - 1
    answer = agent.run("долгая задача")
    fake.close()
    assert "Время на задачу вышло" in answer
    assert "Состояние плана на момент остановки" in answer and "n1" in answer


# ------------------------------------------------- находки из живого прогона (дилемма заключённых)
def test_write_file_on_existing_file(tmp_path):
    """Модель дважды повторила write_file, решив «файла нет». Сообщение должно называть факты."""
    from loca.tools.base import ToolError
    from loca.tools.fs import ReadFile, WriteFile
    (tmp_path / "z").mkdir()
    target = tmp_path / "z" / "dilemma.py"
    target.write_text("старая версия\n" * 20)
    ctx = make_ctx(tmp_path)
    assert "Created z/new.py" in WriteFile().run({"path": "z/new.py", "content": "a\nb\n"}, ctx)
    with pytest.raises(ToolError) as err:
        WriteFile().run({"path": "z/dilemma.py", "content": "новое"}, ctx)
    message = str(err.value)
    assert "ALREADY EXISTS" in message and "20 lines" in message
    assert "read_file with path 'z/dilemma.py'" in message and "overwrite=true" in message
    # осознанная перезапись — законный выход, а не стена
    assert "Overwrote" in WriteFile().run(
        {"path": "z/dilemma.py", "content": "новое\n", "overwrite": True}, ctx)
    # и обычный путь: прочитать, потом писать
    target.write_text("ещё версия\n")
    ReadFile().run({"path": "z/dilemma.py"}, ctx)
    assert "Overwrote" in WriteFile().run({"path": "z/dilemma.py", "content": "финал\n"}, ctx)
    assert target.read_text() == "финал\n"
    # каталог вместо файла
    with pytest.raises(ToolError, match="directory"):
        WriteFile().run({"path": "z", "content": "x"}, ctx)


def test_repeated_failure_is_called_out(tmp_path):
    """Два одинаковых провалившихся вызова — уже цикл; третьего ждать дорого."""
    (tmp_path / "exists.py").write_text("x = 1\n")
    fake = FakeOllama([
        {"tool_calls": [("write_file", {"path": "exists.py", "content": "новое"})]},
        {"tool_calls": [("write_file", {"path": "exists.py", "content": "новое"})]},
        {"content": "Понял, сначала прочитаю."},
    ])
    from loca.cli import main
    main(["-p", "--yolo", "--host", fake.url, "-m", "fake:1b", "--cwd", str(tmp_path), "перепиши"])
    fake.close()
    tools = [m["content"] for m in fake.requests[-1]["messages"] if m["role"] == "tool"]
    assert "failed twice" not in tools[0]
    assert "failed twice" in tools[1] and "Do NOT send it a third time" in tools[1]


def test_compaction_reverts_when_it_inflates(tmp_path):
    """Пересказ длиннее вырезанного куска: сжатие должно откатиться и не повторяться."""
    from loca.agent import Agent
    from loca.llm import OllamaClient
    from loca.tools import main_tools
    from loca.ui import UI
    fake = FakeOllama([{"content": "ОЧЕНЬ ПОДРОБНЫЙ ПЕРЕСКАЗ. " * 400}])
    ctx = make_ctx(tmp_path, num_ctx=2000)
    ctx.cfg["host"], ctx.cfg["model"] = fake.url, "fake:1b"
    agent = Agent(OllamaClient(ctx.cfg), main_tools(), ctx, UI(quiet=True))
    for i in range(4):
        agent.messages += [{"role": "user", "content": f"q{i}"},
                           {"role": "assistant", "content": "a"},
                           {"role": "tool", "tool_name": "grep", "content": "r" * 500}]
    before = list(agent.messages)
    size_before = agent.context_tokens()
    agent.compact()
    assert agent.messages == before                  # откат: стало бы только хуже
    assert agent._compact_floor > size_before        # и повторять не будем
    # пока контекст не вырос, сжатие даже не пытается вызвать модель
    agent.maybe_compact()
    assert len(fake.requests) == 1
    fake.close()


def test_pytest_missing_falls_back_to_unittest(tmp_path):
    """brew обновил питон — pytest остался у старого. Это не повод «не запускать тесты»."""
    from loca.tools.tests import RunTests, detect_test_command
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("")
    (tmp_path / "tests" / "test_x.py").write_text(
        "import unittest\n\n\nclass T(unittest.TestCase):\n"
        "    def test_ok(self):\n        self.assertTrue(True)\n")
    assert detect_test_command(tmp_path, pytest_ok=False) == (
        "python3 -m unittest discover -s tests -t . -q", "unittest")
    ctx = make_ctx(tmp_path)
    command, framework = RunTests().build({"pattern": "test_ok"}, ctx)
    if framework == "unittest":                 # окружение без pytest: путь fallback
        assert command.endswith("-k test_ok")
        out = RunTests().run({}, ctx)
        assert "tests PASSED" in out
    # а когда проверка вообще не запускается, исполнитель не винит субагента
    from loca.executor import looks_broken
    assert looks_broken("ModuleNotFoundError: No module named 'pytest'", 1)
    assert looks_broken("/usr/bin/python3: No module named pytest", 1)
    assert not looks_broken("FAILED (failures=2)", 1)


# ---------------------------------------------------------------- приёмка промежуточных подзадач
def test_plan_write_narrows_whole_suite_acceptance(tmp_path):
    """Kaggle 19.10: у всех подзадач «весь набор тестов» → ни один план не дошёл до конца.
    Теперь весь набор остаётся только у последней, остальным — свой тест или smoke."""
    from loca.tools.agentic import PlanWrite
    ctx = plan_ctx(tmp_path)
    (tmp_path / "app").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_format.py").write_text("def test_x():\n    pass\n")
    out = PlanWrite().run({"tasks": [
        {"id": "n1", "title": "width в config", "files": ["app/config.py"], "accept": {"kind": "tests"}},
        {"id": "n2", "title": "обрезка в format_row", "files": ["app/format.py"],
         "depends_on": ["n1"], "accept": {"kind": "tests"}},
        {"id": "n3", "title": "флаг --width", "files": ["app/cli.py"],
         "depends_on": ["n2"], "accept": {"kind": "tests"}},
    ]}, ctx)
    n1, n2, n3 = (ctx.plan.get(i) for i in ("n1", "n2", "n3"))
    assert n1["accept"] == {"kind": "smoke"}
    assert n2["accept"] == {"kind": "tests", "target": "tests/test_format.py"}
    assert n3["accept"] == {"kind": "tests"}                      # сток проверяет всё
    assert "сужена до tests/test_format.py" in out and "весь набор тестов проверит n3" in out
    # одиночная подзадача с полным набором — нормальна, её не трогаем
    from loca.plan import narrow_acceptance, normalize
    single = normalize([{"title": "x", "files": ["a.py"], "accept": {"kind": "tests"}}])
    assert narrow_acceptance(single) == [] and single[0]["accept"] == {"kind": "tests"}


def test_executor_counts_already_green_as_done(tmp_path):
    """Субагент сделал больше своей части: следующая приёмка уже зелёная — это готово, а не blocked."""
    from loca.executor import Executor
    spawned = []

    def spawn(description, prompt, kind):
        spawned.append(description)
        (tmp_path / "a.py").write_text("def f():\n    return 1\n")
        (tmp_path / "b.py").write_text("print('b')\n")          # заодно сделал и n2
        return "сделал всё"

    ctx = plan_ctx(tmp_path, spawn)
    set_plan(ctx, [
        {"id": "n1", "title": "создать a.py", "files": ["a.py"],
         "accept": {"kind": "symbol", "path": "a.py", "name": "f"}},
        {"id": "n2", "title": "создать b.py", "files": ["b.py"], "depends_on": ["n1"],
         "accept": {"kind": "cmd", "command": "python3 b.py"}},
    ])
    report = Executor(ctx).run()
    assert [n["status"] for n in ctx.plan] == ["done", "done"] and spawned == ["n1: создать a.py"]
    assert "2/2 подзадач готово" in report and ctx.plan_stats["already_done"] == 1
    assert "предыдущих" in ctx.plan.get("n2")["note"]


def test_executor_smoke_acceptance(tmp_path):
    from loca.executor import Executor
    (tmp_path / "cfg.py").write_text("DEFAULTS = {}\n")
    behaviours = iter(["noop", "broken", "good"])

    def spawn(description, prompt, kind):
        b = next(behaviours)
        if b == "broken":
            ctx.snapshots.record(tmp_path / "cfg.py", "edit_file")
            (tmp_path / "cfg.py").write_text("DEFAULTS = {\n")
        elif b == "good":
            ctx.snapshots.record(tmp_path / "cfg.py", "edit_file")
            (tmp_path / "cfg.py").write_text("DEFAULTS = {'width': 8}\n")
        return b

    ctx = plan_ctx(tmp_path, spawn, plan_max_attempts=3)
    plan = set_plan(ctx, [{"id": "n1", "title": "width", "files": ["cfg.py"], "accept": {"kind": "tests"}},
                          {"id": "n2", "title": "конец", "files": ["x.py"], "depends_on": ["n1"],
                           "accept": {"kind": "tests"}}])
    plan.get("n1")["accept"] = {"kind": "smoke"}
    plan.get("n2")["status"] = "done"                         # проверяем только n1
    Executor(ctx).run()
    node = plan.get("n1")
    assert node["status"] == "done" and node["attempts"] == 3
    assert (tmp_path / "cfg.py").read_text() == "DEFAULTS = {'width': 8}\n"
    assert "компилируются" in __import__("loca.plan", fromlist=["accept_text"]).accept_text(node)


def test_resolve_fixes_rootless_absolute_paths(tmp_path):
    ctx = make_ctx(tmp_path)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "config.py").write_text("X = 1\n")
    assert ctx.resolve("/app/config.py") == (tmp_path / "app" / "config.py").resolve()
    assert ctx.resolve("/app/new.py") == (tmp_path / "app" / "new.py").resolve()   # новый файл
    assert ctx.resolve(str(tmp_path / "app" / "config.py")) == (tmp_path / "app" / "config.py").resolve()
    assert str(ctx.resolve("/etc/hosts")) == str(Path("/etc/hosts").resolve())     # настоящий путь не трогаем


def test_read_symbol_accepts_module_paths(tmp_path):
    from loca.tools.codemap import ReadSymbol
    ctx = make_ctx(tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("from pkg.core import calc_total\n")
    (tmp_path / "pkg" / "core.py").write_text("def calc_total(items):\n    return sum(items)\n\n\ndef other():\n    pass\n")
    out = ReadSymbol().run({"name": "pkg.core"}, ctx)
    assert "это модуль pkg/core.py" in out and "calc_total" in out
    out = ReadSymbol().run({"name": "pkg.core.calc_total"}, ctx)
    assert "return sum(items)" in out and "other" not in out
    out = ReadSymbol().run({"name": "pkg.__init__"}, ctx)
    assert "pkg/__init__.py" in out


# ---------------------------------------------------------------- задание и «сначала запусти»
def test_needs_spec_and_verification_heuristics():
    from loca.agent import needs_verification
    from loca.spec import needs_spec
    auto = {"spec": "auto"}
    assert needs_spec("добавь флаг --width в cli", auto)
    assert needs_spec("переименуй calc_total во всём проекте", auto)
    assert needs_spec("как добавить флаг? добавь его в cli.py", auto)
    assert not needs_spec("почему тест падает после рефакторинга", auto)   # вопрос, не просьба
    assert not needs_spec("объясни функцию parse", auto)
    assert not needs_spec("добавь флаг", {"spec": "off"}) and needs_spec("привет", {"spec": "always"})
    assert needs_verification("что выведет этот код?") and needs_verification("почему у меня ошибка KeyError")
    assert needs_verification("What will this print?") and not needs_verification("объясни функцию parse")


class _StubLLM:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def chat(self, messages, tools=None, think=None, **kw):
        from loca.llm import LLMResponse
        self.calls.append(messages)
        assert tools is None                       # задание пишется без инструментов
        return LLMResponse(content=self.replies.pop(0))


def test_confirm_loop_accept_correct_cancel(tmp_path):
    from loca.spec import SPEC_MARK, confirm_loop
    ctx = make_ctx(tmp_path)
    (tmp_path / "cli.py").write_text("def main():\n    pass\n")
    shown = []

    llm = _StubLLM(["Цель: флаг --width\nФайлы: cli.py\nВопросы: обрезать с «…»?",
                    "Цель: флаг --width, обрезать с «…»\nФайлы: cli.py\nВопросы: нет"])
    answers = iter(["да, с многоточием", ""])
    text = confirm_loop(llm, ctx, "добавь флаг --width", ask=lambda h: next(answers), show=shown.append)
    assert SPEC_MARK in text and "обрезать с «…»" in text and "да, с многоточием" in text
    assert len(llm.calls) == 2 and "Уточнение пользователя 1" in llm.calls[1][1]["content"]
    assert "cli.py" in llm.calls[0][1]["content"]                 # карта проекта в запросе
    assert len(shown) == 2

    llm = _StubLLM(["Цель: удалить всё"])
    assert confirm_loop(llm, ctx, "удали лишнее", ask=lambda h: "n", show=shown.append) is None


def test_check_spec_fixes_paths_and_asks_on_vague_request(tmp_path):
    from loca.spec import check_spec, is_vague
    (tmp_path / "shop").mkdir()
    (tmp_path / "shop" / "cart.py").write_text("class Cart: pass\n")
    ctx = make_ctx(tmp_path)
    # так ответила настоящая Qwen2.5-Coder-7B на «Добавь промокоды.»: цель додумана, вопросов нет,
    # существующий файл помечен «(новый)», один путь выдуман
    spec = ("Цель: Добавить промокоды в систему корзины.\n"
            "Файлы: shop/cart.py (новый) · /app/shop/promo.py\n"
            "Ограничения: Нет\nГотово, когда: тесты проходят\nПредположения: Нет\nВопросы: нет")
    out = check_spec(spec, "Добавь промокоды.", ctx)
    assert "shop/cart.py (новый)" not in out and "shop/cart.py" in out
    assert "promo.py (нет в проекте)" in out
    assert "Вопросы: 1) Просьба без подробностей" in out
    # ясная просьба: ничего не добавляем
    clear = ("Цель: discount\nФайлы: shop/cart.py\nОграничения: нет\nГотово, когда: `pytest -q`\n"
             "Предположения: нет\nВопросы: нет")
    assert check_spec(clear, "Добавь в shop/cart.py метод discount(percent)", ctx) == clear
    assert is_vague("Сделай, чтобы корзина работала лучше.")
    assert not is_vague("Поправь /app/shop/report.py: итог с двумя знаками")
    assert not is_vague("сделай calc_total быстрее")


def test_headless_skips_spec_and_verify_nudge_reaches_model(tmp_path, capsys):
    (tmp_path / "app.py").write_text("print(sum([0.1] * 3))\n")
    fake = FakeOllama([{"content": "Выведет 0.30000000000000004."},
                       {"content": "Готово."}])
    from loca.cli import main
    main(["-p", "--yolo", "--host", fake.url, "-m", "fake:1b", "--cwd", str(tmp_path),
          "что выведет app.py?"])
    main(["-p", "--yolo", "--host", fake.url, "-m", "fake:1b", "--cwd", str(tmp_path),
          "добавь флаг --width в app.py"])
    fake.close()
    first, second = fake.requests[0], fake.requests[1]
    assert first["tools"] and second["tools"]          # в -p задание не запрашивалось
    # «что выведет app.py?»: агент сам запустил файл ДО первого ответа модели
    tool_msgs = [m["content"] for m in first["messages"] if m["role"] == "tool"]
    assert any("print(sum([0.1] * 3))" in c for c in tool_msgs)        # исходник показан
    assert any("0.30000000000000004" in c for c in tool_msgs)          # и настоящий вывод
    assert any("was RUN" in m["content"] for m in first["messages"] if m["role"] == "user")
    assert not any("RUN" in m["content"] for m in second["messages"] if m["role"] == "user")


def test_verify_commands_files_snippets_and_unsafe(tmp_path):
    from loca.agent import verify_commands
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "tricky.py").write_text("print(round(2.675, 2))\n")
    (tmp_path / "wipe.py").write_text("import shutil\nshutil.rmtree('/tmp/x')\n")
    cmds, skipped, sources = verify_commands("Что выведет python3 pkg/tricky.py?", tmp_path)
    assert cmds == ["python3 pkg/tricky.py"] and not skipped and sources == ["pkg/tricky.py"]
    cmds, skipped, _ = verify_commands("что выведет wipe.py и missing.py?", tmp_path)
    assert cmds == [] and "shutil.rmtree" in skipped[0]
    q = "Что выведет код?\n```python\nx = [1]\nprint(x * 2)\n```\nа этот?\n```js\nconsole.log(1)\n```"
    cmds, _, sources = verify_commands(q, tmp_path)
    assert sources == []                     # код из вопроса модель и так видит
    assert cmds == ["python3 .loca/scratch/snippet_1.py", "node .loca/scratch/snippet_2.js"]
    assert (tmp_path / ".loca/scratch/snippet_1.py").read_text() == "x = [1]\nprint(x * 2)\n"
    cmds, skipped, _ = verify_commands("что выведет?\n```python\nname = input()\n```", tmp_path)
    assert cmds == [] and "input" in skipped[0]
    sql = "почему пусто?\n```sql\nCREATE TABLE t(a);\nINSERT INTO t VALUES (1);\nSELECT a FROM t;\n```"
    cmds, _, _ = verify_commands(sql, tmp_path)
    assert cmds[0].startswith("python3 .loca/scratch/run_sql.py")
    import subprocess
    out = subprocess.run(cmds[0].split(), cwd=tmp_path, capture_output=True, text=True).stdout
    assert out.splitlines() == ["a", "1", "(1 rows)"]


def test_autorun_falls_back_to_reminder_when_nothing_to_run(tmp_path, capsys):
    fake = FakeOllama([{"content": "Ответ."}])
    from loca.cli import main
    main(["-p", "--yolo", "--host", fake.url, "-m", "fake:1b", "--cwd", str(tmp_path),
          "почему падает мой скрипт, который я не показал?"])
    fake.close()
    msgs = fake.requests[0]["messages"]
    assert not any(m["role"] == "tool" for m in msgs)
    assert any("RUN it" in m["content"] for m in msgs if m["role"] == "user")


def test_interactive_ask_confirms_spec_first(tmp_path, monkeypatch, capsys):
    import argparse
    from loca.cli import App
    from loca.spec import SPEC_MARK
    (tmp_path / "app.py").write_text("print('x')\n")
    fake = FakeOllama([{"content": "Цель: флаг --width\nФайлы: app.py\nГотово, когда: python3 app.py --width 3"},
                       {"content": "Сделал."}])
    args = argparse.Namespace(cwd=str(tmp_path), model="fake:1b", ctx=None, host=fake.url,
                              max_seconds=None, yolo=True, mode=None, think=False, print=False,
                              resume=None, continue_=False)
    app = App(args)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    app.ask("добавь флаг --width в app.py")
    fake.close()
    assert "tools" not in fake.requests[0] or not fake.requests[0]["tools"]   # запрос задания
    sent = [m["content"] for m in fake.requests[1]["messages"] if m["role"] == "user"]
    assert any(SPEC_MARK in c and "python3 app.py --width 3" in c for c in sent)
    assert "Понял задачу так" in capsys.readouterr().out


# ---------------------------------------------------------------- llama.cpp
def test_to_openai_messages_links_tool_results():
    from loca.llm import to_openai_messages
    out = to_openai_messages([
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "read_file", "arguments": {"path": "a.py"}}},
            {"function": {"name": "grep", "arguments": {"pattern": "x"}}}]},
        {"role": "tool", "content": "A", "tool_name": "read_file"},
        {"role": "tool", "content": "B", "tool_name": "grep"},
        {"role": "assistant", "content": "ok"},
    ])
    ids = [c["id"] for c in out[2]["tool_calls"]]
    assert ids == ["call_1", "call_2"] and [m["tool_call_id"] for m in out[3:5]] == ids
    assert out[2]["tool_calls"][0]["function"]["arguments"] == '{"path": "a.py"}'
    assert "tool_name" not in out[3] and out[5] == {"role": "assistant", "content": "ok"}


def test_llamacpp_backend_end_to_end(tmp_path, capsys):
    from fake_llamacpp import FakeLlamaCpp
    (tmp_path / "app.py").write_text("print('helo')\n")
    fake = FakeLlamaCpp([
        {"reasoning": "надо прочитать файл", "tool_calls": [("read_file", {"path": "app.py"})]},
        {"tool_calls": [("edit_file", {"path": "app.py", "old_string": "helo", "new_string": "hello"})]},
        {"content": "Исправил опечатку."},
    ], n_ctx=8192)
    from loca.cli import main
    main(["-p", "--yolo", "--backend", "llamacpp", "--host", fake.url, "--cwd", str(tmp_path),
          "исправь опечатку в app.py"])
    fake.close()
    assert (tmp_path / "app.py").read_text() == "print('hello')\n"
    assert "Исправил" in capsys.readouterr().out
    first, last = fake.requests[0], fake.requests[-1]
    assert first["stream"] and first["tools"] and first["cache_prompt"]
    assert "options" not in first and "num_ctx" not in json.dumps(first)    # контекст — у сервера
    msgs = last["messages"]
    assistant = [m for m in msgs if m["role"] == "assistant" and m.get("tool_calls")]
    tool = [m for m in msgs if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool] == [a["tool_calls"][0]["id"] for a in assistant]
    assert '"old_string": "helo"' in assistant[1]["tool_calls"][0]["function"]["arguments"]


def test_llamacpp_syncs_context_and_explains_offline(tmp_path):
    from fake_llamacpp import FakeLlamaCpp
    from loca.llm import LLMError, LlamaCppClient, make_client
    fake = FakeLlamaCpp([], n_ctx=12288)
    cfg = {"backend": "llamacpp", "host": fake.url, "model": "", "num_ctx": 16384, "temperature": 0.2}
    client = make_client(cfg)
    assert isinstance(client, LlamaCppClient)
    client.sync()
    assert client.num_ctx == 12288 and cfg["num_ctx"] == 12288 and client.model == "qwen-test.gguf"
    assert client.list_models() == ["qwen-test.gguf"]
    fake.close()
    dead = LlamaCppClient({"host": "http://127.0.0.1:9", "num_ctx": 4096, "temperature": 0.2})
    with pytest.raises(LLMError, match="llama-server"):
        dead.chat([{"role": "user", "content": "hi"}])
    # адрес Ollama по умолчанию подменяется адресом llama-server
    assert LlamaCppClient({"host": "http://localhost:11434", "num_ctx": 1, "temperature": 0}).host.endswith(":8080")


def test_plan_test_file_acceptance_is_real_and_runnable(tmp_path):
    """hard_width_option на Qwen2.5-Coder-7B: pattern "test_config.py" (файла нет) уходил в
    `unittest -k` → «Ran 0 tests» → откат первой же подзадачи."""
    import subprocess
    from loca.plan import fix_test_targets
    from loca.tools.agentic import find_test_for, locate_test_file
    from loca.tools.tests import RunTests
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("")
    (tmp_path / "tests" / "test_cli.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n")
    (tmp_path / "config.py").write_text("WIDTH = 8\n")
    (tmp_path / "cli.py").write_text("print(1)\n")
    ctx = make_ctx(tmp_path)
    nodes = [
        {"id": "n1", "files": ["config.py"], "depends_on": [], "accept": {"kind": "tests", "pattern": "test_config.py"}},
        {"id": "n2", "files": ["cli.py"], "depends_on": ["n1"], "accept": {"kind": "tests", "pattern": "test_cli.py"}},
        {"id": "n3", "files": ["cli.py"], "depends_on": ["n2"], "accept": {"kind": "tests", "pattern": "test_width"}},
    ]
    notes = fix_test_targets(nodes, lambda rel: locate_test_file(rel, ctx), lambda n: find_test_for(n, ctx))
    assert nodes[0]["accept"] == {"kind": "smoke"}                         # файла нет → smoke
    assert nodes[1]["accept"] == {"kind": "tests", "target": "tests/test_cli.py"}
    assert nodes[2]["accept"] == {"kind": "tests", "pattern": "test_width"}  # имя теста — не трогаем
    assert any("test_config.py" in n for n in notes)
    # и такой target действительно запускает 1 тест, а не «Ran 0 tests» — в обоих раннерах
    import loca.tools.tests as tt
    real = tt.detect_test_command
    try:
        for found in (("python3 -m unittest discover -s tests -t . -q", "unittest"), real(tmp_path)):
            tt.detect_test_command = lambda cwd, f=found: f
            command, framework = RunTests().build({"target": "tests/test_cli.py"}, ctx)
            out = subprocess.run(command, shell=True, cwd=tmp_path, capture_output=True, text=True)
            text = out.stdout + out.stderr
            assert out.returncode == 0 and ("Ran 1 test" in text or "1 passed" in text), (command, text)
    finally:
        tt.detect_test_command = real
