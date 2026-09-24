"""Checks that uploaded bytes really are the image type they claim to be.

SVG gets stricter treatment because it is XML: it must be UTF-8, must not
declare a DOCTYPE or entities, and must have an <svg> root element.
"""

import xml.etree.ElementTree as ET

ALLOWED_CONTENT_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"}
)

_SIGNATURES = {
    "image/png": lambda d: d.startswith(b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": lambda d: d.startswith(b"\xff\xd8\xff"),
    "image/gif": lambda d: d.startswith((b"GIF87a", b"GIF89a")),
    "image/webp": lambda d: d[:4] == b"RIFF" and d[8:12] == b"WEBP",
}


class ImageContentError(ValueError):
    """The bytes are not a well-formed image of the declared type."""


def validate_image_content(content_type: str, data: bytes) -> None:
    """Raise ImageContentError unless `data` is a `content_type` image."""
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise ImageContentError(f"unsupported content type {content_type!r}")
    if content_type == "image/svg+xml":
        _validate_svg(data)
    elif not _SIGNATURES[content_type](data):
        raise ImageContentError(f"content does not match {content_type}")


def _validate_svg(data: bytes) -> None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ImageContentError("SVG must be UTF-8") from e
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise ImageContentError("SVG must not declare a DOCTYPE or entities")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ImageContentError("SVG is not well-formed XML") from e
    if root.tag.rsplit("}", 1)[-1] != "svg":
        raise ImageContentError("SVG root element must be <svg>")
