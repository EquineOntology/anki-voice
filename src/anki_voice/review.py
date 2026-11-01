"""Which card comes next, and recording answers.

Every call asks Anki for the current state, so the review always follows
Anki's own scheduler: a card graded "again" comes back once its learning
step is due, exactly as it would in the Anki app.
"""

import hashlib
import html
import time
from dataclasses import dataclass, field
from typing import Any

from .anki import AnkiConnect
from .cards import VOICE_FIELD, Languages, is_voice_friendly, language_hint, to_payload

GRADES = ("again", "hard", "good", "easy")  # AnkiConnect's names for Anki's answer buttons
SKIP_TAG = "anki-voice::skip"  # notes tagged this are left out of every voice review
# Anki's flags by the names its Browse window gives them; "none" clears a flag.
FLAGS = {"none": 0, "red": 1, "orange": 2, "green": 3, "blue": 4, "pink": 5, "turquoise": 6, "purple": 7}

# Seconds taken off every answer time logged in Anki.
#
# A card's time runs from when it is handed to Claude until it is graded, and
# in a voice review much of that is speech the viewer can neither skip nor
# hurry: Claude reading the prompt aloud (long for maths), saying it's checking
# the answer, reading the answer back. Without this, the same card logs as
# slower by voice than in Anki's own window, where only thinking time counts.
# It is one flat estimate for every card, so long spoken prompts still log as
# slower than short ones. Overridden by ANKI_VOICE_OVERHEAD_SECONDS.
VOICE_OVERHEAD_SECONDS = 10.0

# Anki card queues (cardsInfo "queue")
LEARNING = 1  # intraday learning; "due" is a Unix timestamp
REVIEW = 2
DAY_LEARNING = 3


@dataclass
class Review:
    anki: AnkiConnect
    exclude_decks: list[str] = field(default_factory=list)
    languages: Languages = field(default_factory=dict)
    learn_ahead_minutes: int = 20
    voice_overhead_seconds: float = VOICE_OVERHEAD_SECONDS
    dry_run: bool = False
    # Cards set aside for this server run: skipped by the viewer, or
    # graded while dry_run is on.
    set_aside: set[int] = field(default_factory=set)
    seed: str = field(default_factory=lambda: str(time.time()))
    # Limits the review to one topic, e.g. "Spanish" or "Math"; None is everything.
    focus: str | None = None
    # For `anki-voice session`, which stops when the review is done or idle.
    last_activity: float = field(default_factory=time.time)
    finished_at: float | None = None
    # When each card was first handed out, so Anki records how long it took.
    shown_at: dict[int, float] = field(default_factory=dict)
    # The last grade or skip, which undo can reverse once.
    last_action: dict[str, Any] | None = None
    # A card brought back by undo, served before any other.
    front: int | None = None
    # The language the Claude app's voice is set to, as far as this review knows:
    # None is the person's usual one. Only the person can change it, so cards in
    # this language come first, and a result says when it has to change.
    voice: str | None = None

    def _scope(self) -> str:
        excluded = " ".join(f'-"deck:{deck}"' for deck in self.exclude_decks)
        return f"-is:suspended -is:buried -tag:{SKIP_TAG} {excluded}".strip()

    def _in_focus(self, card: dict[str, Any]) -> bool:
        """A topic matches part of a deck name, or a card's language."""
        if not self.focus:
            return True
        topic = self.focus.casefold()
        return topic in card["deckName"].casefold() or topic == (language_hint(card, self.languages) or "").casefold()

    def _shuffle_key(self, card_id: int) -> str:
        # Stable within one server run, so the order doesn't jump between calls.
        return hashlib.sha256(f"{self.seed}:{card_id}".encode()).hexdigest()

    async def _state(self) -> dict[str, list[dict[str, Any]]]:
        due_ids = await self.anki.find_cards(f"is:due {self._scope()}")
        learn_ids = await self.anki.find_cards(f"is:learn -is:due {self._scope()}")
        cards = await self.anki.cards_info(sorted(set(due_ids) | set(learn_ids)))
        due_set = set(due_ids)
        now = time.time()
        horizon = now + self.learn_ahead_minutes * 60

        learning_now, reviews, ahead, later, not_spoken = [], [], [], [], []
        for card in cards:
            if card["cardId"] in self.set_aside:
                continue
            if not is_voice_friendly(card):
                not_spoken.append(card)
            # Anki's is:due counts learning cards within its own learn-ahead
            # window, so a card just graded "again" is "due"; go by the time.
            elif card["queue"] == LEARNING:
                if card["due"] <= now:
                    learning_now.append(card)
                elif card["due"] <= horizon:
                    ahead.append(card)
                else:
                    later.append(card)
            elif card["cardId"] in due_set:
                reviews.append(card)

        learning_now.sort(key=lambda c: c["due"])
        reviews.sort(key=lambda c: (c["due"], self._shuffle_key(c["cardId"])))
        ahead.sort(key=lambda c: c["due"])
        later.sort(key=lambda c: c["due"])
        everything = learning_now + reviews + ahead
        # Stable sorts: the rest keep their order.
        everything.sort(key=lambda c: language_hint(c, self.languages) != self.voice)
        everything.sort(key=lambda c: c["cardId"] != self.front)
        return {
            "queue": [c for c in everything if self._in_focus(c)],
            "later": [c for c in later if self._in_focus(c)],
            "other_topics": len([c for c in everything + later if not self._in_focus(c)]),
            "not_spoken": not_spoken,
        }

    async def next(self) -> dict[str, Any]:
        self.last_activity = time.time()
        self.finished_at = None
        state = await self._state()
        queue, later = state["queue"], state["later"]
        counts = {
            "focus": self.focus,
            "remaining": len(queue),
            "learning_later_today": len(later),
            "left_out_needs_picture_or_sound": len(state["not_spoken"]),
            "due_in_other_topics": state["other_topics"],
        }
        if queue:
            self.shown_at.setdefault(queue[0]["cardId"], time.time())
            return {"status": "card", "card": to_payload(queue[0], self.languages), **self._switch_voice(language_hint(queue[0], self.languages)), **counts}
        if later:
            minutes = max(1, round((later[0]["due"] - time.time()) / 60))
            return {"status": "wait", "next_card_in_minutes": minutes, **counts}
        if not state["other_topics"]:
            self.finished_at = time.time()
        return {"status": "done", **self._switch_voice(None), **counts}

    def _switch_voice(self, language: str | None) -> dict[str, str]:
        """`switch_voice_to` when the app's voice has to change language first; nothing when it doesn't."""
        if language == self.voice:
            return {}
        self.voice = language
        return {"switch_voice_to": language or "your usual language"}

    async def grade(self, card_id: int, result: str) -> dict[str, Any]:
        if result not in GRADES:
            raise ValueError(f"result must be one of {', '.join(GRADES)}, not {result!r}")
        state = await self._state()
        if card_id not in {c["cardId"] for c in state["queue"]}:
            raise ValueError(f"Card {card_id} is not up for review now; it may already be graded.")
        shown_at = self.shown_at.pop(card_id, None)
        elapsed = time.time() - shown_at - self.voice_overhead_seconds if shown_at else 0
        elapsed_ms = max(0, round(elapsed * 1000))
        if self.dry_run:
            self.set_aside.add(card_id)
        else:
            await self.anki.answer(card_id, result, elapsed_ms)
        self._done_with(card_id, {"kind": "grade", "card_id": card_id, "result": result, "elapsed_ms": elapsed_ms})
        return {"graded": {"card_id": card_id, "result": result, "dry_run": self.dry_run}, **await self.next()}

    async def skip(self, card_id: int, always: bool = False) -> dict[str, Any]:
        self.set_aside.add(card_id)
        note = None
        if always and not self.dry_run:
            [card] = await self.anki.cards_info([card_id])
            note = card["note"]
            await self.anki.add_tag(note, SKIP_TAG)
        self._done_with(card_id, {"kind": "skip", "card_id": card_id, "always": always, "tagged_note": note})
        return {"skipped": {"card_id": card_id, "always": always}, **await self.next()}

    async def set_instructions(self, card_id: int, instructions: str) -> dict[str, Any]:
        """Replace the instructions on a card's note; they apply to all its cards from now on.

        Anki's undo would take this back before the last grade, so the last
        grade or skip can no longer be undone.
        """
        self.last_activity = time.time()
        [card] = await self.anki.cards_info([card_id])
        if VOICE_FIELD not in card["fields"]:
            raise ValueError(f'The note type "{card["modelName"]}" has no "{VOICE_FIELD}" field: run `anki-voice add-field`.')
        value = html.escape(instructions.strip(), quote=False).replace("\n", "<br>")
        if not self.dry_run:
            await self.anki.set_field(card["note"], VOICE_FIELD, value)
            self.last_action = None
        return {"instructions_set": {"card_id": card_id, "instructions": instructions.strip(), "dry_run": self.dry_run}}

    async def flag(self, card_id: int, color: str) -> dict[str, Any]:
        """Flag a card in Anki, for the person to look at later; it stays up for review.

        Anki's undo would take this back before the last grade, so the last
        grade or skip can no longer be undone.
        """
        if color not in FLAGS:
            raise ValueError(f"color must be one of {', '.join(FLAGS)}, not {color!r}")
        self.last_activity = time.time()
        if not self.dry_run:
            await self.anki.set_flag(card_id, FLAGS[color])
            self.last_action = None
        return {"flagged": {"card_id": card_id, "color": color, "dry_run": self.dry_run}}

    def _done_with(self, card_id: int, action: dict[str, Any]) -> None:
        self.last_action = action
        if self.front == card_id:
            self.front = None

    async def undo(self) -> dict[str, Any]:
        """Reverse the last grade or skip and serve that card next.

        A grade is reversed with Anki's own undo, which takes back whatever
        changed last in Anki: nothing else should change Anki during a review.
        """
        action = self.last_action
        if not action:
            raise ValueError("Nothing to undo: only the last grade or skip can be undone, and only once.")
        card_id = action["card_id"]
        if action["kind"] == "grade" and not self.dry_run:
            await self.anki.undo()
        else:
            self.set_aside.discard(card_id)
        if action.get("tagged_note"):
            await self.anki.remove_tag(action["tagged_note"], SKIP_TAG)
        self.last_action = None
        if action["kind"] == "grade":
            # A regrade logs the undone answer's time, plus the time spent correcting it.
            self.shown_at[card_id] = time.time() - action["elapsed_ms"] / 1000 - self.voice_overhead_seconds
        self.front = card_id
        undone = {k: v for k, v in action.items() if k in ("kind", "card_id", "result", "always")}
        return {"undone": undone, **await self.next()}
