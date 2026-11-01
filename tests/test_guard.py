import os
import signal
import subprocess
import sys
import time

# Prints its pid, then waits; stands in for `tailscale funnel`.
CHILD = ["sh", "-c", "echo $$; exec sleep 60"]


def gone(pid: int, seconds: float = 15) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


def test_stops_the_command_when_the_parent_is_killed():
    # A parent that starts the guard, then dies by SIGKILL with no cleanup.
    parent = subprocess.Popen(
        [sys.executable, "-c", (
            "import os, signal, subprocess, sys\n"
            f"subprocess.Popen([sys.executable, '-m', 'anki_voice.guard', *{CHILD!r}], stdin=subprocess.PIPE)\n"
            "sys.stdin.read()\n"
            "os.kill(os.getpid(), signal.SIGKILL)\n"
        )],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    child = int(parent.stdout.readline())
    parent.stdin.close()
    assert parent.wait(5) == -signal.SIGKILL
    assert gone(child)


def test_ignores_ctrl_c_and_hangup_while_the_parent_lives():
    guard = subprocess.Popen(
        [sys.executable, "-m", "anki_voice.guard", *CHILD], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    child = int(guard.stdout.readline())
    guard.send_signal(signal.SIGINT)
    guard.send_signal(signal.SIGHUP)
    time.sleep(0.5)
    assert guard.poll() is None
    guard.stdin.close()
    assert guard.wait(15) is not None
    assert gone(child)


def test_exits_with_the_command():
    guard = subprocess.Popen([sys.executable, "-m", "anki_voice.guard", "sh", "-c", "exit 3"], stdin=subprocess.PIPE)
    assert guard.wait(5) == 3
    guard.stdin.close()
