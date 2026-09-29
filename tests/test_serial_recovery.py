"""A hung-up serial port, and finding out who is sharing it.

On the Orange Pi a login shell on the LoRa port hung it up every time
the shell restarted; every write after that failed with "[Errno 5]
Input/output error" and the radio stayed dead until the app was
restarted. The driver now reopens the port, and the app says plainly
when something else is on it.
"""

from __future__ import annotations

import errno
import subprocess
import sys

import pytest

from app.radio import sx126x
from app.radio.sx126x import SX126x, port_conflicts
from tests.fakes import FakeModule


class HungUp(FakeModule):
    """A port that fails once, the way a hung-up tty does."""

    def __init__(self, fail_write=False, fail_read=False):
        super().__init__(addr=1)
        self.fail_write, self.fail_read = fail_write, fail_read

    def write(self, data):
        if self.fail_write:
            raise OSError(errno.EIO, "Input/output error")
        return super().write(data)

    def read(self, size=1):
        if self.fail_read:
            raise OSError(errno.EIO, "Input/output error")
        return super().read(size)


@pytest.fixture
def ports(monkeypatch):
    """serial.Serial hands out these in turn, as the driver (re)opens."""
    queue = []
    monkeypatch.setattr("serial.Serial", lambda *a, **k: queue.pop(0))
    return queue


def test_a_write_to_a_hung_up_port_reopens_it_and_goes_through(ports):
    fresh = FakeModule(addr=1)
    ports.extend([HungUp(fail_write=True), fresh])
    radio = SX126x(port="fake", addr=1, freq_mhz=868)
    radio.send(2, b"hello")
    assert radio.ser is fresh
    assert fresh.written.endswith(b"hello")
    assert radio.reopens == 1


def test_a_failed_read_reopens_the_port(ports):
    fresh = FakeModule(addr=1)
    ports.extend([HungUp(fail_read=True), fresh])
    radio = SX126x(port="fake", addr=1, freq_mhz=868)
    assert radio.read_blocking() == b""
    assert radio.ser is fresh


def test_a_port_that_keeps_failing_is_not_hammered(ports):
    ports.extend([HungUp(fail_write=True), HungUp(fail_write=True)])
    radio = SX126x(port="fake", addr=1, freq_mhz=868)
    with pytest.raises(OSError):
        radio.send(2, b"x")            # reopened, and the new one fails too
    with pytest.raises(OSError):
        radio.send(2, b"x")            # too soon to reopen again
    assert radio.reopens == 1


def test_closing_is_not_mistaken_for_a_failure(ports):
    ports.append(HungUp(fail_read=True))
    radio = SX126x(port="fake", addr=1, freq_mhz=868)
    radio.close()
    assert radio.read_blocking() == b""
    assert radio.reopens == 0


# --- who else is on the port -------------------------------------------------
def test_a_kernel_console_on_the_port_is_named(tmp_path, monkeypatch):
    cmdline = tmp_path / "cmdline"
    cmdline.write_text("root=/dev/mmcblk0p1 console=ttyS0,115200 console=tty1\n")
    monkeypatch.setattr(sx126x, "CMDLINE", str(cmdline))
    assert "the kernel console is on ttyS0" in port_conflicts("/dev/ttyS0")


def test_the_pis_serial0_alias_counts_too(tmp_path, monkeypatch):
    cmdline = tmp_path / "cmdline"
    cmdline.write_text("console=serial0,115200 console=tty1\n")
    monkeypatch.setattr(sx126x, "CMDLINE", str(cmdline))
    assert port_conflicts("/dev/ttyS0")


def test_a_console_elsewhere_is_fine(tmp_path, monkeypatch):
    cmdline = tmp_path / "cmdline"
    cmdline.write_text("console=tty1 splash\n")
    monkeypatch.setattr(sx126x, "CMDLINE", str(cmdline))
    assert not [p for p in port_conflicts("/dev/ttyS0") if "console" in p]


def test_another_process_holding_the_port_is_named(tmp_path, monkeypatch):
    """Stand-in for the auto-login shell: a process with the file open."""
    monkeypatch.setattr(sx126x, "CMDLINE", str(tmp_path / "none"))
    port = tmp_path / "ttyFAKE"
    port.write_text("")
    with open(port) as handle:
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"],
                                  stdin=handle)
    try:
        import time
        deadline = time.monotonic() + 5
        found = []
        while not found and time.monotonic() < deadline:
            found = port_conflicts(str(port))
            time.sleep(0.05)
        assert any(f"({holder.pid}) has ttyFAKE open" in p for p in found)
    finally:
        holder.kill()
        holder.wait()


# --- the Messenger on the same board -------------------------------------------
@pytest.fixture
def pty_port():
    """A serial port both apps could open: the slave end of a pty."""
    import os
    master, slave = os.openpty()
    try:
        yield os.ttyname(slave)
    finally:
        os.close(slave)
        os.close(master)


@pytest.fixture
def messenger_holds(tmp_path, pty_port):
    """The Messenger running, with the radio's port open and locked."""
    folder = tmp_path / "Messenger"
    folder.mkdir()
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import serial, sys; port = serial.Serial(sys.argv[1], exclusive=True); "
         "print('ready', flush=True); sys.stdin.read()", pty_port],
        cwd=folder, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        yield pty_port
    finally:
        child.stdin.close()
        child.wait(timeout=5)


def test_a_second_opener_is_refused_until_the_first_closes(pty_port):
    first = SX126x(port=pty_port, addr=1, freq_mhz=868)
    try:
        with pytest.raises(sx126x.PortBusy):
            SX126x(port=pty_port, addr=1, freq_mhz=868)
    finally:
        first.close()
    SX126x(port=pty_port, addr=1, freq_mhz=868).close()


def test_the_holder_is_named_by_its_app_folder(messenger_holds):
    with pytest.raises(sx126x.PortBusy) as refused:
        SX126x(port=messenger_holds, addr=1, freq_mhz=868)
    assert refused.value.holders == ["Messenger"]
    assert any(p.startswith("Messenger (") for p in port_conflicts(messenger_holds))


def test_the_app_says_which_app_to_quit(messenger_holds):
    from app.config.settings import Settings
    from app.main import WalkieApp
    from app.ui.screens import TALK, ViewState

    app = WalkieApp.__new__(WalkieApp)
    app.settings = Settings()
    app.settings.radio.port = messenger_holds
    app.settings.radio.address = 1
    app.state = ViewState(screen=TALK)
    app.radio = app.link = None
    app._open_radio()
    assert app.link is None
    assert app.radio_offline == "radio busy: quit Messenger"
    assert app.state.radio_note == "radio busy: quit Messenger"


# --- a module stuck in configuration mode -------------------------------------
class ConfigMode(FakeModule):
    """M1 held high: every write is a bad setting, answered FF FF FF."""

    def write(self, data):
        self.written.extend(data)
        self._receive(b"\xff\xff\xff")
        return len(data)


def test_a_module_in_configuration_mode_is_noticed(monkeypatch):
    from app.radio import protocol
    from app.radio.link import LoraLink

    port = ConfigMode(addr=1, rssi_byte=None)
    monkeypatch.setattr("serial.Serial", lambda *a, **k: port)
    link = LoraLink(SX126x(port="fake", addr=1, freq_mhz=868), duty_cycle_percent=100.0)
    link.start()
    try:
        for _ in range(3):
            link.send_pair()
        import time
        deadline = time.monotonic() + 5
        while link.stats.config_mode_replies < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert link.stats.config_mode_replies == 3
        assert link.stats.messages_rx == 0
    finally:
        link.stop()
