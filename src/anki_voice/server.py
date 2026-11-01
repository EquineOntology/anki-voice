"""MCP server for reviewing Anki cards by voice from the Claude app."""

import argparse
import asyncio
import logging
import os
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

import uvicorn
from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from .anki import AnkiConnect
from .cards import VOICE_FIELD, parse_languages
from .oauth import PasswordOAuthProvider
from .review import FLAGS, VOICE_OVERHEAD_SECONDS, Review

INSTRUCTIONS = """\
These tools run a spoken Anki review. The person is listening and answering
by voice, usually while doing something else, so keep every turn short.

Running the review:
- Start with next_card. Each card comes with a prompt (the front) and the
  answer (the back). Read the prompt aloud and never read or hint at the answer.
  The back often carries explanations as well (the case a preposition takes,
  why a tense is used). The person doesn't need to say them, but they give the
  answer away, so keep them back until the person has answered. For a cloze,
  working out the gap, its case and its form is the exercise: read the
  sentence with the gap and the card's own bracketed hint, and add nothing.
- If the person asks for one topic ("my Spanish review", "just maths"), pass
  it as focus, using the word that appears in the deck name ("Math", not
  "maths"). grade and skip_card stay on that topic. When they want everything
  again, call next_card without focus.
- Say only the prompt, with no preamble like "here's your next card".
  Read maths notation as you would say it, not as LaTeX.
- A card with a language (its "language" is set) is read with the app's voice
  in that language, and only the person can switch it, in the app's voice
  settings (Spoken language). Cards come grouped by language, and a result
  with switch_voice_to means the voice has to change first: say only "Switch
  the spoken language to <the language given>, then tell me", and read the
  card once they say it's done. With status "done", say it after the summary.
  A few words of the person's usual language on such a card are fine in that
  voice.
- A card with a language gets speech in that language only. Give the verdict
  and the correct answer in it (for Spanish: "Correcto." / "No del todo: la
  escalera."). When a prompt is a sentence in that language followed by its
  translation, read only the sentence; give the translation only if the
  person asks. A prompt in the person's usual language stays in it.
  A bracketed hint such as "(noun)" or "(er~)" belongs to the prompt: say it.
- Judge the spoken answer against the card's answer; for a cloze, that is the
  missing words alone. Grade "good" only if the answer is correct and
  complete; anything partial or wrong is "again". For nouns in a language
  whose articles show gender, the article is part of the answer.
  Speech-to-text mangles spelling, so judge what was said, not how it was
  transcribed.
- A card may come with instructions, which the person wrote for that card:
  what counts as an answer, how to read the prompt, and so on. Follow them;
  where they differ from these rules, they win. Don't read them aloud.
- If the person asks to change how a card is judged or read ("accept either
  order on this one", "don't ask for the plural here"), call set_instructions
  on that card with its complete new instructions: the current ones with the
  change worked in, unless they asked to replace them. Keep the person's
  intent, briefly. Confirm in a few words. If they also dispute the grade,
  undo and regrade first: after set_instructions the last grade can't be
  undone.
- If the person wants a card flagged (it's wrong, odd, needs fixing), call
  flag_card on the card they mean: the current one, or the one just answered.
  They name the colour; take the nearest of Anki's (cyan is turquoise,
  violet is purple). If they name none, ask which, listing red, orange,
  green, blue, pink, turquoise and purple. "Unflag it" is color "none".
  Flagging doesn't answer a card, so carry on where you were. Confirm in a few
  words. If they also dispute the grade, undo and regrade first: after
  flag_card the last grade can't be undone.
- Say the verdict in a few words. When it's "again", give only the correct
  answer (for a cloze, the missing words), in one short sentence. Be matter of
  fact: no explanations, grammar notes, extra details from the card, praise,
  or remarks about earlier cards or past reviews, unless the person asks.
  Then call grade, which records the result in Anki and returns the next card, and read that card's prompt straight away.
- If the person says "skip", call skip_card: the card stays due in Anki.
  If they say a card doesn't work by voice, or not to ask it again, call
  skip_card with always=true: its note gets tagged in Anki and voice reviews
  leave it out from then on. Confirm in a few words that it won't come back.
  "I don't know" is an answer: grade it "again".
- If the person says to undo, or disputes your verdict, call undo: it takes
  back the last grade or skip and returns that card. If they said what the
  grade should be, or their dispute makes it clear, call grade on it right
  away without reading the prompt again; otherwise read its prompt again.
  Only the last grade or skip can be undone, and only once; for anything
  earlier, say they can change it in Anki.
- status "wait" means only cards still in learning are left; tell the person
  how many minutes until the next one and offer to wait. status "done" means
  the review is finished for today, or for the topic in focus; say so, with
  how many cards are still due in other topics and how many were left out
  because they need a picture or a sound.
"""

review = Review(
    anki=AnkiConnect(os.environ.get("ANKICONNECT_URL", "http://127.0.0.1:8765")),
    exclude_decks=[d.strip() for d in os.environ.get("ANKI_VOICE_EXCLUDE_DECKS", "").split(",") if d.strip()],
    languages=parse_languages(os.environ.get("ANKI_VOICE_LANGUAGES", "")),
    learn_ahead_minutes=int(os.environ.get("ANKI_VOICE_LEARN_AHEAD_MINUTES", "20")),
    voice_overhead_seconds=float(os.environ.get("ANKI_VOICE_OVERHEAD_SECONDS", VOICE_OVERHEAD_SECONDS)),
    dry_run=os.environ.get("ANKI_VOICE_DRY_RUN") == "1",
)

CardId = Annotated[int, Field(description="card_id of the card being answered, from the last result")]


def create_server(oauth: PasswordOAuthProvider | None = None, public_url: str | None = None) -> MCPServer:
    """The MCP server; with `oauth`, every MCP request needs a token from a password sign-in."""
    auth_kwargs: dict[str, Any] = {}
    if oauth:
        auth_kwargs = {
            "auth": AuthSettings(
                issuer_url=public_url,
                resource_server_url=f"{public_url}/mcp",
                client_registration_options=ClientRegistrationOptions(enabled=True),
                revocation_options=RevocationOptions(enabled=True),
                validate_token_resource=False,  # this server is the only resource the tokens are for
            ),
            "auth_server_provider": oauth,
        }
    mcp = MCPServer("Anki Voice", instructions=INSTRUCTIONS, **auth_kwargs)
    if oauth:
        mcp.custom_route("/login", methods=["GET", "POST"])(oauth.login)

    @mcp.tool()
    async def next_card(
        focus: Annotated[
            str | None,
            Field(description='Review one topic only: part of a deck name ("Spanish", "Math", "History") or a card language '
                              '("Spanish" also picks up Spanish cards in other decks). Leave empty for every deck.'),
        ] = None,
    ) -> dict[str, Any]:
        """Start or restart the review and get the next card due, with its prompt, its answer and how many are left."""
        review.focus = focus or None
        return await review.next()

    @mcp.tool()
    async def grade(
        card_id: CardId,
        result: Annotated[Literal["again", "good"], Field(description='"good" if the answer was correct and complete, else "again"')],
    ) -> dict[str, Any]:
        """Record the answer to a card in Anki, then return the next card."""
        return await review.grade(card_id, result)

    @mcp.tool()
    async def skip_card(
        card_id: CardId,
        always: Annotated[bool, Field(description="true to leave this card out of every future voice review too")] = False,
    ) -> dict[str, Any]:
        """Leave a card ungraded and return the next card. With always=true, voice reviews leave the card out for good."""
        return await review.skip(card_id, always)

    @mcp.tool()
    async def set_instructions(
        card_id: Annotated[int, Field(description="card_id of the card the person means: the current one or one asked earlier")],
        instructions: Annotated[str, Field(description="The complete new instructions for the card's note; replaces the old ones, empty clears them")],
    ) -> dict[str, Any]:
        """Change the instructions on a card's note in Anki, at the person's request. They apply to every card of that note."""
        return await review.set_instructions(card_id, instructions)

    @mcp.tool()
    async def flag_card(
        card_id: Annotated[int, Field(description="card_id of the card the person means: the current one or one asked earlier")],
        color: Annotated[Literal[tuple(FLAGS)], Field(description='The colour the person named; "none" removes the flag')],
    ) -> dict[str, Any]:
        """Flag a card in Anki at the person's request, for them to look at later. It doesn't answer the card."""
        return await review.flag(card_id, color)

    @mcp.tool()
    async def undo() -> dict[str, Any]:
        """Take back the last grade or skip (once only) and return that card as the next one."""
        return await review.undo()

    return mcp


def create_http_app(public_url: str, password: str, state_file: Path):
    public_url = public_url.rstrip("/")
    oauth = PasswordOAuthProvider(password, state_file, login_url=f"{public_url}/login")
    mcp = create_server(oauth, public_url)
    host = urlparse(public_url).netloc
    return mcp.streamable_http_app(
        transport_security=TransportSecuritySettings(
            allowed_hosts=["127.0.0.1:*", "localhost:*", host, f"{host}:*"],
            allowed_origins=["https://claude.ai"],
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    session = commands.add_parser("session", help="go online through Tailscale Funnel for one review, then stop")
    serve = commands.add_parser("serve", help="run the HTTP server on its own, behind a tunnel you manage")
    serve.add_argument("--stdio", action="store_true", help="serve over stdio instead, without sign-in")
    commands.add_parser("add-field", help=f'add the "{VOICE_FIELD}" field to every note type that lacks it')
    for command in (session, serve):
        command.add_argument("--port", type=int, default=int(os.environ.get("ANKI_VOICE_PORT", "8787")))
    args = parser.parse_args()
    # httpx logs every AnkiConnect request at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    state_file = Path(os.environ.get("ANKI_VOICE_STATE_FILE", "~/.local/share/anki-voice/oauth.json")).expanduser()

    if args.command == "add-field":
        # Anki may wait for an answer in its window before changing a note type.
        anki = AnkiConnect(os.environ.get("ANKICONNECT_URL", "http://127.0.0.1:8765"), timeout=600)
        added = asyncio.run(anki.add_field_to_every_note_type(VOICE_FIELD))
        print(f"Added to: {', '.join(added)}" if added else "Every note type already has it.")
        return
    if args.command == "session":
        from .session import run

        try:
            asyncio.run(run(args.port, state_file))
        except KeyboardInterrupt:
            pass  # run() has already cleaned up on its way out
        return
    if args.stdio:
        create_server().run()
        return

    public_url = os.environ.get("ANKI_VOICE_PUBLIC_URL", "")
    if not public_url.startswith("https://"):
        parser.error("set ANKI_VOICE_PUBLIC_URL to the tunnel's https:// address")
    from .session import password

    uvicorn.run(create_http_app(public_url, password(), state_file), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
