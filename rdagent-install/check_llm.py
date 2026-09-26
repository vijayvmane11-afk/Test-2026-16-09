"""Verify RD-Agent's LLM configuration end-to-end.

Run from the RD-Agent directory (where your .env lives), inside the `rdagent` conda env:

    python ~/rdagent-install/check_llm.py

It goes through RD-Agent's own APIBackend (the same code path the agent uses), so a
pass here means chat, JSON mode and embeddings will work during a real run.
"""

import json
import os
import sys

from dotenv import load_dotenv

if not os.path.exists(".env"):
    sys.exit("No .env in the current directory. cd into your RD-Agent folder first.")
load_dotenv(".env")

from rdagent.oai.llm_utils import APIBackend  # noqa: E402

ok = True
backend = APIBackend()
print(f"CHAT_MODEL      = {os.getenv('CHAT_MODEL')}")
print(f"EMBEDDING_MODEL = {os.getenv('EMBEDDING_MODEL')}")

try:
    reply = backend.build_messages_and_create_chat_completion(
        user_prompt="Reply with exactly: pong", system_prompt="You are a test assistant."
    )
    print(f"[PASS] chat            -> {reply.strip()[:60]!r}")
except Exception as e:  # noqa: BLE001
    ok = False
    print(f"[FAIL] chat            -> {e}")

try:
    reply = backend.build_messages_and_create_chat_completion(
        user_prompt='Return a JSON object {"status": "ok"} and nothing else.',
        system_prompt="You only answer in JSON.",
        json_mode=True,
    )
    json.loads(reply)
    print(f"[PASS] chat JSON mode  -> {reply.strip()[:60]!r}")
except Exception as e:  # noqa: BLE001
    ok = False
    print(f"[FAIL] chat JSON mode  -> {e}")

try:
    vectors = backend.create_embedding(["hello world", "quantitative factor"])
    print(f"[PASS] embedding       -> {len(vectors)} vectors of dim {len(vectors[0])}")
except Exception as e:  # noqa: BLE001
    ok = False
    print(f"[FAIL] embedding       -> {e}")

print("\nAll LLM checks passed." if ok else "\nSome LLM checks FAILED - see messages above.")
sys.exit(0 if ok else 1)
