"""
Which state of Freeze Guard is running, derived from the commit history.

Nobody raises the number by hand, so it cannot be forgotten and cannot lie. It
works the same way as in the other projects (Mint Cleaner, Startup Launcher):

    major  raised by hand, and only for a release that genuinely justifies it
    minor  number of commits that added a new Python module, which is where a new
           capability lands (``__init__.py``, tests and the build files aside)
    patch  commits since that last happened
    build  total number of commits

So "0.2.5 (14)" means: the 14th commit, the second time a module arrived, five
changes since then. The initial import counts as the first arrival, so the first
working state is 0.1.x. The tag of a release is ``v<version>-build<build>``.

One source, three routes to it, tried in this order:

    FREEZE_GUARD_VERSION   a fixed value, for tests and for a one-off run
    git history     a checkout, which is every development machine
    VERSION file    an installation without .git, and the single-file
                    executable, which carries it

The VERSION file is written by ``build-exe.py`` (or ``python3 version.py --write``)
and is not checked in: it is derived, and a checked-in copy would be stale one
commit later. The executable never reads git: its copy lives in the folder it
unpacks itself into.

There is deliberately no invented fallback such as 0.0.0: an unknown version says
so. This module imports nothing from Freeze Guard, so the build and the release
workflow can run it on its own.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

#: ``True`` inside the single-file executable that ``build-exe.py`` produces.
FROZEN = bool(getattr(sys, "frozen", False))

# The executable unpacks the VERSION file next to its other data.
ROOT = Path(getattr(sys, "_MEIPASS", ".")) if FROZEN else Path(__file__).resolve().parent
VERSION_FILE = ROOT / "VERSION"
ENVIRONMENT_VARIABLE = "FREEZE_GUARD_VERSION"

UNKNOWN = "unknown"
GIT_TIMEOUT = 10

# Raised by hand, and only for a release that genuinely justifies it.
MAJOR = 0

# A new module, in the sense of "a new capability arrived". Package markers, tests
# and the files that only build or describe the program are not capabilities.
MODULE_PATHSPECS = (
    "*.py",
    ":(exclude)tests/*",
    ":(exclude)__init__.py",
    ":(exclude)*/__init__.py",
    ":(exclude)build-exe.py",
    ":(exclude)exe_support.py",
    ":(exclude)version.py",
)


@dataclass(frozen=True)
class Version:
    """
    The version in its parts.

    Attributes:
        name: The number, for example ``0.2.5``, or ``unknown``.
        build: Total number of commits, empty when not known.
        commit: Short hash of the newest commit, empty when not known.
        date: Day of the newest commit as ``YYYY-MM-DD``, empty when not known.
    """

    name: str
    build: str = ""
    commit: str = ""
    date: str = ""

    @property
    def is_known(self) -> bool:
        """Report whether any source could say which version this is."""
        return self.name != UNKNOWN

    @property
    def label(self) -> str:
        """Return the full form, for example ``0.2.5 (14), 4957c55, 2026-11-11``."""
        if not self.is_known:
            return UNKNOWN
        parts = [f"{self.name} ({self.build})" if self.build else self.name]
        parts.extend(part for part in (self.commit, self.date) if part)
        return ", ".join(parts)

    def line(self) -> str:
        """Return the form stored in the VERSION file: four fields separated by spaces."""
        return " ".join((self.name, self.build, self.commit, self.date)).strip()


def parse(line: str) -> Optional[Version]:
    """Read a stored version back; None for an empty line."""
    parts = line.split()
    if not parts:
        return None
    parts += [""] * (4 - len(parts))
    return Version(parts[0], parts[1], parts[2], parts[3])


def current(root: Path = ROOT) -> Version:
    """Return the running version, or the honest unknown."""
    fixed = parse(os.environ.get(ENVIRONMENT_VARIABLE, ""))
    if fixed is not None:
        return fixed
    stored = from_file(root)
    if FROZEN or not (root / ".git").exists():
        return stored or Version(UNKNOWN)
    return from_git(root) or stored or Version(UNKNOWN)


def banner(app: str) -> str:
    """Return the line ``--version`` prints, for example ``Freeze Guard 0.2.5 (14), 4957c55, 2026-11-11``."""
    return f"{app} {current().label}"


def from_file(root: Path) -> Optional[Version]:
    """Read the VERSION file; None when there is no usable file."""
    try:
        return parse((root / VERSION_FILE.name).read_text(encoding="utf-8"))
    except OSError:
        return None


def from_git(root: Path) -> Optional[Version]:
    """Derive the version from the commit history; None without a readable history."""
    history = _git(root, "log", "--reverse", "--format=%H").split()
    if not history:
        return None

    # Renames are followed, so a module that only moved does not count as new.
    arrivals = set(
        _git(root, "-c", "diff.renames=true", "log", "--diff-filter=A", "--format=%H",
             "--", *MODULE_PATHSPECS).split()
    )
    minor = patch = 0
    for commit in history:
        if commit in arrivals:
            minor += 1
            patch = 0
        else:
            patch += 1
    name = f"{MAJOR}.{minor}.{patch}"

    head = _git(root, "log", "-1", "--format=%h %cs").split()
    return Version(
        name=name,
        build=str(len(history)),
        commit=head[0] if head else "",
        date=head[1] if len(head) > 1 else "",
    )


def write_file(root: Path = ROOT) -> Optional[Version]:
    """Write the VERSION file from the history; None when there is no history."""
    found = from_git(root)
    if found is None:
        return None
    try:
        (root / VERSION_FILE.name).write_text(found.line() + "\n", encoding="utf-8")
    except OSError:
        pass
    return found


def _git(root: Path, *args: str) -> str:
    """Run one fixed, read-only git command in Freeze Guard's own folder; empty on any failure."""
    executable = shutil.which("git")
    if executable is None:
        return ""
    environment = dict(os.environ)
    environment["LC_ALL"] = "C"
    try:
        done = subprocess.run(
            [executable, "-C", str(root), *args],
            capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False, env=environment,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout if done.returncode == 0 else ""


if __name__ == "__main__":
    # Lets the build, the release workflow and anybody curious ask without starting the app.
    if len(sys.argv) > 1 and sys.argv[1] == "--write":
        written = write_file()
        print(written.label if written else UNKNOWN)
    else:
        print(current().label)
