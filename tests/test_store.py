"""The raw landing zone.

Its one job is being append-only. If a key can be overwritten, "replay from raw"
stops being a recovery strategy and becomes a hope.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from fplq.ingest.store import LocalRawStore, open_store, sha256


@pytest.fixture
def store(tmp_path: Path) -> LocalRawStore:
    return LocalRawStore(tmp_path)


def test_put_and_get_round_trip(store: LocalRawStore) -> None:
    store.put("ns/name/2026-01-01T00-00-00Z.json", b'{"a": 1}')
    assert store.get("ns/name/2026-01-01T00-00-00Z.json") == b'{"a": 1}'


def test_raw_is_append_only(store: LocalRawStore) -> None:
    """Overwriting a raw key would destroy the only copy of what a source said."""
    store.put("ns/name/x.json", b"first")
    with pytest.raises(FileExistsError):
        store.put("ns/name/x.json", b"second")
    assert store.get("ns/name/x.json") == b"first"


@pytest.mark.parametrize("key", [
    "../../etc/evil",
    "/etc/passwd",
    "a/../../elsewhere/x",
    # The one that actually got through. The old containment check was a string
    # prefix compare, so with a root of `<tmp>/raw` a path under the sibling
    # `<tmp>/raw-evil` starts with the root and passed. The test above did not
    # catch it because `../../etc/evil` fails a prefix compare anyway.
    "../raw-evil/pwned.txt",
])
def test_keys_cannot_escape_the_store_root(tmp_path: Path, key: str) -> None:
    """A key is data, and data from a source is never a filesystem path."""
    (tmp_path / "raw-evil").mkdir()
    store = LocalRawStore(tmp_path / "raw")
    with pytest.raises(ValueError, match="escapes"):
        store.put(key, b"x")


def test_a_sibling_directory_sharing_a_name_prefix_is_outside(tmp_path: Path) -> None:
    """`<root>-evil` is not inside `<root>`, however similar the strings look."""
    (tmp_path / "raw-evil").mkdir()
    store = LocalRawStore(tmp_path / "raw")
    with pytest.raises(ValueError):
        store.put("../raw-evil/x", b"x")
    assert not (tmp_path / "raw-evil" / "x").exists()


def test_snapshot_key_encodes_the_nested_filename(store: LocalRawStore) -> None:
    """'gws/merged_gw.csv' must not create a directory level of its own."""
    uri = store.put_snapshot("fpl_archive/2025-26", "gws/merged_gw.csv", b"a,b\n1,2",
                             suffix="csv")
    assert "gws--merged_gw.csv" in uri


def test_latest_picks_the_most_recent_snapshot(store: LocalRawStore) -> None:
    """ISO-8601 stamps sort lexicographically, which is why the format was chosen."""
    for day in (1, 3, 2):
        store.put_snapshot("ns", "thing", f"day {day}".encode(),
                           when=datetime(2026, 1, day, tzinfo=UTC))
    latest = store.latest("ns", "thing")
    assert latest is not None
    assert store.get(latest) == b"day 3"


def test_latest_is_none_when_nothing_stored(store: LocalRawStore) -> None:
    assert store.latest("ns", "never-fetched") is None


def test_list_is_scoped_to_the_prefix(store: LocalRawStore) -> None:
    store.put_snapshot("a", "one", b"x")
    store.put_snapshot("b", "two", b"y")
    assert len(store.list("a/")) == 1


def test_open_store_returns_a_local_store_for_a_path(tmp_path: Path) -> None:
    assert isinstance(open_store(str(tmp_path)), LocalRawStore)


def test_sha256_is_stable() -> None:
    assert sha256(b"abc") == sha256(b"abc")
    assert sha256(b"abc") != sha256(b"abd")
