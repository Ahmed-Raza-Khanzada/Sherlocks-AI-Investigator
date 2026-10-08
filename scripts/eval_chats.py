"""Print the chat test set's scorecard (see src/sherlocks/linkgraph/eval_chats.py).

    python scripts/eval_chats.py
"""

from __future__ import annotations

import json

from sherlocks.linkgraph.eval_chats import scorecard

if __name__ == "__main__":
    print(json.dumps(scorecard(), indent=2, ensure_ascii=False))
