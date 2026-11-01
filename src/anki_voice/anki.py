"""Thin async client for the AnkiConnect HTTP API."""

import asyncio
from typing import Any

import httpx


TEMP_DECK = "anki-voice (temporary)"


class AnkiError(RuntimeError):
    def __init__(self, message: str, kind: str | None = None):
        super().__init__(message)
        self.kind = kind  # API v1's error type, e.g. "notFound"


class AnkiConnect:
    def __init__(self, url: str = "http://127.0.0.1:8765", timeout: float = 10.0):
        self.url = url
        self.timeout = timeout

    async def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, json=payload)
        except httpx.HTTPError as e:
            raise AnkiError(f"Could not reach AnkiConnect at {self.url}: is Anki running? ({e})") from e
        return response.json()

    async def call(self, action: str, **params: Any) -> Any:
        """The legacy API (version 6)."""
        body = await self._post(self.url, {"action": action, "version": 6, "params": params})
        if body.get("error"):
            raise AnkiError(f"AnkiConnect {action} failed: {body['error']}")
        return body["result"]

    async def rpc(self, method: str, **params: Any) -> Any:
        """API v1 (JSON-RPC), for what the legacy API can't do."""
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        body = await self._post(self.url.rstrip("/") + "/api/v1", payload)
        if "error" in body:
            error = body["error"]
            raise AnkiError(f"AnkiConnect {method} failed ({error['data']['type']}): {error['message']}", error["data"]["type"])
        return body["result"]

    async def find_cards(self, query: str) -> list[int]:
        return await self.call("findCards", query=query)

    async def cards_info(self, card_ids: list[int]) -> list[dict[str, Any]]:
        if not card_ids:
            return []
        return await self.call("cardsInfo", cards=card_ids)

    async def add_field_to_every_note_type(self, field: str) -> list[str]:
        """Add `field` as the last field of each note type that lacks it; returns those note types.

        Changing a note type needs a full sync, and Anki may ask in its window
        first, so this waits as long as that takes.
        """
        added = []
        for model in await self.call("modelNames"):
            if field not in await self.call("modelFieldNames", modelName=model):
                await self.call("modelFieldAdd", modelName=model, fieldName=field)
                added.append(model)
        return added

    async def set_field(self, note_id: int, field: str, value: str) -> None:
        await self.call("updateNoteFields", note={"id": note_id, "fields": {field: value}})

    async def add_tag(self, note_id: int, tag: str) -> None:
        await self.call("addTags", notes=[note_id], tags=tag)

    async def remove_tag(self, note_id: int, tag: str) -> None:
        await self.call("removeTags", notes=[note_id], tags=tag)

    async def set_flag(self, card_id: int, flag: int) -> None:
        """Set a card's flag: 1 red, 2 orange, 3 green, 4 blue, 5 pink, 6 turquoise, 7 purple, 0 none."""
        await self.rpc("cards.setFlags", ids=[str(card_id)], flag=flag)

    async def undo(self) -> str:
        """Undo the newest step on Anki's undo stack, whatever it is; returns its label."""
        operation = await self.rpc("collection.undo")
        for _ in range(50):  # API v1 runs it in the background: wait up to 5 s
            record = await self.rpc("operations.get", operationId=operation["operationId"])
            if record["state"] == "succeeded":
                return record["result"]["label"]
            if record["state"] in ("failed", "cancelled"):
                error = record["error"]
                raise AnkiError(f"AnkiConnect collection.undo failed ({error['data']['type']}): {error['message']}", error["data"]["type"])
            await asyncio.sleep(0.1)
        raise AnkiError("AnkiConnect collection.undo did not finish within 5 seconds.")

    async def answer(self, card_id: int, rating: str, elapsed_ms: int) -> None:
        """Grade a card, recording how long the answer took.

        (The legacy answerCards always records 0 ms.)
        """
        try:
            await self._answer(card_id, rating, elapsed_ms)
        except AnkiError as e:
            # API v1 hides Anki's own errors, among them "not at top of queue",
            # behind internalError; that one is the error a voice review hits.
            if e.kind != "internalError":
                raise
            await self.clear_study_queue()
            await self._answer(card_id, rating, elapsed_ms)

    async def _answer(self, card_id: int, rating: str, elapsed_ms: int, attempts: int = 3) -> None:
        # The answer must match a prediction of the card's state. With FSRS, a
        # card in (re)learning changes state every second, so a prediction that
        # crosses a second before the answer arrives is refused as stale
        # ("conflict"): predict again.
        for attempt in range(attempts):
            prediction = await self.rpc("scheduling.predict", cardId=str(card_id))
            try:
                await self.rpc(
                    "scheduling.answer",
                    cardId=str(card_id),
                    rating=rating,
                    stateToken=prediction["stateToken"],
                    elapsedMs=elapsed_ms,
                )
                return
            except AnkiError as e:
                if e.kind != "conflict" or attempt == attempts - 1:
                    raise

    async def clear_study_queue(self) -> None:
        """Let Anki accept answers in any order.

        Once Anki's window has shown a deck overview or the reviewer, Anki keeps
        a study queue and refuses to answer any card but the one at its top.
        Anki drops that queue after any change to decks, so add an empty deck
        and delete it again. (Not a deck preset: deleting one forces a full
        sync.) The Decks screen is the one view that doesn't rebuild the queue
        straight away.
        """
        await self.call("guiDeckBrowser")
        await self.call("createDeck", deck=TEMP_DECK)
        await self.call("deleteDecks", decks=[TEMP_DECK], cardsToo=True)
