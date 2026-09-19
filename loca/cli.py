"""Command-line entry point: REPL, slash commands, headless mode."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__, hooks
from .agent import Agent
from .config import HOME_DIR, ensure_home, load_config
from .llm import LLMError, OllamaClient
from .mcp import MCPManager
from .memory import INIT_PROMPT, add_memory, find_memory_files
from .permissions import MODES, Permissions
from .session import Session, list_sessions
from .snapshots import Snapshots
from .tools import ToolContext, ToolError, main_tools
from .tools.tests import RunTests, detect_test_command
from .ui import UI, bold, cyan, dim, green, yellow

try:
    import readline  # noqa: F401  (line editing + history)
except ImportError:  # pragma: no cover
    readline = None

HELP = """\
Команды:
  /help                 эта справка
  /clear                новый диалог
  /compact              сжать контекст сейчас
  /context              сколько контекста занято
  /model [имя]          показать / сменить модель (список — без аргумента)
  /mode [режим]         default | accept-edits | plan | yolo
  /think                включить / выключить режим размышлений
  /plan-show            текущий граф подзадач (то же — /todos)
  /undo                 откатить последнюю правку файла
  /hooks                какие хуки настроены
  /plan                 перейти в режим плана (только чтение)
  /test [цель]          прогнать тесты проекта
  /vars [clear]         переменные агента
  /init                 изучить проект и создать LOCA.md
  /memory               какие файлы памяти загружены
  /sessions             последние сессии в этой папке
  /resume <id>          продолжить сессию
  /mcp                  статус MCP-серверов
  /exit                 выход
  #текст                добавить заметку в ./LOCA.md
  !команда              выполнить shell-команду без модели
Многострочный ввод: начните и закончите строкой \"\"\".  Ctrl+C прерывает ответ, Ctrl+D — выход."""


def load_custom_commands(cwd: Path) -> dict[str, Path]:
    cmds: dict[str, Path] = {}
    for folder in (HOME_DIR / "commands", cwd / ".loca" / "commands"):
        if folder.is_dir():
            for p in folder.glob("*.md"):
                cmds[p.stem] = p
    return cmds


def read_input(prompt: str) -> str:
    line = input(prompt)
    if line.strip() != '"""':
        return line
    lines = []
    while True:
        nxt = input(dim("… "))
        if nxt.strip() == '"""':
            return "\n".join(lines)
        lines.append(nxt)


class App:
    def __init__(self, args: argparse.Namespace):
        self.cwd = Path(args.cwd or os.getcwd()).resolve()
        ensure_home()
        overrides = {"model": args.model, "num_ctx": args.ctx, "host": args.host,
                     "max_seconds": getattr(args, "max_seconds", None)}
        if args.yolo:
            overrides["permission_mode"] = "yolo"
        elif args.mode:
            overrides["permission_mode"] = args.mode
        if args.think:
            overrides["think"] = True
        self.cfg = load_config(self.cwd, overrides)
        self.headless = bool(args.print)
        self.ui = UI()
        self.llm = OllamaClient(self.cfg)
        self.permissions = Permissions(self.cfg, interactive=not self.headless)
        self.ctx = ToolContext(cwd=self.cwd, cfg=self.cfg, ui=self.ui, permissions=self.permissions)
        self.mcp = MCPManager(self.cfg.get("mcp_servers", {}), self.ui) if self.cfg.get("mcp_servers") else None
        tools = main_tools() + (self.mcp.tools() if self.mcp else [])
        self.agent = Agent(self.llm, tools, self.ctx, self.ui, max_steps=self.cfg["max_steps"])
        self.ctx.spawn_subagent = self.agent.subagent_runner()
        self.ctx.refresh_system = self.agent.refresh_system
        self.session = Session(self.cwd)
        self.bind_snapshots()
        self.commands = load_custom_commands(self.cwd)

        resume_id = args.resume
        if args.continue_:
            recent = list_sessions(self.cwd, limit=1)
            resume_id = recent[0]["id"] if recent else None
            if not resume_id:
                self.ui.warn("В этой папке нет сохранённых сессий.")
        if resume_id:
            self.resume(resume_id)

    # ------------------------------------------------------------------
    def bind_snapshots(self) -> None:
        self.ctx.snapshots = Snapshots(HOME_DIR / "snapshots" / self.session.id,
                                      keep=self.cfg.get("snapshots_keep", 200))

    def undo(self) -> None:
        snaps = self.ctx.snapshots
        try:
            path, what = snaps.undo()
        except LookupError as e:
            self.ui.warn(f"Нечего откатывать ({e}).")
            return
        except OSError as e:
            self.ui.error(f"Не удалось откатить: {e}")
            return
        if str(path) in self.ctx.read_files:
            if path.is_file():
                self.ctx.read_files[str(path)] = path.stat().st_mtime
            else:
                self.ctx.read_files.pop(str(path), None)
        self.ui.info(f"↩ {what}")
        # модель должна знать, что её правки больше нет
        self.agent.messages.append({"role": "user", "content": f"[Я откатил правку: {what}]"})
        self.agent.messages.append({"role": "assistant", "content": "Понял."})

    def write_stats(self, path: str) -> None:
        u = self.agent.usage
        data = {"model": self.llm.model, "num_ctx": self.llm.num_ctx,
                "requests": u.requests, "steps": u.steps,
                "tokens_in": u.total_in, "tokens_out": u.total_out,
                "model_seconds": round(u.seconds, 2),
                "context_tokens": self.agent.context_tokens(),
                "tools": dict(sorted(self.agent.tool_counts.items())),
                "tool_errors": self.agent.tool_errors,
                "trace": list(self.agent.trace),
                "edited": self.ctx.edited, "tests_run": self.ctx.tests_run,
                "tests_passed": self.ctx.tests_passed,
                "touched": sorted(self.ctx.rel(Path(f)) for f in self.ctx.touched),
                "budget_hit": self.ctx.budget_hit,
                "plan": self.ctx.plan.to_list(), "plan_stats": dict(self.ctx.plan_stats),
                "vars": self.ctx.vars.names()}
        try:
            Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            self.ui.warn(f"Статистика не записана: {e}")

    def resume(self, session_id: str) -> None:
        try:
            self.session, messages, plan, variables = Session.load(session_id)
        except (OSError, ValueError, KeyError) as e:
            self.ui.error(f"Не удалось загрузить сессию {session_id}: {e}")
            return
        self.agent.reset()
        self.agent.messages += messages
        self.ctx.plan.load(plan)
        self.ctx.vars.update(variables)
        self.bind_snapshots()
        self.agent.refresh_system()
        turns = sum(1 for m in messages if m["role"] == "user")
        self.ui.info(f"↺ сессия {session_id}: {self.session.title} ({turns} сообщений)")

    def save(self) -> None:
        try:
            self.session.save(self.agent.messages, self.ctx.plan.to_list(),
                              self.ctx.vars.to_dict())
        except OSError as e:
            self.ui.warn(f"Сессия не сохранена: {e}")

    def banner(self) -> None:
        mem = find_memory_files(self.cwd, self.cfg["memory_files"])
        print(f"{bold(cyan('loca'))} {dim('v' + __version__)} · модель {bold(self.llm.model)} · "
              f"ctx {self.llm.num_ctx} · режим {bold(self.permissions.mode)}")
        print(dim(f"{self.cwd}" + (f" · память: {', '.join(p.name for p in mem)}" if mem else "")))
        found = detect_test_command(self.cwd)
        if found:
            print(dim(f"тесты: {found[0]}"))
        active = [e for e in hooks.EVENTS if hooks.commands(self.cfg, e)]
        if active:
            print(dim("хуки: " + ", ".join(active)))
        print(dim("/help — команды, #заметка — в память, Ctrl+D — выход"))
        try:
            models = self.llm.list_models()
            if not any(m == self.llm.model or m.split(":")[0] == self.llm.model for m in models):
                self.ui.warn(f"Модель {self.llm.model} не скачана. Выполните: ollama pull {self.llm.model}")
        except LLMError as e:
            self.ui.error(str(e))

    def prompt_str(self) -> str:
        mode = self.permissions.mode
        tag = "" if mode == "default" else f"[{mode}] "
        used = self.agent.context_tokens()
        pct = int(100 * used / self.llm.num_ctx)
        ctx_tag = dim(f"{pct}% ") if pct >= 30 else ""
        return f"\n{ctx_tag}{yellow(tag)}{bold('›')} "

    # ------------------------------------------------------------------
    def handle_slash(self, line: str) -> str | None:
        """Returns text to send to the model, or None if handled locally."""
        name, _, rest = line[1:].partition(" ")
        rest = rest.strip()
        if name in ("exit", "quit", "q"):
            raise EOFError
        if name == "help":
            print(HELP)
            if self.commands:
                print("Свои команды: " + ", ".join("/" + c for c in sorted(self.commands)))
        elif name == "clear":
            self.save()
            self.agent.reset()
            self.session = Session(self.cwd)
            self.bind_snapshots()
            self.ui.info("Новый диалог.")
        elif name == "compact":
            self.agent.compact()
        elif name == "context":
            u = self.agent.usage
            print(f"контекст: ~{self.agent.context_tokens()} / {self.llm.num_ctx} токенов · "
                  f"сообщений: {len(self.agent.messages)} · всего вход/выход: {u.total_in}/{u.total_out} · "
                  f"запросов: {u.requests} · время модели: {u.seconds:.0f}s")
        elif name == "model":
            if rest:
                self.llm.model = rest
                self.cfg["model"] = rest
                self.ui.info(f"Модель: {rest}")
            else:
                try:
                    for m in self.llm.list_models():
                        print(("● " if m == self.llm.model else "  ") + m)
                except LLMError as e:
                    self.ui.error(str(e))
        elif name == "mode":
            if rest in MODES:
                self.permissions.mode = rest
                self.agent.refresh_system()
                self.ui.info(f"Режим: {rest}")
            else:
                print(f"Текущий режим: {self.permissions.mode}. Доступны: {', '.join(MODES)}")
        elif name == "think":
            self.llm.think = not self.llm.think
            self.ui.info(f"Размышления: {'вкл' if self.llm.think else 'выкл'}")
        elif name in ("todos", "plan-show", "tasks"):
            print(self.ctx.plan.render())
        elif name == "undo":
            self.undo()
        elif name == "hooks":
            configured = {e: hooks.commands(self.cfg, e) for e in hooks.EVENTS}
            if not any(configured.values()):
                print("Хуков нет. Пример для .loca/config.json:\n"
                      '  "hooks": {"after_edit": ["ruff format {file}"], "stop": ["pytest -q"]}')
            else:
                for event, cmds in configured.items():
                    for cmd in cmds:
                        print(f"{green(event)}: {cmd}")
        elif name == "plan":
            self.permissions.mode = "plan"
            self.agent.refresh_system()
            self.ui.info("Режим: plan (правки запрещены, модель предложит план)")
        elif name == "test":
            self.run_tests(rest)
        elif name == "vars":
            if rest in ("clear", "reset"):
                self.ctx.vars.clear()
                self.agent.refresh_system()
                self.ui.info("Переменные очищены.")
            elif rest:
                value = self.ctx.vars.get(rest)
                print(value if value is not None else f"нет переменной {rest}")
            else:
                print(self.ctx.vars.summary() or "(переменных нет)")
        elif name == "init":
            return INIT_PROMPT
        elif name == "memory":
            files = find_memory_files(self.cwd, self.cfg["memory_files"])
            print("\n".join(str(p) for p in files) or "Файлов памяти нет. /init создаст LOCA.md")
        elif name == "sessions":
            for s in list_sessions(self.cwd):
                turns = dim("(" + str(s["turns"]) + ")")
                print(f"{green(s['id'])}  {dim(s['updated'])}  {s['title']} {turns}")
        elif name == "resume":
            if rest:
                self.save()
                self.resume(rest)
            else:
                print("Использование: /resume <id> (см. /sessions)")
        elif name == "mcp":
            if not self.mcp:
                print("MCP-серверы не настроены (mcp_servers в ~/.loca/config.json)")
            else:
                for n, s in self.mcp.servers.items():
                    print(f"{green('●')} {n}: {', '.join(t['name'] for t in s.tools)}")
                for n, err in self.mcp.errors.items():
                    print(f"{yellow('●')} {n}: {err}")
        elif name in self.commands:
            template = self.commands[name].read_text(encoding="utf-8")
            return template.replace("$ARGUMENTS", rest)
        else:
            self.ui.warn(f"Неизвестная команда /{name}. /help — список.")
        return None

    def run_tests(self, target: str = "") -> None:
        args = {"target": target} if target else {}
        tool = RunTests()
        try:
            out = tool.run(args, self.ctx)
        except ToolError as e:
            self.ui.error(str(e))
            return
        print(out)
        self.agent.messages.append({"role": "user", "content":
                                    f"[I ran the tests myself]\n{out[-3000:]}"})
        self.agent.messages.append({"role": "assistant", "content": "Noted."})

    def run_shell(self, command: str) -> None:
        import subprocess
        r = subprocess.run(command, shell=True, cwd=self.ctx.cwd, capture_output=True, text=True)
        out = (r.stdout + r.stderr).rstrip()
        print(out)
        # let the model see what the user ran
        self.agent.messages.append({"role": "user", "content":
                                    f"[I ran `{command}` myself]\n{out[-3000:]}"})
        self.agent.messages.append({"role": "assistant", "content": "Noted."})

    def ask(self, text: str) -> str:
        answer = self.agent.run(text)
        self.save()
        return answer

    def repl(self) -> None:
        self.banner()
        hist = HOME_DIR / "history"
        if readline:
            try:
                readline.read_history_file(hist)
            except OSError:
                pass
        try:
            while True:
                try:
                    line = read_input(self.prompt_str()).strip()
                except KeyboardInterrupt:
                    print()
                    continue
                if not line:
                    continue
                if line.startswith("#"):
                    path = add_memory(self.cwd, line[1:])
                    self.agent.refresh_system()
                    self.ui.info(f"Записано в {path.name}")
                    continue
                if line.startswith("!"):
                    self.run_shell(line[1:])
                    continue
                if line.startswith("/"):
                    line = self.handle_slash(line)
                    if line is None:
                        continue
                self.ask(line)
        except EOFError:
            print()
        finally:
            self.save()
            if readline:
                try:
                    readline.write_history_file(hist)
                except OSError:
                    pass
            if self.mcp:
                self.mcp.stop()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="loca", description="Локальный агент для кода поверх Ollama")
    p.add_argument("prompt", nargs="*", help="начальный запрос")
    p.add_argument("-p", "--print", action="store_true", help="выполнить запрос и выйти (без диалога)")
    p.add_argument("-m", "--model")
    p.add_argument("--ctx", type=int, help="размер контекста (num_ctx)")
    p.add_argument("--host", help="адрес Ollama")
    p.add_argument("--mode", choices=MODES)
    p.add_argument("--yolo", action="store_true", help="не спрашивать разрешений (кроме опасных команд)")
    p.add_argument("--think", action="store_true", help="включить размышления модели")
    p.add_argument("-c", "--continue", dest="continue_", action="store_true", help="продолжить последнюю сессию")
    p.add_argument("-r", "--resume", metavar="ID")
    p.add_argument("--cwd")
    p.add_argument("--stats", metavar="FILE", help="записать метрики прогона в JSON (для бенчмарка)")
    p.add_argument("--max-seconds", type=int, dest="max_seconds",
                   help="лимит времени на один ход: агент остановится сам, а не будет убит")
    p.add_argument("-V", "--version", action="version", version=f"loca {__version__}")
    args = p.parse_args(argv)

    prompt = " ".join(args.prompt).strip()
    if not sys.stdin.isatty() and args.print:
        try:
            piped = sys.stdin.read().strip()
        except (OSError, ValueError):
            piped = ""
        if piped:
            prompt = f"{prompt}\n\n{piped}" if prompt else piped

    app = App(args)
    if args.print:
        if not prompt:
            p.error("для -p нужен запрос")
        app.ui.quiet = False
        if args.stats:
            app.agent.on_step = lambda: app.write_stats(args.stats)
        try:
            app.ask(prompt)
        finally:
            if args.stats:
                app.write_stats(args.stats)
            if app.mcp:
                app.mcp.stop()
        return
    if prompt:
        app.banner()
        app.ask(prompt)
        app.banner = lambda: None  # type: ignore[method-assign]
    app.repl()


if __name__ == "__main__":
    main()
