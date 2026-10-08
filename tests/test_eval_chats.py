"""The chat test set holds its thresholds: answer first, real sources, no repeats,
corrections applied, quick replies (templates)."""

from __future__ import annotations

from sherlocks.linkgraph.eval_chats import CHATS, run_chat, score


def test_the_chat_test_set():
    for lang, messages in CHATS.items():
        chat = run_chat(messages)
        card = score(chat)
        assert card["answer_first_ok"], (lang, card)
        assert card["bad_ids"] == [], (lang, card)
        assert card["repeat_in_a_row"] == 0 and card["asked_over_twice"] == 0, (lang, card)
        assert card["asked_after_answered"] == 0, (lang, card)
        assert card["ms_max"] < 3000, (lang, card)        # templates; the model's budgets are separate
        if lang == "roman":
            assert chat["case"].incident["date"] == "2023-03-02"         # the correction held
            assert card["corrections_kept"] >= 1                        # ...and the old statement was kept
