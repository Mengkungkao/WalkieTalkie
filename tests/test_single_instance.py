"""Only one radio process at a time.

Two instances ping-pong the daemon's foreground between themselves
several times a second -- measured at 98% CPU and 24 focus grants per
second, with the screen thrashing -- and interleave bytes into the same
LoRa module.
"""

from __future__ import annotations

import multiprocessing
import os

import pytest

from app.utils.single_instance import AlreadyRunning, SingleInstance


def test_first_acquire_succeeds(tmp_path):
    with SingleInstance(tmp_path) as lock:
        assert lock.path.exists()
        assert lock.path.read_text().strip() == str(os.getpid())


def test_second_acquire_in_another_process_is_refused(tmp_path):
    """flock is per-process, so a second lock must come from a real one."""

    def child(path, queue):
        from app.utils.single_instance import AlreadyRunning, SingleInstance

        try:
            SingleInstance(path).acquire()
            queue.put("acquired")
        except AlreadyRunning as exc:
            queue.put(str(exc))

    with SingleInstance(tmp_path):
        queue = multiprocessing.Queue()
        process = multiprocessing.Process(target=child, args=(tmp_path, queue))
        process.start()
        process.join(10)
        message = queue.get(timeout=5)
        assert message.startswith("another instance is already running")
        # The holder's pid must survive the failed attempt: opening the
        # lock file for write would truncate it before flock is tried.
        assert str(os.getpid()) in message
        assert tmp_path.joinpath("walkie.lock").read_text().strip() == str(os.getpid())


def test_lock_is_reusable_after_release(tmp_path):
    SingleInstance(tmp_path).acquire().release()
    with SingleInstance(tmp_path):
        pass  # must not raise


def test_a_dead_process_leaves_no_stale_lock(tmp_path):
    """A PID file would strand the app after a crash; flock does not."""

    def child(path, ready):
        from app.utils.single_instance import SingleInstance

        SingleInstance(path).acquire()
        ready.set()
        os._exit(9)  # die hard, no cleanup

    # An Event, not a Queue: Queue.put hands off to a feeder thread, and
    # os._exit kills the process before it flushes.
    ready = multiprocessing.Event()
    process = multiprocessing.Process(target=child, args=(tmp_path, ready))
    process.start()
    assert ready.wait(10), "child never took the lock"
    process.join(10)
    assert process.exitcode == 9

    with SingleInstance(tmp_path):
        pass  # the kernel released it when the process died
