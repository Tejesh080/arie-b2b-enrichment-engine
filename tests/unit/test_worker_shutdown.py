"""Graceful shutdown wiring for the worker loop — no database, no network.

Exercises `install_graceful_shutdown` by invoking the handler it registers
directly, rather than raising a real OS signal: `os.kill(pid, SIGTERM)`
doesn't reach a Python signal handler the same way on every platform (Windows
routes it to TerminateProcess instead), and going through the OS would make
this test's behaviour depend on which platform it happens to run on, not on
the code under test. Signal registration and restoration are still exercised
for real via `signal.signal`/`signal.getsignal`.
"""

from __future__ import annotations

import signal
import threading

from arie.jobs.worker import install_graceful_shutdown


def test_install_graceful_shutdown_registers_sigterm_and_sigint() -> None:
    original_term = signal.getsignal(signal.SIGTERM)
    original_int = signal.getsignal(signal.SIGINT)
    try:
        stop = threading.Event()
        install_graceful_shutdown(stop)

        assert signal.getsignal(signal.SIGTERM) is not original_term
        assert signal.getsignal(signal.SIGINT) is not original_int
    finally:
        signal.signal(signal.SIGTERM, original_term)
        signal.signal(signal.SIGINT, original_int)


def test_the_registered_handler_sets_the_stop_event_for_both_signals() -> None:
    original_term = signal.getsignal(signal.SIGTERM)
    original_int = signal.getsignal(signal.SIGINT)
    try:
        stop = threading.Event()
        install_graceful_shutdown(stop)

        term_handler = signal.getsignal(signal.SIGTERM)
        assert callable(term_handler)
        term_handler(signal.SIGTERM, None)
        assert stop.is_set()

        stop.clear()
        int_handler = signal.getsignal(signal.SIGINT)
        assert callable(int_handler)
        int_handler(signal.SIGINT, None)
        assert stop.is_set()
    finally:
        signal.signal(signal.SIGTERM, original_term)
        signal.signal(signal.SIGINT, original_int)
