#!/usr/bin/env python3
"""A window that shows what is being installed before the application starts.

An application that needs a system package it has not got has two ways to behave. It can die on an
ImportError somewhere deep in its startup, which tells whoever double-clicked it nothing at all;
or it can say what is missing, offer to fetch it, and show the fetching as it happens. This is the
second.

The window appears only when something is actually missing. There is nothing to watch on a machine
that already has everything, and a splash screen that flashes past on every start is worse than no
splash screen at all. `--setup` on the application forces it open, which is how you check what is
installed without waiting for something to break.

Built on tkinter, which ships with CPython, because this is the part that runs before the
dependencies do and cannot depend on any of them. Where even that is missing the same report goes
to the terminal instead, and the terminal is also where the whole thing lives when there is no
display at all.

The texts are English. It is the one language every one of these projects has in common, and it is
what the person reading an installer log will be searching the web with.
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Callable, Sequence

#: Package managers this can drive, and how each one installs without asking questions.
INSTALLERS: dict[str, list[str]] = {
    "apt-get": ["apt-get", "install", "-y"],
    "dnf": ["dnf", "install", "-y"],
    "pacman": ["pacman", "-S", "--noconfirm"],
    "zypper": ["zypper", "--non-interactive", "install"],
}


@dataclass(frozen=True)
class Need:
    """One thing the application needs, and where it comes from.

    Attributes:
        label: What to call it in the window.
        module: An importable name that proves it is there.
        command: A program on PATH that proves it is there.
        packages: What the system package manager calls it.
        pip: What pip calls it, when it is a Python package rather than a system one.
        optional: True when the application runs without it, only with less.
        note: What is lost without it, shown when it is optional and missing.
        check: A test of its own, for anything the other two cannot answer.
    """

    label: str
    module: str = ""
    command: str = ""
    packages: tuple[str, ...] = ()
    pip: tuple[str, ...] = ()
    optional: bool = False
    note: str = ""
    check: Callable[[], bool] | None = None

    def satisfied(self) -> bool:
        """Whether it is there right now.

        A check of its own wins where there is one. Some things cannot be tested by importing them
        in this process — tkinter is the example, because a broken install takes the process with
        it rather than raising — and those bring their own test.

        Returns:
            bool: True when everything named here is present.
        """

        if self.check is not None:
            return bool(self.check())

        if self.command and shutil.which(self.command) is None:
            return False

        if self.module:
            try:
                importlib.import_module(self.module)
            except Exception:
                return False

        return True


@dataclass
class Progress:
    """What the window shows, written by the worker and read by the window."""

    needs: Sequence[Need]
    states: dict[str, str] = field(default_factory=dict)
    details: dict[str, str] = field(default_factory=dict)
    lines: list[str] = field(default_factory=list)
    finished: bool = False
    failed: str = ""

    def set(self, need: Need, state: str, detail: str = "") -> None:
        """Records where one need has got to.

        Args:
            need: Which one.
            state: pending, checking, installing, ready, missing or failed.
            detail: A line under the label.
        """

        self.states[need.label] = state
        self.details[need.label] = detail

    def write(self, line: str) -> None:
        """Adds a line of command output.

        Args:
            line: One line, as the command printed it.
        """

        text = line.rstrip()

        if text:
            self.lines.append(text[:300])
            del self.lines[:-200]


def ensure(app: str, needs: Sequence[Need], *, force: bool = False) -> bool:
    """Makes sure everything is there, showing the work when there is work to show.

    Args:
        app: The application's name, for the window title.
        needs: What it needs.
        force: True to open the window even when nothing is missing.

    Returns:
        bool: True when the application may start. False when something it cannot do without is
        still missing, in which case the reason has already been shown.
    """

    missing = [need for need in needs if not need.satisfied()]

    if not missing and not force:
        return True

    progress = Progress(needs=needs)

    for need in needs:
        progress.set(need, "pending")

    window = _Window(app, progress) if _can_show() else None
    outcome: dict[str, bool] = {}

    def work() -> None:
        """Does the installing, off the window's thread."""
        outcome["ok"] = _install(progress, needs)
        progress.finished = True

    if window is None:
        work()
        _report(progress)

        return outcome.get("ok", False)

    threading.Thread(target=work, daemon=True).start()
    window.loop()

    return outcome.get("ok", False)


def _install(progress: Progress, needs: Sequence[Need]) -> bool:
    """Installs whatever is missing, one need at a time.

    Args:
        progress: Where to report.
        needs: What the application needs.

    Returns:
        bool: True when everything the application cannot do without is there.
    """

    ok = True

    for need in needs:
        progress.set(need, "checking")

        if need.satisfied():
            progress.set(need, "ready", "already installed")
            continue

        if not need.packages and not need.pip:
            # Nothing to install it with — it has to be on the machine already. That is fine for
            # something the application can do without, and the end of the road for anything else.
            if need.optional:
                progress.set(need, "missing", need.note or "not installed")

                continue

            progress.set(need, "failed", need.note or "not installed, and nothing here can fetch it")
            progress.failed = f"{need.label} is missing."
            ok = False

            continue

        progress.set(need, "installing")
        installed = _fetch(progress, need)

        if installed and need.satisfied():
            progress.set(need, "ready", "installed")
            continue

        if need.optional:
            progress.set(need, "missing", need.note or "optional, skipped")
            continue

        progress.set(need, "failed", "could not be installed")
        progress.failed = f"{need.label} is missing and could not be installed."
        ok = False

    return ok


def _fetch(progress: Progress, need: Need) -> bool:
    """Runs the command that installs one need.

    Args:
        progress: Where the output goes.
        need: What to install.

    Returns:
        bool: True when the command succeeded.
    """

    if need.pip:
        return _run(progress, [sys.executable, "-m", "pip", "install", "--user", *need.pip])

    command = _system_install(need.packages)

    if command is None:
        progress.write("No package manager this installer knows how to drive.")

        return False

    return _run(progress, command)


def _system_install(packages: Sequence[str]) -> list[str] | None:
    """The command that installs system packages, asking for rights if it has to.

    pkexec rather than sudo: there is no terminal to type a password into, and pkexec is what puts
    the desktop's own authentication dialog on screen.

    Args:
        packages: What to install.

    Returns:
        list[str] | None: The command, or None when no known manager is installed.
    """

    for name, base in INSTALLERS.items():
        if shutil.which(name) is None:
            continue

        command = [*base, *packages]

        if os.geteuid() == 0:
            return command

        if shutil.which("pkexec") is not None:
            return ["pkexec", *command]

        return ["sudo", *command]

    return None


def _run(progress: Progress, command: Sequence[str]) -> bool:
    """Runs one command and streams what it says.

    Args:
        progress: Where the output goes.
        command: What to run.

    Returns:
        bool: True when it exited cleanly.
    """

    progress.write("$ " + " ".join(command))

    try:
        process = subprocess.Popen(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
        )
    except OSError as error:
        progress.write(str(error))

        return False

    assert process.stdout is not None

    for line in process.stdout:
        progress.write(line)

    return process.wait() == 0


def _can_show() -> bool:
    """Whether a window can be put on screen at all.

    Returns:
        bool: True when there is a display and tkinter to draw with.
    """

    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return False

    try:
        importlib.import_module("tkinter")
    except Exception:
        return False

    return True


def _report(progress: Progress) -> None:
    """Prints the outcome, for a run with no window.

    Args:
        progress: What happened.
    """

    for need in progress.needs:
        state = progress.states.get(need.label, "pending")
        detail = progress.details.get(need.label, "")
        mark = {"ready": "✓", "missing": "!", "failed": "✗"}.get(state, "·")
        print(f"  {mark} {need.label}" + (f" — {detail}" if detail else ""), flush=True)

    if progress.failed:
        print(f"\n  {progress.failed}\n", flush=True)


class _Window:
    """The window itself: one row per need, and the output of the running command underneath."""

    #: Redrawn this often. Fast enough to look live, slow enough to leave the worker alone.
    TICK_MS = 200

    def __init__(self, app: str, progress: Progress) -> None:
        """
        Args:
            app: The application's name.
            progress: What to show.
        """

        import tkinter as tk
        from tkinter import scrolledtext

        self.tk = tk
        self.progress = progress
        self.rows: dict[str, object] = {}

        self.root = tk.Tk()
        self.root.title(f"{app} — setting up")
        self.root.configure(bg="#f7f7f8")
        self.root.minsize(560, 380)

        header = tk.Frame(self.root, bg="#f7f7f8")
        header.pack(fill="x", padx=18, pady=(16, 4))
        tk.Label(
            header, text=app, bg="#f7f7f8", fg="#2b2b2b",
            font=("DejaVu Sans", 14, "bold"), anchor="w",
        ).pack(fill="x")
        tk.Label(
            header, text="Installing what is missing …", bg="#f7f7f8", fg="#6b6b6b",
            font=("DejaVu Sans", 9), anchor="w",
        ).pack(fill="x")

        self.list = tk.Frame(self.root, bg="#f7f7f8")
        self.list.pack(fill="x", padx=18, pady=(10, 6))

        for need in progress.needs:
            row = tk.Frame(self.list, bg="#f7f7f8")
            row.pack(fill="x", pady=1)
            mark = tk.Label(row, text="·", bg="#f7f7f8", fg="#9a9aa2",
                            font=("DejaVu Sans", 10), width=2)
            mark.pack(side="left")
            text = tk.Label(row, text=need.label, bg="#f7f7f8", fg="#2b2b2b",
                            font=("DejaVu Sans", 10), anchor="w")
            text.pack(side="left")
            detail = tk.Label(row, text="", bg="#f7f7f8", fg="#6b6b6b",
                              font=("DejaVu Sans", 9), anchor="w")
            detail.pack(side="left", padx=(8, 0))
            self.rows[need.label] = (mark, detail)

        self.log = scrolledtext.ScrolledText(
            self.root, height=10, bg="#f1f1f3", fg="#4a4a4a", relief="flat",
            font=("DejaVu Sans Mono", 8), wrap="word",
        )
        self.log.pack(fill="both", expand=True, padx=18, pady=(4, 8))
        self.log.configure(state="disabled")

        self.foot = tk.Label(
            self.root, text="", bg="#f7f7f8", fg="#6b6b6b",
            font=("DejaVu Sans", 9), anchor="w",
        )
        self.foot.pack(fill="x", padx=18, pady=(0, 12))

        self.shown = 0

    def loop(self) -> None:
        """Shows the window and keeps it up to date until the work is finished."""
        self.root.after(self.TICK_MS, self._tick)
        self.root.mainloop()

    def _tick(self) -> None:
        """Redraws once, and closes when there is nothing left to wait for."""
        marks = {"ready": ("✓", "#1a7f47"), "installing": ("→", "#d20000"),
                 "checking": ("→", "#6b6b6b"), "missing": ("!", "#b06000"),
                 "failed": ("✗", "#d20000"), "pending": ("·", "#9a9aa2")}

        for need in self.progress.needs:
            state = self.progress.states.get(need.label, "pending")
            symbol, colour = marks.get(state, ("·", "#9a9aa2"))
            mark, detail = self.rows[need.label]  # type: ignore[misc]
            mark.configure(text=symbol, fg=colour)
            detail.configure(text=self.progress.details.get(need.label, ""))

        if len(self.progress.lines) != self.shown:
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            self.log.insert("end", "\n".join(self.progress.lines[-200:]))
            self.log.see("end")
            self.log.configure(state="disabled")
            self.shown = len(self.progress.lines)

        if self.progress.finished:
            if self.progress.failed:
                # Left on screen with the reason on it: this window is the only place a failure
                # can be read when the application was started from a menu.
                self.foot.configure(text=self.progress.failed + "  Close this window to give up.",
                                    fg="#d20000")

                return

            self.root.destroy()

            return

        self.root.after(self.TICK_MS, self._tick)
