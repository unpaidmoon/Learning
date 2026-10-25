"""Durable track checkpoints and single-response entity consistency."""

import hashlib
import json
import os
import time
from uuid import uuid4

import httpx

from learning_app.errors import AppError

BLOCK = 128 * 1024


def strong_etag(response):
    value = response.headers.get("etag", "")
    return (
        value
        if value.startswith('"')
        and value.endswith('"')
        and len(value) <= 1024
        and not any(c in value for c in "\r\n")
        else None
    )


def atomic_json(path, value):
    temporary = path.with_suffix("." + uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as out:
            json.dump(value, out, ensure_ascii=False, separators=(",", ":"))
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def download_track(media, session, track, target, cancelled, progress):
    from .media import content_range

    def check():
        if cancelled.is_set():
            raise AppError("PREPARATION_INTERRUPTED", "下载已暂停，保留检查点", 409)
        media.host.access.check(session["grant"])

    check()
    partial = target.with_suffix(target.suffix + ".partial")
    metadata = target.with_suffix(target.suffix + ".resume.json")
    offset = partial.stat().st_size if partial.exists() else 0
    owner = {
        "source_key": list(session["source_key"]),
        "connection_id": session["grant"].connection_id,
        "format_id": track.get("format_id"),
    }
    previous = None
    hasher = hashlib.sha256()
    if offset and metadata.is_file():
        try:
            if metadata.stat().st_size > 4096:
                raise ValueError("oversize")
            previous = json.loads(metadata.read_text("utf-8"))
            if not isinstance(previous, dict):
                raise ValueError("checkpoint object")
            value = previous.get("etag")
            if (
                previous.get("version") != 1
                or not (
                    value is None
                    or (
                        isinstance(value, str)
                        and value.startswith('"')
                        and value.endswith('"')
                        and len(value) <= 1024
                        and not any(c in value for c in "\r\n")
                    )
                )
                or previous["owner"] != owner
                or type(previous["committed"]) is not int
                or not 0 <= previous["committed"] <= offset
                or type(previous["total"]) is not int
                or previous["total"] < offset
            ):
                raise ValueError("scope")
            with partial.open("rb") as old:
                left = previous["committed"]
                while left:
                    if cancelled.is_set():
                        raise AppError("PREPARATION_INTERRUPTED", "检查点核对已暂停", 409)
                    block = old.read(min(BLOCK, left))
                    if not block:
                        raise ValueError("short checkpoint")
                    hasher.update(block)
                    left -= len(block)
                if hasher.hexdigest() != previous["prefix_sha256"]:
                    raise ValueError("hash")
                for block in iter(lambda: old.read(BLOCK), b""):
                    if cancelled.is_set():
                        raise AppError("PREPARATION_INTERRUPTED", "检查点核对已暂停", 409)
                    hasher.update(block)
            if previous.get("restart_required") is True:
                check()
                # A different CDN representation must not silently retarget an old business version.
                media.verify_checkpoint_source(session)
                check()
                # Only a subsequent explicit preparation retry reaches this point.
                # Keep the old bytes and their receipt together in the owned staging directory.
                retained = target.with_name(target.name + ".retained-" + uuid4().hex)
                atomic_json(retained.with_suffix(retained.suffix + ".json"), previous)
                os.replace(partial, retained.with_suffix(retained.suffix + ".partial"))
                previous = None
                offset = 0
                hasher = hashlib.sha256()
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise AppError(
                "BILI_DOWNLOAD_CHECKPOINT_CONFLICT", "本地断点归属或完整性不符，原文件保留", 409
            ) from error
    elif offset:
        with partial.open("rb") as old:
            for block in iter(lambda: old.read(BLOCK), b""):
                if cancelled.is_set():
                    raise AppError("PREPARATION_INTERRUPTED", "检查点核对已暂停", 409)
                hasher.update(block)

    def require_restart(code, message):
        receipt = previous or {
            "version": 1,
            "owner": owner,
            "committed": offset,
            "total": offset,
            "etag": None,
            "prefix_sha256": hasher.hexdigest(),
        }
        atomic_json(metadata, {**receipt, "restart_required": True})
        raise AppError(code, message, 409)

    def changed():
        require_restart(
            "BILI_DOWNLOAD_SOURCE_CHANGED",
            "远端表示已变化，原断点保留；再次点击重试将保留旧断点并从头保存",
        )

    ranged = bool(
        offset
        and previous
        and previous.get("etag")
        and previous["committed"] == offset
        and offset < previous["total"]
    )
    validator = previous["etag"] if ranged else None
    try:
        client, response = media.upstream(
            session["grant"],
            track["url"],
            f"bytes={offset}-" if ranged else None,
            if_range=validator,
            allow_full_response=True,
            expected_cid=session.get("cid"),
        )
    except AppError as error:
        if ranged and error.code == "BILI_DOWNLOAD_RANGE_REJECTED":
            require_restart(
                error.code, "平台拒绝当前断点范围，原断点保留；再次点击重试将保留旧断点并从头保存"
            )
        raise
    try:
        length = response.headers.get("content-length", "")
        expected = int(length) if length.isdecimal() else 0
        if response.status_code == 206:
            start, end, total = content_range(response)
            if start != (offset if ranged else 0) or end != total - 1:
                raise AppError(
                    "BILI_DOWNLOAD_RANGE_CONFLICT", "平台没有返回完整所需范围，原断点保留", 409
                )
            expected = end - start + 1
        else:
            total = expected
        if expected <= 0:
            raise AppError("BILI_DOWNLOAD_SIZE_UNKNOWN", "平台未提供完整字节长度，未发布副本", 409)
        etag = strong_etag(response)
        if total < offset or (
            previous
            and (
                previous["total"] != total
                or (previous.get("etag") and etag and previous["etag"] != etag)
            )
        ):
            changed()
        replay = offset if response.status_code == 200 or not ranged else 0
        prefix_verified = replay == 0
        current = {
            "version": 1,
            "owner": owner,
            "total": total,
            "etag": etag or (validator if response.status_code == 206 else None),
        }
        last_report = 0.0
        received = 0
        old = partial.open("rb") if replay else None
        try:
            with partial.open("r+b" if offset else "wb") as out:
                out.seek(offset)

                def persist():
                    out.flush()
                    os.fsync(out.fileno())
                    atomic_json(
                        metadata,
                        {**current, "committed": out.tell(), "prefix_sha256": hasher.hexdigest()},
                    )

                try:
                    if prefix_verified:
                        persist()
                    for block in response.iter_bytes(BLOCK):
                        check()
                        received += len(block)
                        if received > expected:
                            raise AppError(
                                "BILI_DOWNLOAD_INCOMPLETE",
                                "平台发送字节超出声明范围，原断点保留",
                                409,
                            )
                        if replay:
                            take = min(len(block), replay)
                            if old.read(take) != block[:take]:
                                changed()
                            replay -= take
                            block = block[take:]
                            if replay == 0:
                                prefix_verified = True
                                persist()
                        if block:
                            out.write(block)
                            hasher.update(block)
                        now = time.monotonic()
                        if prefix_verified and now - last_report >= 1:
                            persist()
                            progress(out.tell(), total)
                            last_report = now
                finally:
                    if prefix_verified:
                        persist()
        finally:
            if old:
                old.close()
        check()
        if received != expected or replay or partial.stat().st_size != total:
            raise AppError("BILI_DOWNLOAD_INCOMPLETE", "媒体下载字节不完整，保留检查点", 409)
        progress(total, total)
        check()
        os.replace(partial, target)
        return target
    except httpx.HTTPError as error:
        raise AppError(
            "BILI_DOWNLOAD_INTERRUPTED", "下载连接中断，保留检查点且未自动重试", 502
        ) from error
    finally:
        response.close()
        client.close()
