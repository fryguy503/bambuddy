"""Stock LAN file metadata and upload-only transport. Never starts a print."""

import asyncio
import hashlib
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from backend.app.utils.anycubic_models import get_model

from .protocol import MAX_BYTES, AnycubicError, json_object, private_host

MAX_GCODE_BYTES = 512 * 1024 * 1024
MAX_LINE_BYTES = 256 * 1024


def basename(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 240
        or value in {".", ".."}
        or any(c in value for c in '/\\:%"')
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise AnycubicError("Invalid printer filename")
    return value


def directory(value):
    if value in {"", "/"}:
        return "/"
    if not isinstance(value, str) or len(value.encode("utf-8")) > 1024 or not value.startswith("/"):
        raise AnycubicError("Invalid printer directory")
    parts = value.strip("/").split("/")
    for part in parts:
        basename(part)
    return "/" + "/".join(parts) + "/"


def upload_url(host, reported):
    host = private_host(host)
    if (
        not isinstance(reported, str)
        or len(reported) > 2048
        or not reported.isascii()
        or any(ord(c) < 33 for c in reported)
    ):
        raise AnycubicError("The printer did not report a valid LAN upload endpoint")
    try:
        url = urlsplit(reported)
        valid = (
            url.scheme == "http"
            and url.hostname == host
            and url.port == 18910
            and url.path == "/gcode_upload"
            and url.username is None
            and url.password is None
            and not url.fragment
            and (not url.query or re.fullmatch(r"s=[A-Za-z0-9._~-]{1,512}", url.query))
        )
    except ValueError:
        valid = False
    if not valid:
        raise AnycubicError("The printer reported an unsupported LAN upload endpoint")
    return reported


def parse_catalog(data, page, limit, path):
    if not isinstance(data, dict) or not isinstance(data.get("records"), list) or len(data["records"]) > 4096:
        raise AnycubicError("The printer returned an invalid file catalog")
    page_fields = {"total", "size", "current", "pages"}
    paged = bool(page_fields.intersection(data))
    if paged:
        for key in page_fields:
            if type(data.get(key)) is not int or not 0 <= data[key] <= 1_000_000:
                raise AnycubicError("The printer returned invalid file pagination")
        if data["current"] != page or data["size"] != limit or len(data["records"]) > limit:
            raise AnycubicError("The printer returned a different file page")
        total, pages = data["total"], data["pages"]
        if pages != (total + limit - 1) // limit or total < len(data["records"]):
            raise AnycubicError("The printer returned inconsistent file pagination")
    else:
        # S1 2.7.2.7 returns the complete list (list_mode=0) without the SDK's
        # pagination fields. Page this bounded snapshot locally; don't invent
        # a server pagination acknowledgement.
        if data.get("list_mode") != 0:
            raise AnycubicError("The printer returned an unsupported file-list mode")
        total = len(data["records"])
        pages = (total + limit - 1) // limit
    files, names = [], set()
    for raw in data["records"]:
        if not isinstance(raw, dict) or type(raw.get("is_dir")) is not bool:
            raise AnycubicError("The printer returned an invalid file entry")
        name = basename(raw.get("filename"))
        if name in names:
            raise AnycubicError("The printer returned duplicate file names")
        names.add(name)
        size = raw.get("size")
        if size is not None and (type(size) is not int or not 0 <= size <= 1 << 53):
            raise AnycubicError("The printer returned an invalid file size")
        files.append({"name": name, "size": size, "is_dir": raw["is_dir"], "path": f"{path or '/'}{name}"})
    if not paged:
        files = files[(page - 1) * limit : page * limit]
    return {
        "path": path or "/",
        "files": files,
        "page": page,
        "pages": pages,
        "total": total,
        "pagination": "printer" if paged else "local",
    }


@dataclass(frozen=True, repr=False)
class FileSession:
    host: str
    model: str
    generation: int
    connection: object = field(repr=False)
    url: str = field(repr=False)


@dataclass(frozen=True)
class GcodeArtifact:
    path: Path = field(repr=False)
    filename: str
    size: int
    sha256: str
    model: str


def validate_gcode(path: Path, filename: str, model: str) -> GcodeArtifact:
    """Require a text G-code export whose slicer metadata identifies the model."""
    basename(filename)
    if not filename.lower().endswith(".gcode"):
        raise AnycubicError("Upload a sliced Anycubic .gcode file")
    size = path.stat().st_size
    if not 0 < size <= MAX_GCODE_BYTES:
        raise AnycubicError("The G-code file is empty or exceeds 512 MiB")
    digest, generated, models, commands = hashlib.sha256(), False, set(), 0
    with path.open("rb") as stream:
        while line := stream.readline(MAX_LINE_BYTES + 1):
            if len(line) > MAX_LINE_BYTES or b"\x00" in line:
                raise AnycubicError("The upload is not supported text G-code")
            digest.update(line)
            try:
                text = line.decode("utf-8-sig").strip()
            except UnicodeError:
                raise AnycubicError("The upload is not UTF-8 text G-code") from None
            if re.search(r";.*generated (?:by|with).*(?:AnycubicSlicer|OrcaSlicer)", text, re.IGNORECASE):
                generated = True
            match = re.match(r";\s*(?:printer_model|machine_model)\s*[:=]\s*(.+)", text, re.IGNORECASE)
            if match:
                selected = get_model(match[1].strip().strip('"'))
                if selected is None:
                    raise AnycubicError("The G-code identifies an unsupported printer model")
                models.add(selected.name)
            if text and not text.startswith(";"):
                if not re.match(r"(?:N\d+\s+)?(?:[GMT]\d+(?:\.\d+)?|[A-Z_][A-Z0-9_]+)(?:\s|;|$)", text, re.IGNORECASE):
                    raise AnycubicError("The file contains unsupported G-code lines")
                commands += 1
    if not generated or models != {model} or not commands:
        raise AnycubicError(
            "Export sliced G-code with slicer and printer_model metadata matching this Anycubic printer"
        )
    # A unique remote name avoids overwriting an existing print file.
    stem = filename[:-6]
    while len(stem.encode("utf-8")) > 190:
        stem = stem[:-1]
    remote = f"{stem}-{uuid.uuid4().hex[:12]}.gcode"
    return GcodeArtifact(path, remote, size, digest.hexdigest(), model)


async def upload_gcode(client, context: FileSession, artifact: GcodeArtifact, *, progress=None, cancelled=None):
    """Stream one multipart POST. Network ambiguity is never automatically retried."""
    url = upload_url(context.host, context.url)
    boundary = "BambuddyAnycubic" + uuid.uuid4().hex
    name = basename(artifact.filename)
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="filename"\r\n\r\n{name}\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="gcode"; filename="{name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()

    def check():
        if cancelled is not None and cancelled.is_set():
            raise AnycubicError("Transfer cancelled; check the printer file list before retrying")
        if not client.file_session_valid(context):
            raise AnycubicError("The printer connection changed; the transfer outcome is unknown")

    async def body():
        check()
        yield head
        sent, digest = 0, hashlib.sha256()
        with artifact.path.open("rb") as stream:
            while chunk := await asyncio.to_thread(stream.read, 65536):
                check()
                sent += len(chunk)
                if sent > artifact.size:
                    raise AnycubicError("The staged G-code changed; transfer outcome is unknown")
                digest.update(chunk)
                yield chunk
                if progress:
                    progress(sent)
        if sent != artifact.size or digest.hexdigest() != artifact.sha256:
            raise AnycubicError("The staged G-code changed; transfer outcome is unknown")
        yield tail

    check()
    try:
        async with (
            aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=120, sock_connect=5), trust_env=False, auto_decompress=False
            ) as session,
            session.post(
                url,
                data=body(),
                allow_redirects=False,
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Content-Length": str(len(head) + artifact.size + len(tail)),
                    "X-File-Length": str(artifact.size),
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
            ) as response,
        ):
            if response.status != 200:
                raise AnycubicError("The printer refused the upload; inspect its file list before retrying")
            payload = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                if len(payload) + len(chunk) > MAX_BYTES:
                    raise AnycubicError("The printer upload response exceeds its size limit")
                payload.extend(chunk)
            value = json_object(bytes(payload))
            returned = value.get("data", {}).get("gcode") if isinstance(value.get("data"), dict) else None
            if type(value.get("code")) is not int or value["code"] != 200 or basename(returned) != name:
                raise AnycubicError("The printer did not confirm the uploaded file identity")
            check()
            return name
    except (aiohttp.ClientError, OSError, TimeoutError):
        raise AnycubicError("Upload interrupted; check the printer file list before retrying") from None
