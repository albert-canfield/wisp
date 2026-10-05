"""Floor plan images uploaded from the panel: kept in Home Assistant's config folder (in backups),
served under an unguessable name, as Home Assistant serves uploaded pictures.

POST /api/wisp/plan_image (admins, multipart field "file") stores a PNG, JPEG, GIF or WebP image
and answers {"url": ...}; GET /api/wisp/plan_image/<name> serves it. An image nobody's plan uses
any more is deleted when its plan is changed or removed.
"""
from __future__ import annotations

from http import HTTPStatus
import logging
from pathlib import Path
import re
import secrets

from aiohttp import web

from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

URL_PREFIX = f"/api/{DOMAIN}/plan_image/"
MAX_BYTES = 20 * 1024 * 1024  # a phone photo of a printed plan
NAME = re.compile(r"[0-9a-f]{32}\.(png|jpg|gif|webp)")
TYPES = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}


def images_dir(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(".storage", f"{DOMAIN}_plans"))


def image_kind(head: bytes) -> str | None:
    """The file type from its first bytes, whatever its name or the browser said."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def uploaded_name(url: str) -> str | None:
    """The stored file behind a plan's url, if it is one of ours."""
    if not url.startswith(URL_PREFIX):
        return None
    name = url.removeprefix(URL_PREFIX)
    return name if NAME.fullmatch(name) else None


async def async_delete_unused(hass: HomeAssistant, url: str, in_use: set[str]) -> None:
    """Deletes an uploaded image no plan uses any more."""
    if (name := uploaded_name(url)) is None or url in in_use:
        return
    path = images_dir(hass) / name
    await hass.async_add_executor_job(lambda: path.unlink(missing_ok=True))


class PlanImageUploadView(HomeAssistantView):
    url = f"/api/{DOMAIN}/plan_image"
    name = f"api:{DOMAIN}:plan_image:upload"

    async def post(self, request: web.Request) -> web.Response:
        if not request["hass_user"].is_admin:
            return self.json_message("Only administrators can upload floor plans.", HTTPStatus.FORBIDDEN)
        hass = request.app[KEY_HASS]
        try:
            reader = await request.multipart()
            part = await reader.next()
            while part is not None and part.name != "file":
                part = await reader.next()
        except (ValueError, AssertionError):
            part = None
        if part is None:
            return self.json_message("Send the image as the form field file.", HTTPStatus.BAD_REQUEST)
        data = bytearray()
        while chunk := await part.read_chunk(256 * 1024):
            data += chunk
            if len(data) > MAX_BYTES:
                return self.json_message("The image is larger than 20 MB.", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        kind = image_kind(bytes(data[:16]))
        if kind is None:
            return self.json_message("Upload a PNG, JPEG, GIF or WebP image.", HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
        name = f"{secrets.token_hex(16)}.{kind}"
        folder = images_dir(hass)

        def write() -> None:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(bytes(data))

        await hass.async_add_executor_job(write)
        _LOGGER.debug("Stored floor plan image %s (%d bytes)", name, len(data))
        return self.json({"url": f"{URL_PREFIX}{name}"})


class PlanImageServeView(HomeAssistantView):
    """Served without a login, like Home Assistant's own uploaded pictures: images go into <img>
    tags, which send no token, and the 128-bit name cannot be guessed."""

    url = f"/api/{DOMAIN}/plan_image/{{name}}"
    name = f"api:{DOMAIN}:plan_image:serve"
    requires_auth = False

    async def get(self, request: web.Request, name: str) -> web.StreamResponse:
        if not NAME.fullmatch(name):
            raise web.HTTPNotFound
        hass = request.app[KEY_HASS]
        path = images_dir(hass) / name
        if not await hass.async_add_executor_job(path.is_file):
            raise web.HTTPNotFound
        return web.FileResponse(
            path, headers={"Content-Type": TYPES[name.rsplit(".", 1)[1]], "Cache-Control": "public, max-age=31536000, immutable"}
        )
