from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse
import asyncio
import ipaddress
import re
import socket

import aiohttp
import disnake


SAFE_RE = re.compile(r"[^A-Za-zА-Яа-я0-9._ -]+")
MAX_REDIRECTS = 10
ALLOWED_FILE_HOST_DOMAINS = {
    "cdn.discordapp.com",
    "media.discordapp.net",
    "github.com",
    "raw.githubusercontent.com",
    "githubusercontent.com",
    "drive.google.com",
    "drive.usercontent.google.com",
    "googleusercontent.com",
    "dropbox.com",
    "dl.dropboxusercontent.com",
    "mega.nz",
    "mega.io",
    "disk.yandex.ru",
    "yadi.sk",
    "cloud.mail.ru",
    "1drv.ms",
    "onedrive.live.com",
    "gofile.io",
    "pixeldrain.com",
    "transfer.sh",
    "file.io",
}
ALLOWED_FILE_HOSTS_LABEL = (
    "Google Drive, Dropbox, MEGA, Яндекс.Диск, Mail Cloud, OneDrive, GoFile, "
    "Pixeldrain, Transfer.sh, Discord CDN, GitHub"
)
FILE_HOST_ERROR = f"Ссылка должна вести на разрешенный файлообменник. Разрешены: {ALLOWED_FILE_HOSTS_LABEL}."


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
    await _validate_download_url(str(parsed.geturl()))

    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        response = await _get_with_validated_redirects(session, str(parsed.geturl()))
        async with response:
            if response.status < 200 or response.status >= 300:
                raise ValueError(f"Ссылка вернула HTTP {response.status}.")
            await _validate_download_url(str(response.url))

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


async def _get_with_validated_redirects(session: aiohttp.ClientSession, url: str) -> aiohttp.ClientResponse:
    current_url = url
    for _ in range(MAX_REDIRECTS + 1):
        await _validate_download_url(current_url)
        response = await session.get(current_url, allow_redirects=False)
        if response.status not in {301, 302, 303, 307, 308}:
            return response

        location = response.headers.get("Location")
        response.release()
        if not location:
            raise ValueError("Ссылка вернула редирект без адреса.")
        current_url = urljoin(current_url, location)
    raise ValueError("Слишком много редиректов при скачивании файла.")


async def _validate_download_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("Разрешены только HTTPS-ссылки.")
    host = parsed.hostname.rstrip(".").lower()
    if not _is_allowed_file_host(host):
        raise ValueError(FILE_HOST_ERROR)
    await _reject_private_host(host, parsed.port)


def _is_allowed_file_host(host: str) -> bool:
    return any(host == domain or host.endswith(f".{domain}") for domain in ALLOWED_FILE_HOST_DOMAINS)


async def _reject_private_host(host: str, port: int | None = None) -> None:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        addresses = await asyncio.to_thread(socket.getaddrinfo, host, port or 443, type=socket.SOCK_STREAM)
        ips = {item[4][0] for item in addresses}
    else:
        ips = {str(ip)}

    for ip_text in ips:
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError:
            raise ValueError("Не удалось проверить адрес ссылки.")
        if not ip.is_global:
            raise ValueError("Ссылка ведет на локальный или внутренний адрес.")


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
