#!/usr/bin/env python3
"""Ensure only one Freeze Guard dashboard runs at a time.

Same mechanism as the other tools in ~/Applications: an exclusive `flock` on
$XDG_RUNTIME_DIR/freeze-guard/instance.lock held for the lifetime of the
process, plus a Unix socket next to it through which a second start asks the
running instance to show its window.

Why a lock file and not just the D-Bus name GtkApplication can register: the lock
survives a session without a D-Bus bus, it names its owner (the pid is written
into it), and a dead process releases it automatically - so a stale lock cannot
block the next start.

Two instances would not merely be untidy here. Both would sample /proc on their
own schedule, both would register a tray icon under the same name, and both would
send notifications about the same incident.
"""

from __future__ import annotations

import fcntl
import os
import socket
import threading
from pathlib import Path

STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", STATE_DIR))
LOCK_DIR = RUNTIME_DIR / "freeze-guard"
LOCK_FILE = LOCK_DIR / "instance.lock"
SOCKET_FILE = LOCK_DIR / "control.sock"
SHOW_COMMAND = b"SHOW"


class SingleInstanceGuard:
    """Acquire and hold an exclusive lock for the lifetime of the application."""

    def __init__(self, lock_file=None):
        self.lock_file = Path(lock_file) if lock_file else LOCK_FILE
        self._lock_handle = None

    def acquire(self):
        """Try to acquire the single-instance lock.

        Returns:
            bool: True when this process is the only running instance.
        """
        try:
            self.lock_file.parent.mkdir(parents=True, exist_ok=True)
            # "a+", not "w": opening for write truncates, which would erase the
            # pid of the instance that actually holds the lock - and that pid is
            # what a blocked start tells the user about. Truncation happens after
            # the lock is ours.
            handle = open(self.lock_file, "a+", encoding="utf-8")
        except OSError:
            # No writable runtime directory: better to run than to refuse.
            return True
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            handle.close()
            return False

        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._lock_handle = handle
        return True

    def release(self):
        """Release the single-instance lock."""
        if self._lock_handle is None:
            return
        try:
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self._lock_handle.close()
        except OSError:
            pass
        self._lock_handle = None

    def owner_pid(self):
        """Pid written by whoever holds the lock, or None."""
        try:
            return int(self.lock_file.read_text().strip())
        except (OSError, ValueError):
            return None


class InstanceControlServer:
    """Unix socket server that receives commands for the running application."""

    def __init__(self, on_show, socket_file=None):
        self._on_show = on_show
        self.socket_file = Path(socket_file) if socket_file else SOCKET_FILE
        self._thread = None
        self._stop_event = threading.Event()
        self._server_socket = None

    def start(self):
        """Start listening for control commands in a background thread."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the control server and remove the socket file."""
        self._stop_event.set()
        if self._server_socket is not None:
            try:
                self._server_socket.close()
            except OSError:
                pass
            self._server_socket = None
        try:
            self.socket_file.unlink()
        except OSError:
            pass

    def _run(self):
        self.socket_file.parent.mkdir(parents=True, exist_ok=True)
        # A socket file left behind by a crashed instance would block bind();
        # the flock has already proven that nobody else owns this one.
        try:
            self.socket_file.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server_socket = server
        try:
            server.bind(str(self.socket_file))
            # Backlog 4, not 1: a user who clicks the launcher twice in a second
            # produces connections faster than this loop accepts them, and the
            # surplus would be refused.
            server.listen(4)
            server.settimeout(0.5)
        except OSError:
            server.close()
            self._server_socket = None
            return

        while not self._stop_event.is_set():
            try:
                connection, _address = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                payload = connection.recv(32)
            except OSError:
                payload = b""
            finally:
                connection.close()
            if payload.strip() == SHOW_COMMAND:
                self._on_show()

        try:
            server.close()
        except OSError:
            pass
        self._server_socket = None


def request_show_existing_instance(socket_file=None):
    """Ask the running instance to show its main window.

    Returns:
        bool: True when a running instance accepted the request.
    """
    path = Path(socket_file) if socket_file else SOCKET_FILE
    if not path.exists():
        return False
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(1.0)
        client.connect(str(path))
        client.sendall(SHOW_COMMAND)
    except OSError:
        return False
    finally:
        try:
            client.close()
        except OSError:
            pass
    return True


def enforce_single_instance(quiet=False, on_blocked=None):
    """Block startup when another instance is already running.

    Args:
        quiet: Skip focusing the running instance and the message, used for
            unattended launches such as the login autostart entry.
        on_blocked: Called with (focused_existing, owner_pid) when blocked, so
            the caller can put the message in front of the user.

    Returns:
        tuple: (may_continue, guard)
    """
    guard = SingleInstanceGuard()
    if guard.acquire():
        return True, guard
    if quiet:
        return False, guard
    focused = request_show_existing_instance()
    if on_blocked is not None:
        on_blocked(focused, guard.owner_pid())
    return False, guard
