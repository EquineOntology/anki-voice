import asyncio
import time
from types import SimpleNamespace

import pytest

from anki_voice import session


class Funnel:
    def __init__(self, running=True):
        self.running = running

    def poll(self):
        return None if self.running else 1


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    async def instant(_):
        pass

    monkeypatch.setattr(session.asyncio, "sleep", instant)


def ready(is_ready=True):
    event = asyncio.Event()
    if is_ready:
        event.set()
    return event


async def watch_once(review, funnel=None, is_ready=True):
    server = SimpleNamespace(should_exit=False)
    await session.watch(server, review, funnel or Funnel(), ready(is_ready))
    return server.should_exit


@pytest.mark.anyio
async def test_stops_after_the_review_is_done():
    review = SimpleNamespace(finished_at=time.time() - 200, last_activity=time.time())
    assert await watch_once(review)


@pytest.mark.anyio
async def test_stops_when_idle():
    review = SimpleNamespace(finished_at=None, last_activity=time.time() - session.IDLE_MINUTES * 60 - 1)
    assert await watch_once(review)


@pytest.mark.anyio
async def test_stops_when_the_address_never_becomes_public():
    review = SimpleNamespace(finished_at=None, last_activity=time.time() - session.IDLE_MINUTES * 60 - 1)
    assert await watch_once(review, is_ready=False)


HOST = "mac.tailnet.ts.net"
RELAY = "185.40.234.55"


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]

    async def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(session.asyncio, "sleep", sleep)
    monkeypatch.setattr(session, "time", SimpleNamespace(time=lambda: now[0]))
    return now


def network(monkeypatch, records, reachable=None, stop_after_online=True):
    """Fake DNS and relay checks; returns the server, the review, the ready event and what was said."""
    server = SimpleNamespace(should_exit=False)
    said = []

    def say(message):
        said.append(message)
        if stop_after_online and message.startswith("online"):
            server.should_exit = True

    records = iter(records)
    monkeypatch.setattr(session, "say", say)
    monkeypatch.setattr(session, "miss_cache_seconds", lambda host: 300)
    monkeypatch.setattr(session, "published", lambda host: next(records, [RELAY]))
    monkeypatch.setattr(session, "reachable", reachable or (lambda host: True))
    return server, SimpleNamespace(last_activity=0), ready(False), said


@pytest.mark.anyio
async def test_online_at_once_when_public_from_the_start(monkeypatch, clock):
    server, review, event, said = network(monkeypatch, [])
    await session.announce_when_public(server, review, HOST, event)
    assert event.is_set()
    assert said == [f"online at https://{HOST}/mcp (connector URL). Stops when the review is done or after {session.IDLE_MINUTES} idle minutes."]
    assert review.last_activity == clock[0] == 1000


@pytest.mark.anyio
async def test_waits_out_cached_misses_once_the_record_appears(monkeypatch, clock):
    server, review, event, said = network(monkeypatch, [[], []])
    await session.announce_when_public(server, review, HOST, event)
    appeared = 1000 + 2 * 10
    assert said[0].startswith("the address is public; waiting 300 s")
    assert said[1].startswith("online")
    assert appeared + 300 <= review.last_activity < appeared + 310


@pytest.mark.anyio
async def test_a_lookup_without_an_answer_is_not_a_miss(monkeypatch, clock):
    server, review, event, said = network(monkeypatch, [None, None])
    await session.announce_when_public(server, review, HOST, event)
    assert [m.split(" ")[0] for m in said] == ["online"]
    assert review.last_activity == 1000 + 2 * 10


@pytest.mark.anyio
async def test_a_lookup_without_an_answer_keeps_the_wait_going(monkeypatch, clock):
    server, review, event, said = network(monkeypatch, [[], [RELAY], None, [RELAY]])
    await session.announce_when_public(server, review, HOST, event)
    appeared = 1000 + 10
    assert [m.split(" ")[0] for m in said] == ["the", "online"]
    assert appeared + 300 <= review.last_activity < appeared + 310


@pytest.mark.anyio
async def test_not_online_until_the_server_answers_through_the_relay(monkeypatch, clock):
    answers = iter([False, False, True])
    server, review, event, said = network(monkeypatch, [], reachable=lambda host: next(answers))
    await session.announce_when_public(server, review, HOST, event)
    assert said == [said[0]] and said[0].startswith("online")
    assert review.last_activity == 1000 + 2 * 10


@pytest.mark.anyio
async def test_says_when_it_stops_answering_and_when_it_is_back(monkeypatch, clock):
    checks = iter([True, False, False, False, True, False, True])
    server, review, event, said = network(monkeypatch, [], stop_after_online=False)

    def reachable(host):
        if (result := next(checks, None)) is None:
            server.should_exit = True
            return True
        return result

    monkeypatch.setattr(session, "reachable", reachable)
    await session.announce_when_public(server, review, HOST, event)
    assert [m.split(" ")[0] for m in said] == ["online", "can't", "reachable"]


@pytest.mark.anyio
async def test_not_online_if_the_session_stops_first(monkeypatch, clock):
    server, review, event, said = network(monkeypatch, [[]] * 100)
    server.should_exit = True
    await session.announce_when_public(server, review, HOST, event)
    assert not event.is_set() and not said


@pytest.fixture
def installed(monkeypatch, tmp_path):
    """Pretend the Mac app and a CLI on PATH are installed or not."""
    session.tailscale_cli.cache_clear()
    app = tmp_path / "Tailscale"

    def install(mac_app, on_path):
        if mac_app:
            app.touch()
        monkeypatch.setattr(session, "MAC_APP_CLI", str(app))
        monkeypatch.setattr(session.shutil, "which", lambda name: "/opt/homebrew/bin/tailscale" if on_path else None)
        return str(app)

    yield install
    session.tailscale_cli.cache_clear()


def test_the_mac_apps_cli_comes_first(installed):
    app = installed(mac_app=True, on_path=True)
    assert session.tailscale_cli() == app


def test_otherwise_tailscale_on_path(installed):
    installed(mac_app=False, on_path=True)
    assert session.tailscale_cli() == "/opt/homebrew/bin/tailscale"


def test_no_tailscale_stops_the_session(installed):
    installed(mac_app=False, on_path=False)
    with pytest.raises(SystemExit):
        session.tailscale_cli()


def test_password_from_the_environment(monkeypatch):
    monkeypatch.setenv("ANKI_VOICE_PASSWORD", "p" * 16)
    monkeypatch.setenv("ANKI_VOICE_PASSWORD_COMMAND", "exit 1")
    assert session.password() == "p" * 16


def test_password_from_a_command(monkeypatch):
    monkeypatch.delenv("ANKI_VOICE_PASSWORD", raising=False)
    monkeypatch.setenv("ANKI_VOICE_PASSWORD_COMMAND", "echo ' correct horse battery staple '")
    assert session.password() == "correct horse battery staple"


@pytest.mark.parametrize("command", [None, "exit 1", "echo short"])
def test_no_usable_password_stops_the_session(monkeypatch, command):
    monkeypatch.delenv("ANKI_VOICE_PASSWORD", raising=False)
    if command:
        monkeypatch.setenv("ANKI_VOICE_PASSWORD_COMMAND", command)
    else:
        monkeypatch.delenv("ANKI_VOICE_PASSWORD_COMMAND", raising=False)
    with pytest.raises(SystemExit):
        session.password()


def zone(monkeypatch, answer):
    calls = []

    def dig(*args):
        calls.append(args)
        if args == ("NS", "ts.net"):
            return ["ns1.example."]
        return answer(args) if "@ns1.example." in args else []

    monkeypatch.setattr(session, "dig", dig)
    return calls


def test_published_asks_the_zones_nameserver(monkeypatch):
    calls = zone(monkeypatch, lambda args: ["alias.example.", RELAY] if args[-2:] == (HOST, "A") else [])
    assert session.published(HOST) == [RELAY]
    assert calls[0] == ("NS", "tailnet.ts.net")
    assert "+norec" in calls[-1]


def test_unpublished_until_the_nameserver_answers(monkeypatch):
    zone(monkeypatch, lambda args: [])
    assert session.published(HOST) == []


def test_unknown_when_the_nameserver_doesnt_answer(monkeypatch):
    zone(monkeypatch, lambda args: None)
    assert session.published(HOST) is None


def test_miss_cache_is_the_zones_soa_minimum(monkeypatch):
    soa = "ns1.example. admin.example. 1630094812 86400 7200 604800 120".split()
    zone(monkeypatch, lambda args: soa if args[-2:] == ("ts.net", "SOA") else [])
    assert session.miss_cache_seconds(HOST) == 120


def test_miss_cache_defaults_to_five_minutes(monkeypatch):
    zone(monkeypatch, lambda args: [])
    assert session.miss_cache_seconds(HOST) == 300


@pytest.mark.parametrize("status, expected", [("401", True), ("502", False), ("000", False)])
def test_answers_only_with_the_servers_challenge_through_the_relay(monkeypatch, status, expected):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout=status)

    monkeypatch.setattr(session.subprocess, "run", run)
    assert session.answers(HOST, RELAY) is expected
    assert f"{HOST}:443:{RELAY}" in calls[0]


@pytest.mark.anyio
async def test_stops_when_the_funnel_dies():
    review = SimpleNamespace(finished_at=None, last_activity=time.time())
    assert await watch_once(review, Funnel(running=False))


@pytest.mark.anyio
async def test_stops_when_its_parent_dies(monkeypatch):
    review = SimpleNamespace(finished_at=None, last_activity=time.time())
    parents = iter([100, 1])
    monkeypatch.setattr(session.os, "getppid", lambda: next(parents))
    assert await watch_once(review)


@pytest.fixture
def anyio_backend():
    return "asyncio"
