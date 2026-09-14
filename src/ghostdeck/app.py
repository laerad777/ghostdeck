"""Host remote for the D200. A window that runs the CLI; not a player."""

from __future__ import annotations

import io
import re
import sys
import threading
from dataclasses import dataclass

from ghostdeck import cli

_SHIM_UP = re.compile(r"(?:^|\s)shim=up(?:\s|$)")


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    code: int
    stdout: str
    stderr: str

    @property
    def detail(self) -> str:
        text = (self.stderr or self.stdout).strip()
        if text:
            return text.splitlines()[-1]
        return f"exit {self.code}"


def run_cli(argv: list[str]) -> CommandResult:
    """Run `ghostdeck` in-process and capture the same stdout/stderr a terminal would show."""
    out = io.StringIO()
    err = io.StringIO()
    stdout, stderr = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = out, err
        code = cli.main(list(argv))
    except SystemExit as error:
        raw = error.code
        code = 0 if raw is None else (raw if isinstance(raw, int) else 1)
    finally:
        sys.stdout, sys.stderr = stdout, stderr
    return CommandResult(list(argv), int(code), out.getvalue(), err.getvalue())


def shim_is_up(status_stdout: str) -> bool:
    return bool(_SHIM_UP.search(status_stdout))


class DeckRemote:
    """The play button's contract: studio first if the shim is down, then play."""

    def __init__(self, run=run_cli):
        self._run = run

    def status(self) -> CommandResult:
        return self._run(["status"])

    def stop(self) -> CommandResult:
        return self._run(["stop"])

    def play(self, source: str) -> list[CommandResult]:
        source = source.strip()
        if not source:
            return [CommandResult(["play"], 2, "", "파일을 고르십시오")]
        results: list[CommandResult] = []
        st = self._run(["status"])
        results.append(st)
        if not shim_is_up(st.stdout):
            results.append(self._run(["studio"]))
            if results[-1].code != 0:
                return results
        results.append(self._run(["play", source]))
        return results


def _busy_call(remote: DeckRemote, op: str, source: str, done) -> None:
    try:
        if op == "status":
            results = [remote.status()]
        elif op == "stop":
            results = [remote.stop()]
        else:
            results = remote.play(source)
        done(results, None)
    except Exception as error:
        done([], error)


def main() -> int:
    import tkinter as tk
    from tkinter import filedialog

    remote = DeckRemote()
    root = tk.Tk()
    root.title("ghostdeck")
    root.resizable(True, False)
    root.attributes("-topmost", True)
    root.minsize(420, 160)

    status_var = tk.StringVar(value="usb=? shim=? copy=? playing=?")
    file_var = tk.StringVar()
    error_var = tk.StringVar(value="정지 후 Studio가 켜져 있으면 덱은 ADB로 남습니다.")
    busy = {"on": False}

    def set_error(text: str) -> None:
        error_var.set(text)

    def apply_results(results: list[CommandResult]) -> None:
        for item in results:
            if item.argv[:1] == ["status"] and item.stdout.strip():
                status_var.set(item.stdout.strip().splitlines()[-1])
        last = results[-1] if results else None
        if last is None:
            return
        if last.code != 0:
            set_error(last.detail)
        elif last.argv[:1] == ["stop"]:
            set_error("정지. Studio가 켜져 있으면 덱은 ADB입니다.")
        elif last.argv[:1] == ["play"]:
            set_error("재생. 루프는 정지까지 계속됩니다.")
        elif last.argv[:1] == ["studio"]:
            set_error("Studio 브리지를 시작했습니다.")

    def finish(results, error) -> None:
        busy["on"] = False
        play_btn.configure(state="normal")
        stop_btn.configure(state="normal")
        if error is not None:
            set_error(f"{type(error).__name__}: {error}")
            return
        apply_results(results)

    def kick(op: str) -> None:
        if busy["on"]:
            return
        busy["on"] = True
        play_btn.configure(state="disabled")
        stop_btn.configure(state="disabled")
        if op == "play":
            set_error("재생 준비…")
        elif op == "stop":
            set_error("정지…")
        thread = threading.Thread(
            target=_busy_call,
            args=(remote, op, file_var.get(), lambda r, e: root.after(0, finish, r, e)),
            daemon=True,
        )
        thread.start()

    def poll() -> None:
        if not busy["on"]:
            result = remote.status()
            apply_results([result])
            if result.code != 0 and result.detail:
                set_error(result.detail)
        root.after(2000, poll)

    def choose() -> None:
        path = filedialog.askopenfilename(
            title="재생할 파일",
            filetypes=[
                ("Video", "*.mp4 *.mov *.mkv *.webm *.m4v"),
                ("All", "*.*"),
            ],
        )
        if path:
            file_var.set(path)

    frame = tk.Frame(root, padx=10, pady=8)
    frame.pack(fill="both", expand=True)
    tk.Label(frame, textvariable=status_var, anchor="w", font=("Menlo", 11)).pack(fill="x")
    row = tk.Frame(frame)
    row.pack(fill="x", pady=(8, 4))
    tk.Entry(row, textvariable=file_var).pack(side="left", fill="x", expand=True)
    tk.Button(row, text="열기", command=choose).pack(side="left", padx=(6, 0))
    buttons = tk.Frame(frame)
    buttons.pack(fill="x", pady=4)
    play_btn = tk.Button(buttons, text="재생", command=lambda: kick("play"))
    stop_btn = tk.Button(buttons, text="정지", command=lambda: kick("stop"))
    play_btn.pack(side="left")
    stop_btn.pack(side="left", padx=(6, 0))
    tk.Label(frame, textvariable=error_var, anchor="w", wraplength=400, fg="#333").pack(fill="x", pady=(6, 0))

    root.after(100, poll)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
