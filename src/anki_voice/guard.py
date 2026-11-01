"""Run a command until the process that started this one is gone.

`python -m anki_voice.guard CMD...` starts CMD and stops it once its stdin
closes, which happens however the starting process exits, `kill -9` included,
as long as that process holds the only write end of the pipe. It exits with
CMD's exit code when CMD stops on its own.
"""

import os
import signal
import subprocess
import sys
import threading


def main() -> None:
    # Ctrl-C and a closed terminal reach the whole process group; the session
    # stops by closing the pipe. Handlers (not SIG_IGN) so CMD gets the defaults.
    for sig in (signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: None)
    child = subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL)

    def stop_when_orphaned() -> None:
        # The raw fd: a thread blocked in buffered stdin aborts the interpreter's shutdown.
        while os.read(0, 1024):
            pass
        child.send_signal(signal.SIGINT)
        try:
            child.wait(10)
        except subprocess.TimeoutExpired:
            child.kill()

    threading.Thread(target=stop_when_orphaned, daemon=True).start()
    sys.exit(child.wait())


if __name__ == "__main__":
    main()
