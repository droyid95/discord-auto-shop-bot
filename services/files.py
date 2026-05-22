from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse
import re

import aiohttp
import disnake


SAFE_RE = re.compile(r"[^A-Za-zА-Яа-я0-9._ -]+")


def safe_filename(name: str, fallback: str = "file") -> str:
    clean = SAFE_RE.sub("_", name).strip(" ._")
    return clean[:120] or fallback


def product_dir(base: Path, seller_id: int, seller_name: str, product_id: int, product_name: str) -> Path:
    seller_part = f"{seller_id}_{safe_filename(seller_name, 'seller')}"
    product_part = f"{product_id}_{safe_filename(product_name, 'product')}"
    path = base / seller_part / product_part
    path.mkdir(parents=True, exist_ok=True)
    return path


async def save_message_payload(base: Path, seller_id: int, seller_name: str, product_id: int, product_name: str, text: str) -> Path:
    path = unique_path(product_dir(base, seller_id, seller_name, product_id, product_name), "message.txt")
    path.write_text(text, encoding="utf-8")
    return path


async def save_attachment_payload(
    base: Path,
    seller_id: int,
    seller_name: str,
    product_id: int,
    product_name: str,
    attachment: disnake.Attachment,
    max_bytes: int,
) -> Path:
    if attachment.size > max_bytes:
        raise ValueError("Файл больше разрешенного лимита.")

    filename = safe_filename(attachment.filename, "attachment.bin")
    target = unique_path(product_dir(base, seller_id, seller_name, product_id, product_name), filename)
    await attachment.save(target)
    if target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        raise ValueError("Файл пустой.")
    if target.stat().st_size > max_bytes:
        target.unlink(missing_ok=True)
        raise ValueError("Файл больше разрешенного лимита.")
    return target


async def download_url_payload(
    base: Path,
    seller_id: int,
    seller_name: str,
    product_id: int,
    product_name: str,
    url: str,
    max_bytes: int,
) -> Path:
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() != "https":
        raise ValueError("Разрешены только HTTPS-ссылки.")
    if not parsed.netloc:
        raise ValueError("Некорректная ссылка.")

    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, allow_redirects=True) as response:
            if response.status < 200 or response.status >= 300:
                raise ValueError(f"Ссылка вернула HTTP {response.status}.")

            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > max_bytes:
                raise ValueError("Файл больше разрешенного лимита.")

            filename = _filename_from_response(url, response)
            target = unique_path(product_dir(base, seller_id, seller_name, product_id, product_name), filename)
            total = 0
            with target.open("wb") as file:
                async for chunk in response.content.iter_chunked(1024 * 256):
                    total += len(chunk)
                    if total > max_bytes:
                        file.close()
                        target.unlink(missing_ok=True)
                        raise ValueError("Файл больше разрешенного лимита.")
                    file.write(chunk)

    if target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        raise ValueError("Файл пустой.")
    return target


def _filename_from_response(url: str, response: aiohttp.ClientResponse) -> str:
    disposition = response.headers.get("Content-Disposition", "")
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disposition, flags=re.IGNORECASE)
    if match:
        return safe_filename(unquote(match.group(1)), "download.bin")

    path_name = Path(unquote(urlparse(str(response.url) or url).path)).name
    return safe_filename(path_name, "download.bin")


def unique_path(directory: Path, filename: str) -> Path:
    candidate = directory / filename
    if not candidate.exists():
        return candidate

    stem = candidate.stem or "file"
    suffix = candidate.suffix
    for index in range(2, 10_000):
        next_candidate = directory / f"{stem}_{index}{suffix}"
        if not next_candidate.exists():
            return next_candidate
    raise ValueError("Не удалось подобрать уникальное имя файла.")
