"""Checks that uploaded bytes really are the image type they claim to be.

Raster types are checked by their magic-byte signature. SVG gets stricter
treatment because it is active XML that a browser may render: the bytes must
decode as UTF-8, and a single expat pass over exactly those bytes rejects any
DOCTYPE or entity declaration, rejects active content (script, foreignObject,
iframe, embed and object elements; on* event-handler attributes; href or
xlink:href values other than same-document "#" fragments or raster
data:image URLs; any attribute value containing "javascript:") and captures
the root element, which must be <svg>. Doing every check in the one parse that
also produces the accepted document means there is no second interpretation of
the bytes that a check could disagree with.
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

_FORBIDDEN_ELEMENTS = frozenset(
    {"script", "foreignobject", "iframe", "embed", "object"}
)

_ALLOWED_HREF_PREFIXES = (
    "#",
    "data:image/png",
    "data:image/jpeg",
    "data:image/gif",
    "data:image/webp",
)

_ACTIVE_CONTENT_MESSAGE = (
    "SVG must not contain scripts, event handlers or external references"
)


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
    if _local_name(root_tag) != "svg":
        raise ImageContentError("SVG root element must be <svg>")


def _local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def _is_active_attribute(name: str, value: str) -> bool:
    local = _local_name(name).lower()
    if local.startswith("on"):
        return True
    if local == "href" and not value.strip().lower().startswith(_ALLOWED_HREF_PREFIXES):
        return True
    return "javascript:" in "".join(value.split()).lower()


def _parse_svg_root(data: bytes) -> str:
    root = {"tag": None}

    def _reject_doctype_or_entity(*_args):
        raise ImageContentError("SVG must not declare a DOCTYPE or entities")

    def _check_element(name, attrs):
        if root["tag"] is None:
            root["tag"] = name
        if _local_name(name).lower() in _FORBIDDEN_ELEMENTS:
            raise ImageContentError(_ACTIVE_CONTENT_MESSAGE)
        for attr_name, attr_value in attrs.items():
            if _is_active_attribute(attr_name, attr_value):
                raise ImageContentError(_ACTIVE_CONTENT_MESSAGE)

    parser = xml.parsers.expat.ParserCreate(encoding="utf-8", namespace_separator="}")
    parser.StartDoctypeDeclHandler = _reject_doctype_or_entity
    parser.EntityDeclHandler = _reject_doctype_or_entity
    parser.StartElementHandler = _check_element

    try:
        parser.Parse(data, True)
    except xml.parsers.expat.ExpatError as e:
        raise ImageContentError("SVG is not well-formed XML") from e

    if root["tag"] is None:
        raise ImageContentError("SVG is not well-formed XML")
    return root["tag"]
