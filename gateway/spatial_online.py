"""Read-only fixed-origin metadata and bounded downloads; never calls generation APIs."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
import uuid

from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar

ORIGIN = "http://10.100.16.56:8189"
MAX_BYTES = 200 * 1024 * 1024
METADATA_BYTES = 64 * 1024


def validate_audio_url(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 \
            or any(character in value for character in ("%", "\\", "?", "#")) \
            or any(ord(character) <= 32 or ord(character) >= 127 for character in value):
        raise ValueError("仅接受指定线上服务的未编码/audio/文件地址。")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme != "http" or parsed.netloc != "10.100.16.56:8189":
            raise ValueError("音频地址必须来自指定线上服务。")
    elif not value.startswith("/audio/"):
        raise ValueError("音频地址须位于/audio/下。")
    segments = parsed.path.split("/")
    if len(segments) < 3 or segments[:2] != ["", "audio"] or any(
            segment in ("", ".", "..") or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,179}", segment)
            for segment in segments[2:]):
        raise ValueError("音频文件路径无效。")
    return ORIGIN + parsed.path


def session():
    return ClientSession(timeout=ClientTimeout(total=120, connect=10, sock_read=20),
                         trust_env=False, cookie_jar=DummyCookieJar(), auto_decompress=False)


async def bounded_json(client, path):
    async with client.get(ORIGIN + path, allow_redirects=False) as response:
        if response.status != 200 or response.headers.get("Content-Encoding", "identity") != "identity":
            raise ValueError("线上状态不可用；未跟随重定向。")
        data = bytearray()
        async for chunk in response.content.iter_chunked(8192):
            data.extend(chunk)
            if len(data) > METADATA_BYTES:
                raise ValueError("线上状态响应超过大小限制。")
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError("线上状态格式无效。")
        return value


async def online_metadata():
    try:
        async with asyncio.timeout(12), session() as client:
            models, health = await asyncio.gather(bounded_json(client, "/api/generate/models"),
                                                  bounded_json(client, "/health"))
        safe_models = []
        items = models.get("models", [])
        if not isinstance(items, list):
            raise ValueError("线上模型目录格式无效。")
        for model in items[:32]:
            if not isinstance(model, dict) or not isinstance(model.get("name"), str) \
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/ -]{0,159}", model["name"]):
                continue
            item = {"name": model["name"]}
            for field in ("is_active", "is_preloaded"):
                if type(model.get(field)) is bool:
                    item[field] = model[field]
            safe_models.append(item)
        safe_health = {key: health[key] for key in ("status", "service")
                       if isinstance(health.get(key), str)
                       and re.fullmatch(r"[A-Za-z0-9._ -]{1,80}", health[key])}
        return {"origin": ORIGIN, "reachable": True, "models": safe_models, "health": safe_health,
                "meaning": "线上目录与健康元数据；不证明模型生成或质量验收通过。"}
    except (ClientError, TimeoutError, ValueError, UnicodeError):
        return {"origin": ORIGIN, "reachable": False, "models": [], "health": {},
                "reason": "线上状态不可用、超时或响应无效；未调用生成接口。"}


async def download_audio(audio_url, downloads, max_bytes=MAX_BYTES):
    url = validate_audio_url(audio_url)
    if downloads is None:
        raise ValueError("尚未配置线上音频下载目录。")
    downloads = Path(downloads)
    downloads.mkdir(parents=True, exist_ok=True)
    filename = uuid.uuid4().hex + "_" + urlsplit(url).path.rsplit("/", 1)[1]
    target = downloads / filename
    digest, count, complete, created = hashlib.sha256(), 0, False, False
    try:
        async with asyncio.timeout(120), session() as client:
            async with client.get(url, allow_redirects=False) as response:
                if response.status != 200:
                    raise ValueError("线上下载失败；不接受重定向或非成功响应。")
                if response.headers.get("Content-Encoding", "identity") != "identity":
                    raise ValueError("线上音频须为未压缩传输。")
                declared = response.headers.get("Content-Length")
                if declared is not None and (not declared.isdecimal() or int(declared) > max_bytes):
                    raise ValueError("线上音频超过200MiB或长度无效。")
                with target.open("xb") as handle:
                    created = True
                    async for chunk in response.content.iter_chunked(65536):
                        count += len(chunk)
                        if count > max_bytes:
                            raise ValueError("线上音频超过200MiB；未导入。")
                        handle.write(chunk)
                        digest.update(chunk)
                if not count or declared is not None and count != int(declared):
                    raise ValueError("线上音频为空或下载不完整。")
                complete = True
        source = {"origin": ORIGIN, "url": url, "filename": filename, "sha256": digest.hexdigest()}
        receipt = {"schema": "spatial-online-download-v1", **source, "bytes": count,
                   "downloadedAt": datetime.now(timezone.utc).isoformat(),
                   "meaning": "Downloaded original retained; URL provenance only, model identity not inferred."}
        with target.with_name(filename + ".source.json").open("x", encoding="utf-8") as handle:
            json.dump(receipt, handle, ensure_ascii=False, indent=2, allow_nan=False)
        return target, source
    except (ClientError, TimeoutError) as exc:
        raise ValueError("线上音频下载失败或超时；未导入本地工程。") from exc
    finally:
        if created and not complete:
            target.unlink(missing_ok=True)
