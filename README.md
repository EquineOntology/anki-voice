# anki-voice

Hands-free Anki review via Claude voice mode.

Claude reads each card's front, listens to your answer, tells you whether it
was right, and records the grade in Anki. It works by running a small server
on your Mac next to Anki and Claude connects to it as a custom connector.

## Setup

Your agent can take care of the setup, except for a few like generating a
password, signing up for Tailscale, etc.

### Prereqs

- A Mac: anki-voice runs on macOS only, `serve` included
- [Anki](https://apps.ankiweb.net) with a recent
  [AnkiConnect](https://foosoft.net/projects/anki-connect/) add-on, one that
  has its JSON-RPC API v1 (see [Checking AnkiConnect](#checking-ankiconnect))
- [uv](https://docs.astral.sh/uv/)
- [Tailscale](https://tailscale.com), with
  MagicDNS and [Funnel](https://tailscale.com/kb/1223/funnel) enabled for your tailnet. It
  puts the server on the internet for the length of a review, so Claude can
  reach it. You can do this on a free account.
- A place to keep the sign-in password, ideally a password manager with a
  command-line tool
- A Claude account that can add custom connectors

### First-time setup

1. **Install it**:

   ```bash
   uv tool install anki-voice
   ```
2. **Choose a password** of at least 16 characters. Claude will ask for it
   the first time it connects. Tell the server how to get it by setting, in
   your shell profile, a command that prints it:

   ```bash
   export ANKI_VOICE_PASSWORD_COMMAND='<your password manager> get anki-voice'
   ```

   or the password itself, in `ANKI_VOICE_PASSWORD`. Prefer the command: the
   password then exists only in the server's memory, while
   `ANKI_VOICE_PASSWORD` keeps it in plain text in your profile and in the
   environment of everything started from your shell.
3. **Add the instructions field** to your note types:

   ```bash
   anki-voice add-field
   ```

   This adds an `AnkiVoice instructions` field to every note type that lacks
   it (see [Instructions for single cards](#instructions-for-single-cards)).
   Changing a note type needs a full sync, so sync your other devices first,
   then choose "upload" at the next sync. Run it again after you create a
   new note type.
4. **Name the languages you study**, if any, so their cards are read in that
   language (see [Cards in other languages](#cards-in-other-languages)):

   ```bash
   export ANKI_VOICE_LANGUAGES='Spanish 🇪🇸 🇲🇽'
   ```
5. **Leave out decks** that don't work by voice, if you like:

   ```bash
   export ANKI_VOICE_EXCLUDE_DECKS='Geography::Flags,Music'
   ```
6. **Add the connector in Claude** once a session is online (see below): add a
   custom connector with the address the session prints, and choose **Sign in
   now** and **Register automatically**. A sign-in page opens; enter the
   password.

## Run a review

```bash
anki-voice session
```

Then **wait until it prints `online at https://…/mcp`**. This often takes 10
minutes or more the first time in a while (see
[Why the wait](#why-the-wait)). Connecting earlier fails.

Once it's online, start voice mode in Claude and say something like "let's do
my Anki review" or "just my Spanish cards".

The session stops on its own two minutes after the review is done, or after 30
minutes without activity. Ctrl-C or closing the terminal stops it too. Either
way, the server goes offline and nothing is left on the internet.

### What you can say

| Say | What happens |
|---|---|
| your answer | Claude says whether it was right, gives the correct answer if not, and moves on |
| "I don't know" | Counts as wrong ("again") |
| your answer and "easy", or "that one was hard" | A correct answer is graded "easy" or "hard" instead of "good". Said after the grade, it changes that grade |
| "skip" | The card stays due in Anki; you get the next one |
| "don't ask me this one again" | The card's note gets the tag `anki-voice::skip` and voice reviews leave it out from now on. Remove the tag in Anki to bring it back |
| "undo", or "that was right!" | Takes back the last grade or skip, once |
| "flag this red" | Flags the card in Anki for you to look at later. Claude asks for a colour if you don't name one |
| "on this card, accept either order" | Claude rewrites the card's instructions (see below) |
| "just maths" / "everything again" | Limits the review to one topic, or lifts the limit |

Grading is strict: only a correct and complete answer is "good" (or "hard" or
"easy", when you ask). For nouns in languages whose articles show gender, the
article is part of the answer.

### Cards in other languages

Each language in `ANKI_VOICE_LANGUAGES` is a name followed by any flag emoji
that mark its cards. A card is in that language when its deck or note type name
contains the name (`Spanish`, `Study::Spanish`, `Spanish vocab`), or its front
or back shows one of the flags. Separate languages with commas:
`'Spanish 🇪🇸 🇲🇽, French 🇫🇷'`.

Claude reads these cards with its voice for that language, and speaks only
that language on them. Or that's how it works most of the time, it's a bit
finicky. If it doesn't work well, try doing a session only on that deck.

### Instructions for single cards

The `AnkiVoice instructions` field on a note tells Claude how to treat its
cards: what counts as an answer, how to read the prompt. Claude follows them
over its general rules and never reads them aloud. No card template shows the
field, so the cards look the same in Anki.

You can write them in Anki, or ask Claude during a review ("on this card,
accept either order").

### While a session runs

**Don't use Anki's window.** Two reasons:

- "undo" uses Anki's own undo, which takes back whatever changed last, even if
  that was you in the window.
- Once Anki's window shows a deck or its reviewer, Anki only accepts answers
  for the card at the top of its own queue. The session puts the window back on
  the Decks screen when it starts, and again if Anki refuses an answer.

### Which cards come up

1. Cards in learning that are due now
2. Reviews due, shuffled within each due day
3. Cards in learning due within the next 20 minutes

New cards **do not** come up. Neither do suspended or buried cards, cards from
excluded decks, or cards whose front needs a picture or a sound. Claude tells
you at the end how many of those last were left out.

Each grade is saved in Anki the moment it's given, so a card you got wrong
comes back when its learning step makes it due. Anki logs each answer with the
time from when Claude got the card to when it was graded, less 10 seconds for
the speech you can't skip.

## Technical details

### Security

- **Online only during a review.** The Funnel and its public address exist only
  while a session runs. A small guard process takes the Funnel down as soon as
  the session is gone, even after `kill -9`. To check that nothing is
  published:

  ```bash
  tailscale funnel status
  ```

  It should say "No serve config". If only the Mac app is installed, the
  command is `/Applications/Tailscale.app/Contents/MacOS/Tailscale`.
- **Password sign-in.** The server signs Claude in itself, with one password.
  Every request without a valid token is refused.
- **Only Claude can sign in.** The sign-in only ever sends its result to
  Claude's own address, so a link to the sign-in page can't be used to phish
  the password into someone else's hands.
- **Tokens expire.** Claude's access lasts an hour and is renewed for up to 48
  hours of disuse; after that, it asks for the password again.
- **Tokens are stored as hashes**, in `~/.local/share/anki-voice/oauth.json`
  (readable only by you), so restarting doesn't sign Claude out. Delete that
  file to sign everything out.
- **Guessing is limited.** After 10 wrong passwords in 15 minutes, sign-in is
  refused for the rest of that window. The limit is shared by everyone, so
  someone who finds the address can keep you from signing in for as long as
  they keep guessing. Claude stays connected if it already is.
- **Card text reaches Claude as-is.** Each card's front, back and
  `AnkiVoice instructions` field go to Claude, including text a template
  hides, and Claude follows the instructions field over its general rules. A
  shared deck written by someone else could use that to have Claude misgrade
  cards or rewrite other notes' instructions. Check the instructions field of
  shared decks before you review them by voice, or leave them out with
  `ANKI_VOICE_EXCLUDE_DECKS`.

### Why the wait when starting a session

Tailscale publishes the server's public address in DNS only when the Funnel
starts, and the record often takes 10 minutes or more to appear. If anything
looks the address up before then, such as Claude when you try to connect, the
answer "no such name" is cached for up to 5 minutes. So the session waits until
the record exists, then until any such cached answer has expired, then until a
request through the public relay actually reaches the server. Only then does it
say it's online. After that it checks every minute and tells you if the server
stops answering.

### Other ways to run it

**Behind a tunnel of your own**, instead of Tailscale:

```bash
export ANKI_VOICE_PUBLIC_URL=https://<your tunnel's address>
anki-voice serve
```

It gets the password the same way a session does. The server listens on
`127.0.0.1:8787`; point the tunnel there. The connector
address is `$ANKI_VOICE_PUBLIC_URL/mcp`. Unlike a session, this runs until you
stop it.

**For a local client** such as Claude Code, without sign-in:

```bash
anki-voice serve --stdio
```

### Settings

All settings are environment variables.

| Variable | Default | What it sets |
|---|---|---|
| `ANKI_VOICE_PASSWORD_COMMAND` | | A command that prints the sign-in password |
| `ANKI_VOICE_PASSWORD` | | The password itself, instead of a command |
| `ANKI_VOICE_LANGUAGES` | none | Languages to read in their own voice, each with its flags, comma-separated |
| `ANKI_VOICE_EXCLUDE_DECKS` | none | Decks to leave out, with their subdecks, comma-separated |
| `ANKI_VOICE_IDLE_MINUTES` | `30` | Minutes without activity before a session stops |
| `ANKI_VOICE_LEARN_AHEAD_MINUTES` | `20` | How far ahead cards in learning come up |
| `ANKI_VOICE_OVERHEAD_SECONDS` | `10` | Taken off each answer time logged in Anki |
| `ANKI_VOICE_DRY_RUN` | | `1` to try things out: grades, instructions and flags aren't saved to Anki |
| `ANKI_VOICE_PORT` | `8787` | Local port of the server |
| `ANKI_VOICE_STATE_FILE` | `~/.local/share/anki-voice/oauth.json` | Where tokens are stored |
| `ANKICONNECT_URL` | `http://127.0.0.1:8765` | Where AnkiConnect listens |

### Checking AnkiConnect

anki-voice uses AnkiConnect's long-standing API (version 6) to read cards and
change notes, and its newer JSON-RPC API v1 (at `/api/v1`) to grade, flag and
undo. Older AnkiConnect releases have only the first. To check yours, with Anki
open:

```bash
curl -s localhost:8765/api/v1 -d '{"jsonrpc":"2.0","id":1,"method":"operations.get","params":{"operationId":"check"}}'
```

A reply that starts with `{"jsonrpc"` (here, "Operation not found") means API v1
is there. anki-voice is tested with the AnkiConnect release of October 2026,
which needs Anki 26.8 or later.


## For developers

- How Claude runs a review is set by the instructions at the top of
`src/anki_voice/server.py`.

- You can run tests via uv: `uv run pytest`, and anki-voice from the
  checkout with `uv run anki-voice …`.
- To release: set the new version in `pyproject.toml` (and `uv lock`), commit,
  then push a tag `v<version>`. GitHub Actions runs the tests, and publishes to
  PyPI only if the tag matches the version.
