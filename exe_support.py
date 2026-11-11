"""
What the single-file executable needs at run time.

``build-exe.py`` packs the program with PyInstaller. In that file ``sys.executable``
is the program itself, not a Python interpreter, so nothing may start
``sys.executable -m ...`` or ``sys.executable -c ...``: that would start the program
again. Everything here is a no-op when the program runs from a checkout.
"""

import os
import subprocess
import sys
from typing import Dict, List, Optional

#: ``True`` inside the single-file executable that ``build-exe.py`` produces.
IS_FROZEN = bool(getattr(sys, "frozen", False))


def executable() -> str:
    """Return the absolute path of the running executable (pkexec needs an absolute path)."""
    return os.path.realpath(sys.executable)


def relaunch_command(script: str, *arguments: str) -> List[str]:
    """
    Return the command that starts this program again with the given arguments.

    In a checkout that is the interpreter and the script. In the executable it is the
    executable itself: it already contains the interpreter, and the script is not a
    file any more.

    :param script: Absolute path of the script, used outside the executable.
    :param arguments: Arguments for the new process, for example ``--helper``.
    :return: The command, without any ``pkexec`` in front.
    """
    if IS_FROZEN:
        return [executable(), *arguments]
    return [sys.executable, "-u", script, *arguments]


def child_environment(env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """
    Return the environment a child process should get.

    The executable runs with ``LD_LIBRARY_PATH`` (and, through PyInstaller's Tcl/Tk
    hook, ``TCL_LIBRARY`` and ``TK_LIBRARY``) pointing into its unpacked files.
    Inherited, they make the tools the program starts load the program's libraries
    instead of their own. Every entry that points into the unpacked files is removed,
    ``LD_LIBRARY_PATH`` gets back the value it had before the start, and the
    ``_PYI_*`` markers go, so a relaunched executable unpacks itself afresh.

    :param env: The environment to clean; defaults to this process's.
    :return: A new dict; a plain copy outside the executable.
    """
    source = os.environ if env is None else env
    bundle = getattr(sys, "_MEIPASS", "")
    if not IS_FROZEN or not bundle:
        return dict(source)
    clean: Dict[str, str] = {}
    for key, value in source.items():
        if key.startswith("_PYI_") or key == "LD_LIBRARY_PATH_ORIG":
            continue
        if bundle in value:
            kept = [part for part in value.split(os.pathsep) if part and bundle not in part]
            if not kept:
                continue
            value = os.pathsep.join(kept)
        clean[key] = value
    original = source.get("LD_LIBRARY_PATH_ORIG")
    if original:
        clean["LD_LIBRARY_PATH"] = original
    return clean


def use_system_environment_for_children() -> None:
    """
    Start every child process with :func:`child_environment`.

    One place instead of every ``subprocess`` call: a single forgotten ``env=`` would
    bring the problem back. Does nothing outside the executable.
    """
    if not IS_FROZEN or getattr(subprocess.Popen, "_clean_environment", False):
        return

    class _SystemPopen(subprocess.Popen):  # type: ignore[misc, valid-type]
        """``Popen`` that never hands the bundled libraries to a child."""

        _clean_environment = True

        def __init__(self, *args, **kwargs) -> None:
            kwargs["env"] = child_environment(kwargs.get("env"))
            super().__init__(*args, **kwargs)

    subprocess.Popen = _SystemPopen  # type: ignore[misc]
