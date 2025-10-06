import json
import os
import tempfile
import time

from litellm.contrib.batch import JsonlCheckpoint, TokenBucket


def test_jsonl_checkpoint_append_and_read():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "results.jsonl")
        cp = JsonlCheckpoint(path, id_key="id")
        assert cp.processed_ids() == set()
        cp.append({"id": "a", "val": 1})
        cp.append({"id": "b", "val": 2})
        # malformed line
        with open(path, "a", encoding="utf-8") as f:
            f.write("not-json\n")
        ids = cp.processed_ids()
        assert ids == {"a", "b"}


def test_token_bucket_single_thread_rate():
    bucket = TokenBucket(rate_per_sec=5.0, capacity=1, max_sleep_s=1.0)
    n = 5
    t0 = time.perf_counter()
    for _ in range(n):
        with bucket.acquire():
            pass
    elapsed = time.perf_counter() - t0
    # With rate=5/s for 5 tokens, elapsed should be around 1s; allow slack
    assert elapsed >= 0.7

