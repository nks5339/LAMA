"""iter-14.17 — In-memory log ring-buffer tail tests."""
import logging
import os
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")

import log_tail as lt  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_ring():
    """Wipe the ring before every test so records don't leak across tests."""
    with lt._LOCK:
        lt._RING.clear()
    yield


def _install_and_get_root():
    lt.install_log_tail_handler()
    return logging.getLogger()


def test_install_is_idempotent():
    lt.install_log_tail_handler()
    n_before = sum(1 for h in logging.getLogger().handlers if isinstance(h, lt.RingBufferHandler))
    lt.install_log_tail_handler()
    lt.install_log_tail_handler()
    n_after = sum(1 for h in logging.getLogger().handlers if isinstance(h, lt.RingBufferHandler))
    assert n_after == n_before, "install must be idempotent"


def test_records_captured_with_monotonic_seq():
    root = _install_and_get_root()
    logging.getLogger("lama.test").info("first")
    logging.getLogger("lama.test").warning("second")
    logging.getLogger("lama.test").error("third")
    r = lt.tail(since_seq=0)
    seqs = [rec["seq"] for rec in r["records"]]
    assert seqs == sorted(seqs) and len(seqs) >= 3
    msgs = [rec["msg"] for rec in r["records"][-3:]]
    assert msgs == ["first", "second", "third"]
    assert r["next_seq"] == seqs[-1]
    # Every fresh record starts at repeat=1.
    assert all(rec.get("repeat", 1) == 1 for rec in r["records"])


def test_consecutive_duplicates_collapse_with_repeat_counter():
    """iter-14.17.1 — spammy repeated messages (e.g. JWT_SECRET warning)
    collapse into a single row with `repeat` bumped so the log pane
    doesn't drown in duplicates."""
    _install_and_get_root()
    lg = logging.getLogger("lama.auth")
    for _ in range(5):
        lg.warning("LAMA_JWT_SECRET is not set — using dev fallback.")
    r = lt.tail(since_seq=0)
    # 5 identical warnings must produce exactly 1 ring record.
    assert len(r["records"]) == 1
    only = r["records"][0]
    assert only["repeat"] == 5
    # Non-matching message resets — different name.
    logging.getLogger("lama.factory").info("something else")
    r2 = lt.tail(since_seq=0)
    assert len(r2["records"]) == 2
    assert r2["records"][0]["repeat"] == 5
    assert r2["records"][1]["repeat"] == 1


def test_duplicate_collapse_bumps_seq_so_pollers_see_updates():
    """The collapsed row's seq must advance on every repeat so a
    connected frontend polling with `since_seq=<oldSeq>` still gets
    the updated `repeat` value."""
    _install_and_get_root()
    lg = logging.getLogger("lama.test.dup")
    lg.warning("noisy")
    first = lt.tail(since_seq=0)
    seq_after_first = first["next_seq"]
    lg.warning("noisy")
    lg.warning("noisy")
    second = lt.tail(since_seq=seq_after_first)
    # Second poll must return the collapsed record with the newer seq
    # and the bumped repeat count.
    assert len(second["records"]) == 1
    assert second["records"][0]["repeat"] == 3
    assert second["records"][0]["seq"] > seq_after_first



def test_since_seq_returns_only_new():
    _install_and_get_root()
    logging.getLogger("lama.test").info("a")
    logging.getLogger("lama.test").info("b")
    r1 = lt.tail(since_seq=0)
    seq_b = r1["next_seq"]
    logging.getLogger("lama.test").info("c")
    r2 = lt.tail(since_seq=seq_b)
    assert [rec["msg"] for rec in r2["records"]] == ["c"]


def test_level_filter():
    _install_and_get_root()
    logging.getLogger("lama.test").info("keep-quiet")
    logging.getLogger("lama.test").warning("keep-loud")
    r = lt.tail(since_seq=0, min_level="WARNING")
    msgs = [rec["msg"] for rec in r["records"]]
    assert "keep-loud" in msgs
    assert "keep-quiet" not in msgs


def test_substring_filter_matches_msg_and_name():
    _install_and_get_root()
    logging.getLogger("lama.factory").info("routed to droid")
    logging.getLogger("lama.test").info("qdrant timeout")
    r = lt.tail(since_seq=0, contains="factory")
    names = [rec["name"] for rec in r["records"]]
    assert "lama.factory" in names
    r2 = lt.tail(since_seq=0, contains="qdrant")
    msgs = [rec["msg"] for rec in r2["records"]]
    assert any("qdrant" in m.lower() for m in msgs)


def test_limit_caps_batch_size():
    _install_and_get_root()
    for i in range(50):
        logging.getLogger("lama.test").info(f"line-{i}")
    r = lt.tail(since_seq=0, limit=10)
    assert len(r["records"]) == 10
    # Second batch continues from next_seq.
    r2 = lt.tail(since_seq=r["next_seq"], limit=10)
    assert len(r2["records"]) == 10
    assert r2["records"][0]["msg"] == "line-10"


def test_ring_eviction_signals_dropped():
    _install_and_get_root()
    original = lt.CAP
    # Temporarily narrow the ring to exercise eviction cheaply.
    with lt._LOCK:
        lt._RING = type(lt._RING)(maxlen=5)
    try:
        for i in range(20):
            logging.getLogger("lama.test").info(f"e-{i}")
        r = lt.tail(since_seq=2, limit=100)
        # 20 records emitted with seqs approximately 1..20; ring holds
        # only the last 5. Dropped = oldest_held - (since_seq + 1).
        assert r["dropped"] > 0
        assert r["size"] == 5
        assert len(r["records"]) == 5
    finally:
        with lt._LOCK:
            lt._RING = type(lt._RING)(maxlen=original)


def test_long_message_is_hard_capped():
    _install_and_get_root()
    long_msg = "x" * 5000
    logging.getLogger("lama.test").info(long_msg)
    r = lt.tail(since_seq=0)
    last = r["records"][-1]["msg"]
    assert len(last) <= 2000
