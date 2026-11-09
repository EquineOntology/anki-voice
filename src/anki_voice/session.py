"""`anki-voice session`: put the server online for one review, then take it down.

Opens Anki, connects Tailscale if needed, starts the server and a foreground
Tailscale Funnel, and keeps the Mac awake. It says it's online once the server
answers from the internet, and the idle timer starts then; after that it keeps
checking, and says so if the server stops answering. Everything stops when the review is
done, after a stretch with no activity, on Ctrl-C, or when the terminal closes;
Tailscale goes back to the state it was in. If the process is killed outright,
the Funnel still goes down, but Tailscale stays connected.
"""

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from functools import cache
from importlib.metadata import version
from pathlib import Path

import uvicorn

from .anki import AnkiConnect, AnkiError
from .review import Review

MAC_APP_CLI = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
IDLE_MINUTES = int(os.environ.get("ANKI_VOICE_IDLE_MINUTES", "30"))
DONE_GRACE_SECONDS = 120  # time for Claude to say the review is over


def say(message: str) -> None:
    print(f"anki-voice: {message}", flush=True)


@cache
def tailscale_cli() -> str:
    """The Mac app's own CLI if the app is installed, else `tailscale` on PATH.

    The app comes first: a CLI installed some other way may talk to a different
    tailscaled than the one the app runs.
    """
    if os.path.exists(MAC_APP_CLI):
        return MAC_APP_CLI
    if found := shutil.which("tailscale"):
        return found
    sys.exit("anki-voice: Tailscale isn't installed: found neither the Mac app nor `tailscale` on PATH.")


def tailscale(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([tailscale_cli(), *args], capture_output=True, text=True)


def tailscale_state() -> dict:
    result = tailscale("status", "--json")
    if result.returncode and not result.stdout:
        return {"BackendState": "NoState"}
    return json.loads(result.stdout)


def password() -> str:
    """The sign-in password: ANKI_VOICE_PASSWORD, or what ANKI_VOICE_PASSWORD_COMMAND prints."""
    if not (value := os.environ.get("ANKI_VOICE_PASSWORD", "")):
        if not (command := os.environ.get("ANKI_VOICE_PASSWORD_COMMAND")):
            sys.exit("anki-voice: set ANKI_VOICE_PASSWORD, or ANKI_VOICE_PASSWORD_COMMAND to a command that prints it.")
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        if result.returncode:
            sys.exit(f"anki-voice: ANKI_VOICE_PASSWORD_COMMAND failed: {result.stderr.strip()}")
        value = result.stdout.strip()
    if len(value) < 16:
        sys.exit("anki-voice: the password is shorter than 16 characters.")
    return value


async def wait_for_anki(anki: AnkiConnect, seconds: int = 60) -> None:
    deadline = time.time() + seconds
    while True:
        try:
            await anki.call("version")
            return
        except AnkiError:
            if time.time() > deadline:
                sys.exit("anki-voice: Anki didn't answer. Is the AnkiConnect add-on installed?")
            await asyncio.sleep(1)


def connect_tailscale() -> tuple[str, bool]:
    """Return the Mac's public hostname, and whether this session brought Tailscale up."""
    state = tailscale_state()
    brought_up = False
    if state["BackendState"] == "NoState":
        subprocess.run(["open", "-ga", "Tailscale"])
        time.sleep(3)
        state = tailscale_state()
    if state["BackendState"] == "NeedsLogin":
        sys.exit("anki-voice: Tailscale isn't signed in. Sign in from its menu bar icon, then try again.")
    if state["BackendState"] != "Running":
        if tailscale("up").returncode:
            sys.exit("anki-voice: `tailscale up` failed.")
        brought_up = True
        state = tailscale_state()
    host = (state.get("Self") or {}).get("DNSName", "").rstrip(".")
    if not host:
        sys.exit("anki-voice: Tailscale has no DNS name for this Mac. Turn on MagicDNS in the admin console.")
    return host, brought_up


def dig(*args: str) -> list[str] | None:
    """The answer's records, empty if there are none; None if no server answered."""
    try:
        result = subprocess.run(["dig", "+short", *args], capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return None
    return result.stdout.split() if result.returncode == 0 else None


def zone_nameserver(host: str) -> tuple[str, str] | None:
    """The zone `host` belongs to, and one of its nameservers."""
    labels = host.split(".")
    for i in range(1, len(labels) - 1):
        zone = ".".join(labels[i:])
        if nameservers := dig("NS", zone):
            return zone, nameservers[0]
    return None


def published(host: str) -> list[str] | None:
    """The addresses the public internet resolves `host` to; empty until the Funnel's record exists.

    Asks the zone's own nameserver, without recursion: a public resolver asked
    before the record exists caches "no such name" for minutes. None if the
    nameserver couldn't be asked or didn't answer.
    """
    if not (found := zone_nameserver(host)):
        return None
    records = dig("+norec", "+time=3", "+tries=1", f"@{found[1]}", host, "A")
    return None if records is None else [r for r in records if not r.endswith(".")]  # addresses, not CNAME targets


def miss_cache_seconds(host: str) -> int:
    """How long a resolver may keep answering "no such name" for `host`: its zone's SOA minimum."""
    if found := zone_nameserver(host):
        if soa := dig("+norec", "+time=3", "+tries=1", f"@{found[1]}", found[0], "SOA"):
            try:
                return int(soa[-1])
            except ValueError:
                pass
    return 300


def answers(host: str, address: str) -> bool:
    """Whether the server answers through the Funnel relay at `address`.

    Pinning the relay's address matters: the Mac would otherwise reach itself
    over the tailnet. The server's 401 sign-in challenge proves the relay, the
    certificate and the server all work.
    """
    result = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-m", "5",
         "--resolve", f"{host}:443:{address}", f"https://{host}/mcp"],
        capture_output=True, text=True,
    )
    return result.stdout == "401"


def reachable(host: str) -> bool:
    return bool(addresses := published(host)) and answers(host, addresses[0])


async def pause(server: uvicorn.Server, seconds: int) -> None:
    """Sleep, but return as soon as the server is stopping."""
    for _ in range(seconds):
        if server.should_exit:
            return
        await asyncio.sleep(1)


async def wait_until_reachable(server: uvicorn.Server, host: str) -> bool:
    """Wait until the server answers from the internet; False if the session stopped first.

    If the record was missing at any check, this also waits until resolvers that
    looked the name up in the meantime have forgotten the miss. Claude's backend
    may be one of them; nothing on the Mac can ask its resolver directly.
    """
    cache = await asyncio.to_thread(miss_cache_seconds, host)
    missed, appeared = False, None
    while not server.should_exit:
        records = await asyncio.to_thread(published, host)
        if records == []:
            missed, appeared = True, None
        elif records:  # None, a lookup that got no answer, says nothing about the record
            if appeared is None:
                appeared = time.time()
                if missed:
                    say(f"the address is public; waiting {cache} s for DNS caches that missed it to expire. Don't connect yet.")
            if (not missed or time.time() - appeared >= cache) and await asyncio.to_thread(reachable, host):
                return True
        await pause(server, 10)
    return False


async def announce_when_public(server: uvicorn.Server, review: Review, host: str, ready: asyncio.Event) -> None:
    """Say it's online once the server answers from the internet, then keep checking that it does."""
    if not await wait_until_reachable(server, host):
        return
    review.last_activity = time.time()  # the idle timer starts from here
    ready.set()
    say(f"online at https://{host}/mcp (connector URL). Stops when the review is done or after {IDLE_MINUTES} idle minutes.")

    failures = 0
    while not server.should_exit:
        await pause(server, 60)
        if server.should_exit:
            return
        if await asyncio.to_thread(reachable, host):
            if failures >= 2:
                say(f"reachable again at https://{host}/mcp.")
            failures = 0
        else:
            failures += 1
            if failures == 2:
                say("can't reach the server from the internet anymore; Claude can't connect until it's back.")


async def watch(server: uvicorn.Server, review: Review, funnel: subprocess.Popen, ready: asyncio.Event) -> None:
    parent = os.getppid()  # `uv run`, which leaves this process running if it's killed with -9
    while not server.should_exit:
        await asyncio.sleep(5)
        now = time.time()
        if funnel.poll() is not None:
            say("the Funnel stopped; shutting down.")
        elif os.getppid() != parent:
            say("the process that started this session is gone; shutting down.")
        elif review.finished_at and now - review.finished_at > DONE_GRACE_SECONDS:
            say("review done; shutting down.")
        elif now - review.last_activity > IDLE_MINUTES * 60:
            if ready.is_set():
                say(f"no activity for {IDLE_MINUTES} minutes; shutting down.")
            else:
                say(f"the address still isn't public after {IDLE_MINUTES} minutes; shutting down.")
        else:
            continue
        server.should_exit = True


async def run(port: int, state_file: Path) -> None:
    from .server import create_http_app, review

    # `uv run` inside the repo runs the checkout, not the installed tool: say which.
    say(f"version {version('anki-voice')}, running from {sys.prefix.replace(str(Path.home()), '~')}")
    subprocess.run(["open", "-ga", "Anki"])
    await wait_for_anki(review.anki)
    await review.anki.clear_study_queue()
    secret = password()  # before Tailscale, so nothing is left up while the password command asks
    host, brought_up = connect_tailscale()

    config = uvicorn.Config(create_http_app(f"https://{host}", secret, state_file), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    # A closed terminal sends SIGHUP. uvicorn re-raises the SIGTERM it caught once it has
    # shut down, which would end the process before the cleanup below without a handler.
    for sig in (signal.SIGHUP, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, setattr, server, "should_exit", True)
    caffeinate = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    # The guard stops the Funnel once this process is gone, even after `kill -9`.
    funnel = subprocess.Popen(
        [sys.executable, "-m", "anki_voice.guard", tailscale_cli(), "funnel", str(port)],
        stdin=subprocess.PIPE, stdout=sys.stdout, stderr=sys.stderr,
    )
    # Tailscale publishes the Funnel's DNS record only once the Funnel is up, and it
    # can take ten minutes or more to appear. Connecting before then fails, and
    # leaves the miss cached in whichever resolver was asked.
    review.last_activity = time.time()
    ready = asyncio.Event()
    say("Funnel started; waiting for its public address to resolve (often 10+ minutes). Don't connect yet.")
    try:
        await asyncio.gather(
            server.serve(), watch(server, review, funnel, ready), announce_when_public(server, review, host, ready),
        )
    finally:
        funnel.stdin.close()  # the guard stops the Funnel, which unpublishes itself on exit
        try:
            funnel.wait(15)
        except subprocess.TimeoutExpired:
            funnel.kill()
        tailscale("funnel", "reset")
        caffeinate.terminate()
        if brought_up:
            tailscale("down")
        say("offline.")
