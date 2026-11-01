"""Turn AnkiConnect's rendered card HTML into text that can be read aloud."""

import html
import re
from typing import Any

FLAG = re.compile(r"[\U0001F1E6-\U0001F1FF]{2}")
NOT_SPOKEN = ("<img", "[sound:", "[anki:play")
# A field on every note type for instructions to Claude about that note's
# cards: what counts as an answer, how to read the prompt. No card template
# shows it.
VOICE_FIELD = "AnkiVoice instructions"

# Languages the Claude app's voice switches to, each with the flags that mark
# its cards, e.g. {"Spanish": ("🇪🇸",)}. Cards in none of them are read in the
# person's usual language.
Languages = dict[str, tuple[str, ...]]


def parse_languages(spec: str) -> Languages:
    """`Spanish 🇪🇸 🇲🇽, French` -> {"Spanish": ("🇪🇸", "🇲🇽"), "French": ()}."""
    return {words[0]: tuple(words[1:]) for entry in spec.split(",") if (words := entry.split())}


def clean(rendered: str) -> str:
    s = re.sub(r"<style>.*?</style>", "", rendered, flags=re.S)
    s = re.sub(r"<script.*?</script>", "", s, flags=re.S)
    s = re.sub(r"<!--.*?-->", "", s, flags=re.S)
    s = re.sub(r"<link[^>]*>", "", s)
    s = re.sub(r"<br\s*/?>|</div>|</p>|</li>|<hr[^>]*>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = FLAG.sub("", s)
    lines = [line.strip() for line in s.splitlines()]
    return "\n".join(line for line in lines if line)


def answer_part(rendered_answer: str, prompt: str) -> str:
    """The back of the card without the repeated front."""
    if m := re.search(r"<hr id=['\"]?answer['\"]?\s*/?>", rendered_answer):
        return clean(rendered_answer[m.end():])
    answer = clean(rendered_answer)
    if answer.startswith(prompt):
        answer = answer[len(prompt):].strip()
    return answer


def is_voice_friendly(card: dict[str, Any]) -> bool:
    """Cards whose front needs a picture or a sound can't be asked aloud."""
    question = re.sub(r"<style>.*?</style>", "", card["question"], flags=re.S)
    return not any(marker in question for marker in NOT_SPOKEN)


def language_hint(card: dict[str, Any], languages: Languages) -> str | None:
    """The first language whose name is in the card's deck or note type name, or whose flag is on the card."""
    names = f'{card["deckName"]} {card["modelName"]}'.casefold()
    text = card["question"] + card["answer"]
    for language, flags in languages.items():
        if language.casefold() in names or any(flag in text for flag in flags):
            return language
    return None


def to_payload(card: dict[str, Any], languages: Languages) -> dict[str, Any]:
    prompt = clean(card["question"])
    payload = {
        "card_id": card["cardId"],
        "deck": card["deckName"],
        "language": language_hint(card, languages),
        "prompt": prompt,
        "answer": answer_part(card["answer"], prompt),
    }
    if instructions := clean(card.get("fields", {}).get(VOICE_FIELD, {}).get("value", "")):
        payload["instructions"] = instructions
    return payload
