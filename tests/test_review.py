import time

import pytest

from anki_voice.anki import AnkiConnect
from anki_voice.cards import answer_part, clean, is_voice_friendly, language_hint, parse_languages, to_payload
from anki_voice.review import Review

STYLE = "<style>.card { font-family: arial; }</style>"


def card(card_id, queue, due, question="Q", answer="A", deck="Default", model="Basic"):
    return {
        "cardId": card_id,
        "queue": queue,
        "due": due,
        "question": STYLE + question,
        "answer": STYLE + question + "<hr id=answer>" + answer,
        "deckName": deck,
        "modelName": model,
        "note": card_id * 10,
    }


class FakeAnki:
    def __init__(self, cards, due_ids):
        self.cards = {c["cardId"]: c for c in cards}
        self.due_ids = set(due_ids)
        self.answers = []
        self.tags = []
        self.fields = []
        self.flags = []

    async def set_flag(self, card_id, flag):
        self.flags.append((card_id, flag))

    async def add_tag(self, note_id, tag):
        self.tags.append((note_id, tag))

    async def find_cards(self, query):
        if query.startswith("is:due"):
            return sorted(self.due_ids)
        return sorted(i for i, c in self.cards.items() if c["queue"] == 1 and i not in self.due_ids)

    async def cards_info(self, ids):
        return [self.cards[i] for i in ids]

    async def set_field(self, note_id, field, value):
        self.fields.append((note_id, field, value))

    async def remove_tag(self, note_id, tag):
        self.tags.remove((note_id, tag))

    async def answer(self, card_id, rating, elapsed_ms):
        self.answers.append((card_id, rating))
        self.elapsed_ms = elapsed_ms
        self.undo_stack = (self.cards.pop(card_id), card_id in self.due_ids)
        self.due_ids.discard(card_id)

    async def undo(self):
        (card, due), self.undo_stack = self.undo_stack, None
        self.cards[card["cardId"]] = card
        if due:
            self.due_ids.add(card["cardId"])
        return "Answer Card"


def test_clean_strips_markup_and_flags():
    text = clean(STYLE + "🇩🇪 to receive (er~)<br/>(<span class=\"attrs\">verb</span>)<!-- x -->")
    assert text == "to receive (er~)\n(verb)"


def test_answer_part_drops_the_front():
    assert answer_part(STYLE + "Q<hr id=answer>die Stadt<br>Plural: die Städte", "Q") == "die Stadt\nPlural: die Städte"
    assert answer_part("Q\nA", "Q") == "A"


def test_picture_and_sound_fronts_are_not_voice_friendly():
    assert not is_voice_friendly(card(1, 2, 0, question='<img src="bird.jpg">'))
    assert not is_voice_friendly(card(1, 2, 0, question="Bm [anki:play:q:0]"))
    assert is_voice_friendly(card(1, 2, 0, question="city, town"))


LANGUAGES = {"German": ("🇩🇪",)}


def test_payload_marks_cards_in_a_language():
    payload = to_payload(card(7, 2, 0, question="city, town", answer="die Stadt", model="German"), LANGUAGES)
    assert payload == {"card_id": 7, "deck": "Default", "language": "German", "prompt": "city, town", "answer": "die Stadt"}


def test_payload_carries_the_notes_voice_instructions():
    c = card(7, 2, 0)
    c["fields"] = {"Front": {"value": "Q", "order": 0}, "AnkiVoice instructions": {"value": "", "order": 2}}
    assert "instructions" not in to_payload(c, {})
    c["fields"]["AnkiVoice instructions"]["value"] = "Any order is fine.<br>Read &quot;x&quot; as ex."
    assert to_payload(c, {})["instructions"] == 'Any order is fine.\nRead "x" as ex.'


@pytest.mark.anyio
async def test_order_learning_then_reviews_then_learn_ahead():
    now = time.time()
    anki = FakeAnki(
        [card(1, 2, 100), card(2, 1, now - 60), card(3, 1, now + 300), card(4, 1, now + 7200)],
        due_ids=[1, 2],
    )
    review = Review(anki=anki)
    first = await review.next()
    assert first["card"]["card_id"] == 2
    assert first["remaining"] == 3
    assert first["learning_later_today"] == 1

    after = await review.grade(2, "good")
    assert anki.answers == [(2, "good")]
    assert after["card"]["card_id"] == 1
    after = await review.grade(1, "again")
    assert after["card"]["card_id"] == 3  # learn-ahead once nothing else is due
    after = await review.skip(3)
    assert after["status"] == "wait"
    assert after["next_card_in_minutes"] == 120


def test_languages_are_configured():
    assert parse_languages("German 🇩🇪 🇦🇹, Spanish") == {"German": ("🇩🇪", "🇦🇹"), "Spanish": ()}
    assert parse_languages("") == {}


@pytest.mark.parametrize("deck, model, question, expected", [
    ("Study::Spanish", "Basic", "Q", "Spanish"),
    ("Default", "Spanish vocab", "Q", "Spanish"),
    ("Default", "Basic", "🇪🇸 la escalera", "Spanish"),
    ("Default", "Basic", "🇲🇽 Q", "Spanish"),
    ("Default", "Basic", "🇫🇷 Q", None),
    ("Study::German", "Basic", "🇩🇪 Q", None),
])
def test_a_card_is_in_a_language_by_its_name_or_flag(deck, model, question, expected):
    c = card(1, 2, 0, question=question, deck=deck, model=model)
    assert language_hint(c, parse_languages("Spanish 🇪🇸 🇲🇽")) == expected


@pytest.mark.anyio
async def test_cards_come_grouped_by_language_and_say_when_the_voice_must_switch():
    anki = FakeAnki(
        [card(1, 2, 100, model="German"), card(2, 2, 101), card(3, 2, 102, deck="Study::German"), card(4, 2, 103)],
        due_ids=[1, 2, 3, 4],
    )
    review = Review(anki=anki, languages=LANGUAGES)
    results = [await review.next()]
    for _ in range(4):
        results.append(await review.grade(results[-1]["card"]["card_id"], "good"))
    assert [r.get("card", {}).get("card_id") for r in results] == [2, 4, 1, 3, None]
    assert [r.get("switch_voice_to") for r in results] == [None, None, "German", None, "your usual language"]


@pytest.mark.anyio
async def test_the_voice_switch_is_asked_for_once():
    anki = FakeAnki([card(1, 2, 100, model="German")], due_ids=[1])
    review = Review(anki=anki, focus="German", languages=LANGUAGES)
    assert (await review.next())["switch_voice_to"] == "German"
    assert "switch_voice_to" not in await review.next()


@pytest.mark.anyio
async def test_card_graded_again_waits_behind_reviews():
    # Anki's is:due includes learning cards within its learn-ahead window.
    now = time.time()
    anki = FakeAnki([card(1, 1, now + 60), card(2, 2, 100)], due_ids=[1, 2])
    first = await Review(anki=anki).next()
    assert first["card"]["card_id"] == 2


@pytest.mark.anyio
async def test_grading_a_card_that_is_not_up_refuses():
    review = Review(anki=FakeAnki([card(1, 2, 100)], due_ids=[1]))
    with pytest.raises(ValueError):
        await review.grade(99, "good")
    with pytest.raises(ValueError):
        await review.grade(1, "hard")


@pytest.mark.anyio
async def test_grade_records_the_time_since_the_card_was_shown():
    anki = FakeAnki([card(1, 2, 100), card(2, 2, 100)], due_ids=[1, 2])
    review = Review(anki=anki)
    first = (await review.next())["card"]["card_id"]
    await review.next()  # asked again: the time still runs from the first showing
    review.shown_at[first] -= review.voice_overhead_seconds + 2.5
    await review.grade(first, "good")
    assert 2_500 <= anki.elapsed_ms < 3_500  # the speech is not counted

    second = 3 - first
    await review.grade(second, "good")  # handed out by grade itself, just now
    assert anki.elapsed_ms == 0
    assert review.shown_at == {}


@pytest.mark.anyio
async def test_undo_brings_the_graded_card_back_first_and_only_once():
    anki = FakeAnki([card(1, 2, 100), card(2, 2, 100)], due_ids=[1, 2])
    review = Review(anki=anki)
    first = (await review.next())["card"]["card_id"]
    review.shown_at[first] -= review.voice_overhead_seconds + 5
    await review.grade(first, "again")

    after = await review.undo()
    assert after["undone"] == {"kind": "grade", "card_id": first, "result": "again"}
    assert after["card"]["card_id"] == first
    with pytest.raises(ValueError):
        await review.undo()

    await review.grade(first, "good")  # the regrade keeps the undone answer's time
    assert anki.answers[-1] == (first, "good")
    assert 5_000 <= anki.elapsed_ms < 6_000
    assert review.front is None


@pytest.mark.anyio
async def test_undo_of_a_permanent_skip_removes_the_tag():
    anki = FakeAnki([card(1, 2, 100), card(2, 2, 100)], due_ids=[1, 2])
    review = Review(anki=anki)
    first = (await review.next())["card"]["card_id"]
    await review.skip(first, always=True)
    after = await review.undo()
    assert anki.tags == []
    assert after["undone"] == {"kind": "skip", "card_id": first, "always": True}
    assert after["card"]["card_id"] == first


@pytest.mark.anyio
async def test_undo_in_dry_run_leaves_anki_alone():
    anki = FakeAnki([card(1, 2, 100)], due_ids=[1])
    review = Review(anki=anki, dry_run=True)
    await review.grade(1, "good")
    after = await review.undo()
    assert anki.answers == []
    assert after["card"]["card_id"] == 1


@pytest.mark.anyio
async def test_undo_waits_for_ankis_background_operation():
    from anki_voice.anki import AnkiConnect

    states = iter(["running", "succeeded"])

    class Anki(AnkiConnect):
        async def rpc(self, method, **params):
            if method == "collection.undo":
                return {"operationId": "op"}
            assert params == {"operationId": "op"}
            return {"state": next(states), "result": {"label": "Answer Card", "changed": True}}

    assert await Anki().undo() == "Answer Card"


@pytest.mark.anyio
async def test_dry_run_sets_cards_aside_without_answering():
    anki = FakeAnki([card(1, 2, 100)], due_ids=[1])
    review = Review(anki=anki, dry_run=True)
    after = await review.grade(1, "good")
    assert anki.answers == []
    assert after["status"] == "done"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_answer_clears_ankis_queue_and_retries_once():
    from anki_voice.anki import AnkiConnect, AnkiError

    calls = []

    class Anki(AnkiConnect):
        async def call(self, action, **params):
            calls.append(action)

        async def rpc(self, method, **params):
            calls.append(method)
            if method == "scheduling.predict":
                return {"stateToken": "t"}
            assert params["elapsedMs"] == 4200
            if calls.count("scheduling.answer") == 1:
                raise AnkiError("AnkiConnect scheduling.answer failed", "internalError")

    await Anki().answer(1, "good", 4200)
    assert calls == [
        "scheduling.predict", "scheduling.answer",
        "guiDeckBrowser", "createDeck", "deleteDecks",
        "scheduling.predict", "scheduling.answer",
    ]


@pytest.mark.anyio
async def test_answer_predicts_again_when_the_prediction_went_stale():
    from anki_voice.anki import AnkiConnect, AnkiError

    calls = []

    class Anki(AnkiConnect):
        def __init__(self, stale):
            super().__init__()
            self.stale = stale

        async def rpc(self, method, **params):
            calls.append(method)
            if method == "scheduling.predict":
                return {"stateToken": "t"}
            if calls.count("scheduling.answer") <= self.stale:
                raise AnkiError("Prediction/conversion token is stale.", "conflict")

    await Anki(stale=2).answer(1, "good", 0)
    assert calls == ["scheduling.predict", "scheduling.answer"] * 3
    calls.clear()
    with pytest.raises(AnkiError):
        await Anki(stale=3).answer(1, "good", 0)


@pytest.mark.anyio
async def test_focus_limits_the_review_to_one_topic():
    anki = FakeAnki(
        [
            card(1, 2, 100, deck="Study::Math"),
            card(2, 2, 100, deck="Study::German"),
            card(3, 2, 100, deck="Default", model="German"),
        ],
        due_ids=[1, 2, 3],
    )
    review = Review(anki=anki, focus="german", languages=LANGUAGES)
    first = await review.next()
    assert first["remaining"] == 2
    assert first["due_in_other_topics"] == 1
    assert first["card"]["card_id"] in (2, 3)

    await review.grade(2, "good")
    after = await review.grade(3, "good")
    assert after["status"] == "done"
    assert review.finished_at is None  # maths is still due, so the session stays up

    review.focus = None
    assert (await review.next())["card"]["card_id"] == 1


@pytest.mark.anyio
async def test_skip_always_tags_the_note_and_plain_skip_does_not():
    anki = FakeAnki([card(1, 2, 100), card(2, 2, 100)], due_ids=[1, 2])
    review = Review(anki=anki)
    await review.skip(1)
    assert anki.tags == []
    after = await review.skip(2, always=True)
    assert anki.tags == [(20, "anki-voice::skip")]
    assert after["skipped"] == {"card_id": 2, "always": True}
    assert "-tag:anki-voice::skip" in review._scope()


@pytest.mark.anyio
async def test_field_is_added_only_where_missing():
    fields = {"Basic": ["Front", "Back"], "Cloze": ["Text", "AnkiVoice instructions"]}
    added = []

    async def call(action, **params):
        if action == "modelNames":
            return list(fields)
        if action == "modelFieldNames":
            return fields[params["modelName"]]
        assert action == "modelFieldAdd"
        added.append((params["modelName"], params["fieldName"]))

    anki = AnkiConnect()
    anki.call = call
    assert await anki.add_field_to_every_note_type("AnkiVoice instructions") == ["Basic"]
    assert added == [("Basic", "AnkiVoice instructions")]


@pytest.mark.anyio
async def test_set_instructions_writes_the_field_and_ends_undo():
    c = card(1, 2, 100)
    c["fields"] = {"Front": {"value": "Q", "order": 0}, "AnkiVoice instructions": {"value": "", "order": 1}}
    no_field = card(3, 2, 100)
    no_field["fields"] = {"Front": {"value": "Q", "order": 0}}
    anki = FakeAnki([c, card(2, 2, 100), no_field], due_ids=[1, 2, 3])
    review = Review(anki=anki)
    await review.grade(2, "good")
    result = await review.set_instructions(1, " Either order.\nSay <b> as bold. ")
    assert anki.fields == [(10, "AnkiVoice instructions", "Either order.<br>Say &lt;b&gt; as bold.")]
    assert result["instructions_set"]["instructions"] == "Either order.\nSay <b> as bold."
    with pytest.raises(ValueError):
        await review.undo()  # Anki's undo would take back the instructions, not the grade
    with pytest.raises(ValueError):
        await review.set_instructions(3, "x")  # its note type has no field


@pytest.mark.anyio
async def test_flag_marks_the_card_and_ends_undo():
    anki = FakeAnki([card(1, 2, 100), card(2, 2, 100)], due_ids=[1, 2])
    review = Review(anki=anki)
    await review.grade(1, "good")
    with pytest.raises(ValueError):
        await review.flag(1, "cyan")  # Claude maps it to turquoise first
    result = await review.flag(1, "turquoise")
    assert anki.flags == [(1, 6)]
    assert result["flagged"] == {"card_id": 1, "color": "turquoise", "dry_run": False}
    with pytest.raises(ValueError):
        await review.undo()  # Anki's undo would take back the flag, not the grade


@pytest.mark.anyio
async def test_flag_in_dry_run_leaves_anki_alone():
    anki = FakeAnki([card(1, 2, 100), card(2, 2, 100)], due_ids=[1, 2])
    review = Review(anki=anki, dry_run=True)
    await review.grade(1, "good")
    await review.flag(1, "red")
    assert anki.flags == []
    assert (await review.undo())["undone"]["card_id"] == 1
