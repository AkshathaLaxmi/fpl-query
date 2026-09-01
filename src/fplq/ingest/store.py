"""Immutable raw landing.

Every byte we fetch is written here before anything parses it, under a key that
records what it is and when we got it. Two properties matter:

  * Append-only. A key is never overwritten, so a bad transform is always
    recoverable by replaying from the same bytes.
  * Backend-agnostic. Local filesystem in development, S3 in deployment, same
    interface. The transform layer never learns which one it is talking to.

Keys look like:
    fpl_api/bootstrap-static/2026-09-01T09:14:22Z.json
    fpl_archive/2025-26/gws--merged_gw.csv/2026-09-01T09:14:22Z.csv
"""

from __future__ import annotations

import hashlib
import logging
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)


def _stamp(when: datetime | None = None) -> str:
    when = when or datetime.now(UTC)
    return when.strftime("%Y-%m-%dT%H-%M-%SZ")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class RawStore(ABC):
    @abstractmethod
    def put(self, key: str, data: bytes) -> str:
        """Write bytes under key. Returns the full URI written."""

    @abstractmethod
    def get(self, key: str) -> bytes: ...

    @abstractmethod
    def list(self, prefix: str) -> list[str]: ...

    def put_snapshot(self, namespace: str, name: str, data: bytes,
                     *, suffix: str = "json", when: datetime | None = None) -> str:
        """Write a timestamped snapshot and return its URI."""
        safe = name.replace("/", "--")
        return self.put(f"{namespace}/{safe}/{_stamp(when)}.{suffix}", data)

    def latest(self, namespace: str, name: str) -> str | None:
        """Key of the most recent snapshot for a name, or None."""
        safe = name.replace("/", "--")
        keys = self.list(f"{namespace}/{safe}/")
        return max(keys) if keys else None   # ISO stamps sort lexicographically


class LocalRawStore(RawStore):
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        """Resolve a key to a path, refusing anything outside the store root.

        The containment check is `is_relative_to`, not a string prefix compare.
        A prefix compare looks equivalent and is not: with a root of
        `/data/raw`, the path `/data/raw-evil/x` starts with `/data/raw` and
        would pass. Sibling directories that share a name prefix are exactly
        the case a traversal attempt lands in, and the earlier version of this
        check let one through -- the test that was supposed to catch it used
        `../../etc/evil`, which the prefix compare happens to block.

        Path components are also rejected outright rather than resolved away,
        so a key is never a way to address the filesystem even if a future
        caller builds one from source data.
        """
        if Path(key).is_absolute() or ".." in Path(key).parts:
            raise ValueError(f"key escapes the store root: {key!r}")
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError(f"key escapes the store root: {key!r}")
        return path

    def put(self, key: str, data: bytes) -> str:
        path = self._path(key)
        if path.exists():
            # Raw is append-only. A collision means two runs in the same second,
            # which is a caller bug, not something to paper over.
            raise FileExistsError(f"raw key already exists: {key}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        log.debug("raw put %s (%d bytes)", key, len(data))
        return str(path)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def list(self, prefix: str) -> list[str]:
        base = self.root / prefix
        parent = base if base.is_dir() else base.parent
        if not parent.exists():
            return []
        root = self.root.resolve()
        return sorted(
            str(p.resolve().relative_to(root))
            for p in parent.rglob("*")
            if p.is_file() and str(p.resolve().relative_to(root)).startswith(prefix)
        )


class S3RawStore(RawStore):
    """S3 backend. Imported lazily so local development needs no boto3."""

    def __init__(self, bucket: str, prefix: str = "") -> None:
        import boto3

        self._s3 = boto3.client("s3")
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def put(self, key: str, data: bytes) -> str:
        full = self._key(key)
        self._s3.put_object(Bucket=self.bucket, Key=full, Body=data,
                            IfNoneMatch="*")  # refuse to overwrite
        return f"s3://{self.bucket}/{full}"

    def get(self, key: str) -> bytes:
        return self._s3.get_object(Bucket=self.bucket, Key=self._key(key))["Body"].read()

    def list(self, prefix: str) -> list[str]:
        paginator = self._s3.get_paginator("list_objects_v2")
        out: list[str] = []
        strip = len(self.prefix) + 1 if self.prefix else 0
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self._key(prefix)):
            out.extend(o["Key"][strip:] for o in page.get("Contents", []))
        return sorted(out)


def open_store(uri: str) -> RawStore:
    """Build a store from a URI: s3://bucket/prefix, or any local path."""
    parsed = urlparse(uri)
    if parsed.scheme == "s3":
        return S3RawStore(parsed.netloc, parsed.path.lstrip("/"))
    return LocalRawStore(uri)
