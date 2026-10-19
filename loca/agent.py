"""The agent loop: model -> tool calls -> results -> model ... until a final answer."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

from . import hooks
from .llm import LLMError, LLMResponse, OllamaClient, ToolCall  # noqa: F401
from .memory import COMPACT_PROMPT, system_prompt
from .tools import Tool, ToolContext, ToolError, subagent_tools, truncate
from .ui import UI, Spinner, dim


@dataclass
class Usage:
    prompt_tokens: int = 0      # size of the last prompt (≈ context in use)
    total_in: int = 0
    total_out: int = 0
    requests: int = 0
    steps: int = 0
    seconds: float = 0.0


# после этого в выводе инструмента стоит подумать, а не отвечать рефлексом
TROUBLE_MARKERS = ("tests FAILED", "Traceback", "exit code", "Error:", "not found in file",
                   "Denied", "TIMED OUT")


def needs_thought(text: str) -> bool:
    return any(marker in text for marker in TROUBLE_MARKERS)


def plan_text(plan) -> str:
    return plan.summary()


_PATH_RE = re.compile(r"[\w./-]+\.(?:py|pyi|js|jsx|ts|tsx|go|rs|rb|java|kt|swift|c|h|cpp|"
                      r"hpp|lua|php|sh|json|toml|yaml|yml|md|cfg|ini)\b")
# слова, которые сами по себе означают работу по всему проекту
_SCOPE_WORDS = ("везде", "во всех", "во всём проекте", "во всем проекте", "по всему проекту",
                "everywhere", "across the", "project-wide", "весь проект")
# а эти — только вместе с парой упомянутых файлов
_ACTION_WORDS = ("переименуй", "rename", "отрефактор", "refactor", "вынеси", "extract",
                 "перенеси", "move", "раздели", "split")

PLAN_GATE_TEXT = (
    "You have been editing on your own for several steps and there is still no plan. "
    "Stop improvising. Call plan_write now with the REMAINING work: 2-7 subtasks, each with "
    "`files` it changes, `depends_on`, and an executable `accept` check, e.g. "
    '{"kind": "tests", "target": "tests/test_x.py"} or {"kind": "cmd", "command": "python3 main.py"}. '
    "Then call run_plan — each subtask gets a fresh subagent and its check is run for real. "
    "If what is left is genuinely one small edit, say so in one sentence and finish instead.")


# вопросы о том, что код ДЕЛАЕТ: ответ из головы на 4–7B часто неверен, а запуск — нет
_VERIFY_RE = re.compile(
    r"(что\s+(он\s+|она\s+|оно\s+|это\s+|код\s+|программа\s+|функция\s+)?"
    r"(выведет|выводит|напечата|вернёт|вернет|возвращает|произойд)"
    r"|какой\s+(будет\s+)?(вывод|результат)"
    r"|почему\b[^.?!\n]{0,40}?(падает|упал|ошибк|exception|выдаёт|выдает|выводит|"
    r"не\s+работает|возвращает|работает\s+не)"
    r"|traceback|what\s+(does|will)\b.*\b(print|output|return)|why\s+does\b.*\b(fail|crash|print|return))",
    re.I | re.S)

VERIFY_TEXT = (
    "[reminder] This question is about what code actually does. Do not answer from memory: "
    "first RUN it and look at the real result — the project file via bash "
    "(`python3 path.py`, `node file.js`), the relevant tests via run_tests, or the snippet from "
    "the question saved with write_file to .loca/scratch/snippet.py and run. Then explain the "
    "mechanism behind the REAL output and quote it. If running is unsafe (deletes files, network, "
    "waits for input), say so and trace the execution step by step instead.")


def needs_verification(text: str) -> bool:
    return bool(_VERIFY_RE.search(text or ""))


# ---- автозапуск: маленькая модель напоминание «сначала запусти» игнорирует (пробник на
# Qwen2.5-Coder-7B: ответила «файла не существует», не вызвав ни одного инструмента), поэтому
# код запускает сам агент — до первого ответа модели, обычным вызовом bash с проверкой прав.
_FENCE_RE = re.compile(r"```([\w+-]*)[ \t]*\n(.*?)```", re.S)
_FILE_RE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w-]+\.(?:py|js|mjs|sql))\b")
# что не запускаем сами: удаляет/пишет файлы, сеть, процессы, ждёт ввода
_UNSAFE_RE = re.compile(
    r"\b(?:os\.(?:remove|unlink|rmdir|removedirs|system|popen|kill)|shutil\.(?:rmtree|move)|"
    r"subprocess|socket|requests|urllib|http\.client|input\s*\(|sys\.stdin|"
    r"open\s*\([^)]*['\"][wax+]|\.(?:unlink|rmdir|write_text|write_bytes)\s*\(|"
    r"child_process|fs\.(?:unlink|rm|write)|process\.stdin|readline|"
    r"\b(?:DROP|DELETE|ATTACH)\b|while\s+True|setInterval)", re.I)
_LANG = {"python": "py", "py": "py", "python3": "py", "": "py",
         "javascript": "js", "js": "js", "node": "js", "sql": "sql", "sqlite": "sql"}
_SQL_RUNNER = (
    "import sqlite3, sys\n"
    "con = sqlite3.connect(':memory:')\n"
    "for st in [s.strip() for s in open(sys.argv[1], encoding='utf-8').read().split(';') if s.strip()]:\n"
    "    cur = con.execute(st)\n"
    "    if cur.description:\n"
    "        print(' | '.join(d[0] for d in cur.description))\n"
    "        rows = cur.fetchall()\n"
    "        for r in rows: print(' | '.join('NULL' if v is None else str(v) for v in r))\n"
    "        print(f'({len(rows)} rows)')\n")


def verify_commands(text: str, cwd) -> tuple[list[str], list[str]]:
    """Что запустить до ответа: (команды, причины пропуска, файлы проекта, которые надо показать
    модели вместе с выводом — без исходника она объясняет вывод выдуманным кодом)."""
    from pathlib import Path
    cmds, skipped, sources = [], [], []
    scratch = Path(cwd) / ".loca" / "scratch"

    def safe(code: str, label: str) -> bool:
        m = _UNSAFE_RE.search(code)
        if m:
            skipped.append(f"{label}: не запускаю сам — в коде `{m.group(0).strip()}`")
        return not m

    for i, m in enumerate(_FENCE_RE.finditer(text or "")):
        lang = _LANG.get(m.group(1).lower())
        code = m.group(2)
        if lang is None or not code.strip() or not safe(code, f"код из вопроса #{i + 1}"):
            continue
        scratch.mkdir(parents=True, exist_ok=True)
        f = scratch / f"snippet_{i + 1}.{lang}"
        f.write_text(code, encoding="utf-8")
        rel = f.relative_to(cwd)
        if lang == "py":
            cmds.append(f"python3 {rel}")
        elif lang == "js":
            cmds.append(f"node {rel}")
        else:
            runner = scratch / "run_sql.py"
            runner.write_text(_SQL_RUNNER, encoding="utf-8")
            cmds.append(f"python3 {runner.relative_to(cwd)} {rel}")
    outside = _FENCE_RE.sub(" ", text or "")
    for m in _FILE_RE.finditer(outside):
        path = Path(cwd) / m.group(1)
        if not path.is_file() or path.suffix == ".sql":
            continue
        try:
            code = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not safe(code, m.group(1)):
            continue
        cmd = ("node " if path.suffix in (".js", ".mjs") else "python3 ") + m.group(1)
        if cmd not in cmds:
            cmds.append(cmd)
            sources.append(m.group(1))
    return cmds[:2], skipped, sources[:2]


AUTORUN_TEXT = (
    "[loca] Before you answer, the code from the question was RUN (see the bash result above; "
    "the source is above too). Base the answer on that REAL output: quote it exactly, then explain "
    "line by line which code in the source produces it and why (the language mechanism). "
    "Never state a different output and never invent code that is not in the source.")


def looks_multifile(text: str) -> bool:
    """Запрос, который почти наверняка не влезает в один файл и один шаг."""
    low = (text or "").lower()
    if any(word in low for word in _SCOPE_WORDS):
        return True
    paths = {m.group(0) for m in _PATH_RE.finditer(text or "")}
    if len(paths) >= 3:
        return True
    return len(paths) >= 2 and any(word in low for word in _ACTION_WORDS)


def estimate_tokens(messages: list[dict]) -> int:
    # rough: ~3.2 chars/token for mixed code / Russian text
    return int(len(json.dumps(messages, ensure_ascii=False)) / 3.2)


class Agent:
    def __init__(self, llm: OllamaClient, tools: list[Tool], ctx: ToolContext, ui: UI,
                 max_steps: int = 40, sub: bool = False, sub_kind: str = "research"):
        self.llm = llm
        self.tools = {t.name: t for t in tools}
        self.ctx = ctx
        self.ui = ui
        self.max_steps = max_steps
        self.sub = sub
        self.sub_kind = sub_kind
        self.usage = Usage()
        self.tool_counts: dict[str, int] = {}
        self.tool_errors = 0    # сколько вызовов вернули ошибку — признак пробуксовки
        self.trace: list[dict] = []   # последние вызовы: видно, на чём агент зациклился
        self.on_step = None     # колбэк после каждого шага (пишет метрики на диск)
        self._compact_floor = 0       # порог, ниже которого сжатие больше не пробуем
        self._ratio = 1.0          # calibrates estimate_tokens against real counts
        self.messages: list[dict] = [{"role": "system", "content": self._system()}]

    # ------------------------------------------------------------------ setup
    def _system(self) -> str:
        return system_prompt(self.ctx.cwd, self.ctx.cfg, self.ctx.permissions.mode, sub=self.sub,
                             vars_summary=self.ctx.vars.summary(), subagent_kind=self.sub_kind)

    def refresh_system(self) -> None:
        self.messages[0] = {"role": "system", "content": self._system()}

    def reset(self) -> None:
        self.ctx.plan.clear()
        self.ctx.read_files.clear()
        self.ctx.vars.clear()
        self.ctx.edited = False
        self.ctx.tests_run = 0
        self.messages = [{"role": "system", "content": self._system()}]
        self.usage.prompt_tokens = 0

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self.tools.values()]

    # ------------------------------------------------------------------ context
    def context_tokens(self) -> int:
        return int(estimate_tokens(self.messages) * self._ratio)

    def _turn_starts(self) -> list[int]:
        return [i for i, m in enumerate(self.messages) if m["role"] == "user"]

    def _trim_old_tool_outputs(self) -> int:
        keep = self.ctx.cfg["keep_recent_turns"]
        starts = self._turn_starts()
        if len(starts) > keep:
            boundary = starts[-keep]
        else:  # few turns but maybe one long agentic turn: keep only the tail intact
            boundary = max(0, len(self.messages) - 6)
        limit = self.ctx.cfg["old_tool_output"]
        saved = 0
        for m in self.messages[1:boundary]:
            if m["role"] == "tool" and len(m["content"]) > limit:
                saved += len(m["content"]) - limit
                m["content"] = truncate(m["content"], limit)
        return saved

    def maybe_compact(self, force: bool = False) -> None:
        budget = self.ctx.cfg["num_ctx"] * self.ctx.cfg["compact_threshold"]
        if not force and self.context_tokens() < budget:
            return
        if not force and self._compact_floor and self.context_tokens() < self._compact_floor:
            return                     # прошлое сжатие не помогло, ждём реального роста
        if not force:
            self._trim_old_tool_outputs()
            if self.context_tokens() < budget:
                self.ui.info("✂ старые результаты инструментов сокращены")
                return
        self.compact()

    def compact(self) -> None:
        msgs = self.messages
        if len(msgs) < 4:
            return
        starts = self._turn_starts()
        if len(starts) >= 2:
            cut = starts[-1]                     # keep the current turn intact
            head_user = None
        else:
            cut = max(2, len(msgs) - 6)          # one long turn: keep its tail
            while cut < len(msgs) and msgs[cut]["role"] == "tool":
                cut -= 1
            head_user = msgs[1]["content"] if len(msgs) > 1 else ""
        old = msgs[1:cut]
        if not old:
            return
        transcript = []
        for m in old:
            content = m.get("content") or ""
            if m["role"] == "tool":
                content = truncate(content, 400)
            line = f"[{m['role']}] {content}"
            for tc in m.get("tool_calls") or []:
                fn = tc["function"]
                line += f"\n  -> {fn['name']}({json.dumps(fn['arguments'], ensure_ascii=False)[:200]})"
            transcript.append(line)
        text = truncate("\n".join(transcript), int(self.ctx.cfg["num_ctx"] * 2.2))
        spinner = Spinner("сжимаю контекст").start()
        try:
            resp = self.llm.chat([{"role": "system", "content": COMPACT_PROMPT},
                                  {"role": "user", "content": text}], think=False)
        finally:
            spinner.stop()
        summary = resp.content.strip()
        new = [msgs[0]]
        if head_user is not None:
            new.append({"role": "user", "content":
                        f"{head_user}\n\n[Progress so far, summarized]\n{summary}"})
        else:
            new.append({"role": "user", "content": f"[Summary of earlier conversation]\n{summary}"})
            new.append({"role": "assistant", "content": "Understood."})
        if len(self.ctx.plan):
            new[1]["content"] += "\n\nCurrent plan:\n" + plan_text(self.ctx.plan)
        if len(self.ctx.vars):
            new[1]["content"] += ("\n\nSaved variables (still available as {{name}}):\n"
                                  + self.ctx.vars.summary())
        before = self.context_tokens()
        candidate = new + msgs[cut:]
        after = int(estimate_tokens(candidate) * self._ratio)
        if after >= before:
            # пересказ оказался длиннее вырезанного: откатываемся и больше не пробуем,
            # пока контекст не вырастет ещё — иначе сжатие зацикливается
            self._compact_floor = int(before * 1.2)
            self.ui.warn(f"✂ сжатие не помогло (~{before} → ~{after}), оставляю как было")
            return
        self.messages = candidate
        self._compact_floor = 0
        self.ui.info(f"✂ контекст сжат: ~{before} → ~{self.context_tokens()} токенов")

    # ------------------------------------------------------------------ loop
    def _think_for(self, step: int):
        """Размышления там, где они окупаются: план в начале шага и разбор ошибки."""
        if self.llm.think or not self.ctx.cfg.get("think_smart", True):
            return None                      # глобальный режим важнее
        if step == 1:
            return True                      # планирование
        last = self.messages[-1] if self.messages else {}
        if last.get("role") == "tool" and needs_thought(str(last.get("content") or "")):
            return True                      # что-то упало — стоит подумать
        return None

    def _call_model(self, think=None) -> LLMResponse:
        spinner = Spinner().start()
        self.ui.begin_stream()

        def on_token(t: str) -> None:
            spinner.stop()
            self.ui.token(t)

        def on_think(t: str) -> None:
            spinner.stop()
            self.ui.think_token(t)

        try:
            resp = self.llm.chat(self.messages, tools=self.schemas(),
                                 on_token=on_token, on_think=on_think, think=think)
        finally:
            spinner.stop()
            self.ui.end_stream()
        est = estimate_tokens(self.messages)
        if resp.prompt_tokens and est:
            self._ratio = max(0.3, min(3.0, resp.prompt_tokens / est))
        u = self.usage
        u.prompt_tokens = resp.prompt_tokens + resp.output_tokens
        u.total_in += resp.prompt_tokens
        u.total_out += resp.output_tokens
        u.requests += 1
        u.seconds += resp.seconds
        return resp

    def run(self, user_text: str) -> str:
        self.messages.append({"role": "user", "content": user_text})
        recent_calls: list[str] = []
        recent_failures: list[str] = []
        nudged = False
        nudged_tests = False
        nudged_plan = False
        nudged_stop = False
        nudged_gate = False
        self.ctx.edited = False
        self.ctx.tests_run = 0
        self.ctx.touched = set()
        final = ""
        budget = float(self.ctx.cfg.get("max_seconds") or 0)
        if not self.sub:
            self.ctx.budget_hit = False
            if budget > 0:
                self.ctx.deadline = time.time() + budget
            # без max_seconds уважаем дедлайн, выставленный снаружи (оркестратор, тесты)
        # большая задача — просим план сразу, не дожидаясь, пока модель увязнет
        if (not self.sub and "plan_write" in self.tools and not len(self.ctx.plan)
                and looks_multifile(user_text)):
            self.messages.append({"role": "user", "content":
                                  "[reminder] This touches several files: start with plan_write "
                                  "(subtasks + executable accept checks), then run_plan."})
            nudged_gate = True
        # «что выведет / почему падает»: сначала запуск, потом объяснение
        if (not self.sub and self.ctx.cfg.get("verify_first", True) and "bash" in self.tools
                and needs_verification(user_text)):
            if not self._autorun(user_text):
                self.messages.append({"role": "user", "content": VERIFY_TEXT})
        for step in range(1, self.max_steps + 1):
            if self.ctx.deadline and time.time() > self.ctx.deadline:
                self.ctx.budget_hit = True
                limit = f" ({int(budget)}s)" if budget > 0 else ""
                self.ui.warn(f"⏱ лимит времени{limit} исчерпан, останавливаюсь")
                tail = ""
                if len(self.ctx.plan):
                    tail = "\n\nСостояние плана на момент остановки:\n" + self.ctx.plan.summary()
                    self.ui.info(self.ctx.plan.summary())
                return (final or "Время на задачу вышло: работа не закончена.") + tail
            self.maybe_compact()
            self.usage.steps += 1
            try:
                resp = self._call_model(think=self._think_for(step))
            except KeyboardInterrupt:
                self.messages.append({"role": "assistant", "content": "[interrupted by user]"})
                self.ui.warn("⏹ прервано")
                return final
            except LLMError as e:
                self.ui.error(str(e))
                if self.messages[-1]["role"] == "user":
                    self.messages.pop()
                return final

            msg: dict = {"role": "assistant", "content": resp.content}
            if resp.tool_calls:
                msg["tool_calls"] = [{"function": {"name": c.name, "arguments": c.arguments}}
                                     for c in resp.tool_calls]
            self.messages.append(msg)
            if resp.content.strip():
                final = resp.content

            if not resp.tool_calls:
                if not resp.content.strip() and not nudged:
                    nudged = True
                    self.messages.append({"role": "user", "content":
                                          "Continue: call a tool or give your final answer."})
                    continue
                reminder = self._closing_reminder(nudged_tests, nudged_plan)
                if reminder:
                    if reminder[0] == "tests":
                        nudged_tests = True
                    else:
                        nudged_plan = True
                    self.messages.append({"role": "user", "content": reminder[1]})
                    continue
                stop_text = "" if self.sub else hooks.run("stop", self.ctx)
                if stop_text:
                    self.ui.info(stop_text)
                    if "→ exit " in stop_text and not nudged_stop:
                        nudged_stop = True
                        self.messages.append({"role": "user", "content":
                                              "Проверка после ответа не прошла:\n" + stop_text
                                              + "\nРазберитесь и доведите до зелёного."})
                        continue
                return final

            for i, call in enumerate(resp.tool_calls):
                try:
                    result = self.execute(call)
                except KeyboardInterrupt:
                    for rest in resp.tool_calls[i:]:
                        self.messages.append({"role": "tool", "tool_name": rest.name,
                                              "content": "Interrupted by user."})
                    self.ui.warn("⏹ прервано")
                    return final
                sig = f"{call.name}:{json.dumps(call.arguments, sort_keys=True)}"
                recent_calls.append(sig)
                failed = result.startswith(("Error:", "Denied")) or "ALREADY EXISTS" in result
                if failed and recent_failures[-1:] == [sig]:
                    result += ("\n\nNOTE: this exact call has now failed twice. Do NOT send it a "
                               "third time — read the error above and do what it says, or take "
                               "another route (read_file first, another path, another tool). "
                               "If you are stuck, say so instead of repeating.")
                if failed:
                    recent_failures.append(sig)
                else:
                    recent_failures.clear()
                if recent_calls[-3:] == [sig] * 3:
                    result += ("\n\nNOTE: you made this exact call 3 times in a row. "
                               "Stop repeating it; try a different approach or answer the user.")
                self.messages.append({"role": "tool", "tool_name": call.name, "content": result})
            if self.on_step is not None:
                try:
                    self.on_step()
                except Exception:  # noqa: BLE001  метрики не должны ронять работу
                    pass
            if (not self.sub and not nudged_gate and "plan_write" in self.tools
                    and not len(self.ctx.plan)
                    and step >= int(self.ctx.cfg.get("plan_gate_steps", 6))
                    and len(self.ctx.touched) >= int(self.ctx.cfg.get("plan_gate_files", 2))):
                nudged_gate = True
                self.messages.append({"role": "user", "content": PLAN_GATE_TEXT})
                continue
            self._maybe_remind(step)
        self.ui.warn(f"Достигнут лимит шагов ({self.max_steps}). Напишите «продолжай», чтобы продолжить.")
        return final

    def _autorun(self, user_text: str) -> bool:
        """Запускает код из вопроса сам. True — реальный вывод уже в истории."""
        try:
            cmds, skipped, sources = verify_commands(user_text, self.ctx.cwd)
        except OSError:
            return False
        for why in skipped:
            self.ui.info(why)
        if not cmds:
            return False
        for rel in sources:              # сначала исходник: объяснять надо его, а не догадку о нём
            if "read_file" not in self.tools:
                break
            call = ToolCall("read_file", {"path": rel})
            result = self.execute(call)
            self.messages.append({"role": "assistant", "content": "",
                                  "tool_calls": [{"function": {"name": "read_file", "arguments": call.arguments}}]})
            self.messages.append({"role": "tool", "tool_name": "read_file", "content": result})
        ran = False
        for cmd in cmds:
            call = ToolCall("bash", {"command": cmd, "timeout": 30})
            result = self.execute(call)
            self.messages.append({"role": "assistant", "content": "",
                                  "tool_calls": [{"function": {"name": "bash", "arguments": call.arguments}}]})
            self.messages.append({"role": "tool", "tool_name": "bash", "content": result})
            ran = ran or not result.startswith(("Denied", "Error:"))
        if ran:
            self.messages.append({"role": "user", "content": AUTORUN_TEXT})
        return ran

    def _maybe_remind(self, step: int) -> None:
        """Periodically re-state the plan, the way Claude Code re-injects its plan."""
        every = int(self.ctx.cfg.get("reminder_every") or 0)
        if not every or step % every or not len(self.ctx.plan):
            return
        if not self.ctx.plan.unfinished():
            return
        self.messages.append({"role": "user", "content":
                              "[reminder] Your plan right now:\n" + plan_text(self.ctx.plan)
                              + "\nContinue it with run_plan, or send a new plan_write if the "
                                "plan no longer fits the facts."})

    def _closing_reminder(self, nudged_tests: bool, nudged_plan: bool):
        """One-shot nudges before accepting a final answer: verify, then finish the plan."""
        if self.sub:
            return None
        if (not nudged_tests and self.ctx.edited and self.ctx.tests_run == 0
                and "run_tests" in self.tools):
            return ("tests",
                    "You changed files but never ran the tests. Run run_tests now. If the project "
                    "has no test covering this change, write one first and then run it. If tests "
                    "genuinely cannot run here, say so explicitly in your answer.")
        if not nudged_plan and self.ctx.edited and len(self.ctx.plan):
            if self.ctx.plan.unfinished():
                return ("plan",
                        "Your plan still has unfinished subtasks:\n" + plan_text(self.ctx.plan)
                        + "\nRun run_plan to finish them, or send a new plan_write if they are no "
                          "longer needed. If you already did them by hand, say so explicitly.")
        return None

    def _trace(self, name: str, summary: str, result: str, ok: bool) -> None:
        limit = int(self.ctx.cfg.get("trace_len", 60))
        if not ok:
            self.tool_errors += 1
        if limit <= 0:
            return
        head = " ".join((result or "").strip().splitlines()[:1])[:120]
        self.trace.append({"step": self.usage.steps, "tool": name,
                           "args": str(summary)[:80], "ok": ok, "out": head})
        if len(self.trace) > limit:
            del self.trace[:-limit]

    def execute(self, call: ToolCall) -> str:
        tool = self.tools.get(call.name)
        if tool is None:
            self.ui.tool_call(call.name, "?")
            msg = f"Unknown tool '{call.name}'. Available tools: {', '.join(self.tools)}"
            self.ui.tool_result(msg, error=True)
            self._trace(call.name, "?", msg, False)
            return "Error: " + msg
        self.tool_counts[tool.name] = self.tool_counts.get(tool.name, 0) + 1
        args = dict(call.arguments or {})
        if len(self.ctx.vars):
            args = self.ctx.vars.expand_args(args, tool.expand_vars, tool.expand_vars_whole)
        try:
            tool.validate(args)
        except ToolError as e:
            self.ui.tool_call(call.name, "…")
            self.ui.tool_result(str(e), error=True)
            self._trace(call.name, "…", str(e), False)
            return f"Error: {e}"
        self.ui.tool_call(tool.name, tool.summary(args))
        allowed, feedback = self.ctx.permissions.check(tool, args, self.ctx)
        if not allowed:
            self.ui.tool_result(feedback or "отклонено", error=True)
            self._trace(tool.name, tool.summary(args), feedback or "Denied.", False)
            return feedback or "Denied."
        target, snap_mark = None, None
        if tool.kind == "edit" and args.get("path"):
            try:
                target = self.ctx.resolve(str(args["path"]))
            except Exception:  # noqa: BLE001
                target = None
            if target is not None and self.ctx.snapshots is not None:
                snap_mark = self.ctx.snapshots.mark()
                self.ctx.snapshots.record(target, tool.name)
        try:
            result = tool.run(args, self.ctx)
        except ToolError as e:
            self._drop_snapshot(snap_mark)
            self.ui.tool_result(str(e), error=True)
            self._trace(tool.name, tool.summary(args), str(e), False)
            return f"Error: {e}"
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001
            self._drop_snapshot(snap_mark)
            self.ui.tool_result(f"{type(e).__name__}: {e}", error=True)
            self._trace(tool.name, tool.summary(args), f"{type(e).__name__}: {e}", False)
            return f"Error: {type(e).__name__}: {e}"
        if tool.kind == "edit":
            self.ctx.edited = True
            if target is not None:
                self.ctx.touched.add(str(target))
            hook_text = hooks.run("after_edit", self.ctx, file=target)
        elif tool.name == "run_tests":
            hook_text = hooks.run("after_tests", self.ctx)
        else:
            hook_text = ""
        self._trace(tool.name, tool.summary(args), result, True)
        if hook_text:
            result = f"{result}\n{hook_text}"
        self.ui.tool_result(result)
        return truncate(result, self.ctx.cfg["max_tool_output"])

    def _drop_snapshot(self, mark) -> None:
        """Правка не состоялась — снимок «до» не нужен.

        Иначе /undo тратится на пустое место, а исполнитель плана считает откатом
        неудачный вызов инструмента.
        """
        if mark is None or self.ctx.snapshots is None:
            return
        try:
            self.ctx.snapshots.rollback_to(mark)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ subagents
    def subagent_runner(self):
        def run(description: str, prompt: str, kind: str = "research") -> str:
            sub_ui = UI(quiet=False, prefix="    ")
            sub_ctx = ToolContext(cwd=self.ctx.cwd, cfg=self.ctx.cfg, ui=sub_ui,
                                  permissions=self.ctx.permissions, vars=self.ctx.vars,
                                  snapshots=self.ctx.snapshots, deadline=self.ctx.deadline)
            steps = int(self.ctx.cfg["code_subagent_max_steps"] if kind == "code"
                        else self.ctx.cfg["subagent_max_steps"])
            sub = Agent(self.llm, subagent_tools(kind), sub_ctx, sub_ui,
                        max_steps=steps, sub=True, sub_kind=kind)
            sub_ui.token = lambda t: None        # hide subagent prose, show only its tool calls
            sub_ui.think_token = lambda t: None
            answer = sub.run(prompt)
            self.usage.total_in += sub.usage.total_in
            self.usage.total_out += sub.usage.total_out
            self.ctx.edited = self.ctx.edited or sub_ctx.edited
            self.ctx.tests_run += sub_ctx.tests_run
            self.ctx.touched |= sub_ctx.touched
            self.ctx.budget_hit = self.ctx.budget_hit or sub_ctx.budget_hit
            for name, count in sub.tool_counts.items():
                self.tool_counts[name] = self.tool_counts.get(name, 0) + count
            self.tool_errors += sub.tool_errors
            for entry in sub.trace[-10:]:
                self.trace.append({**entry, "tool": "sub:" + entry["tool"]})
            if not answer.strip():
                # ask once more for a report if it ran out of steps
                sub.max_steps = 1
                sub.tools = {}
                answer = sub.run("Stop now and write your report with what you found.")
            print(dim(f"    ↳ субагент завершён ({sub.usage.requests} запросов)"))
            return answer or "Subagent returned no findings."
        return run
