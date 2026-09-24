"""Checks that uploaded bytes really are the image type they claim to be.

SVG gets stricter treatment because it is XML: it must be UTF-8, must not
declare a DOCTYPE or entities, and must have an <svg> root element. The XML
is parsed with expat forced to UTF-8 so a payload that is valid UTF-8
byte-for-byte but is actually unlabeled or falsely-labeled UTF-16 cannot
smuggle a DOCTYPE/entity past the encoding the parser actually uses.
"""

import xml.parsers.expat

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
        data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ImageContentError("SVG must be UTF-8") from e
    root_tag = _parse_svg_root(data)
    if root_tag.rsplit("}", 1)[-1] != "svg":
        raise ImageContentError("SVG root element must be <svg>")


def _parse_svg_root(data: bytes) -> str:
    root = {"tag": None}

    def _reject_doctype_or_entity(*_args):
        raise ImageContentError("SVG must not declare a DOCTYPE or entities")

    def _capture_root(name, _attrs):
        if root["tag"] is None:
            root["tag"] = name

    parser = xml.parsers.expat.ParserCreate(encoding="utf-8", namespace_separator="}")
    parser.StartDoctypeDeclHandler = _reject_doctype_or_entity
    parser.EntityDeclHandler = _reject_doctype_or_entity
    parser.StartElementHandler = _capture_root

    try:
        parser.Parse(data, True)
    except xml.parsers.expat.ExpatError as e:
        raise ImageContentError("SVG is not well-formed XML") from e

    if root["tag"] is None:
        raise ImageContentError("SVG is not well-formed XML")
    return root["tag"]
