"""validate_image_content: declared type must match the bytes."""

import pytest

from src.services.dashboard_image_validation import (
    ALLOWED_CONTENT_TYPES,
    ImageContentError,
    validate_image_content,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
GIF = b"GIF89a" + b"\x00" * 16
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 8
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'


@pytest.mark.parametrize(
    "content_type,data",
    [
        ("image/png", PNG),
        ("image/jpeg", JPEG),
        ("image/gif", GIF),
        ("image/gif", b"GIF87a" + b"\x00" * 16),
        ("image/webp", WEBP),
        ("image/svg+xml", SVG),
        ("image/svg+xml", b'<?xml version="1.0"?>' + SVG),
        ("image/svg+xml", b'<svg width="1" height="1"/>'),
        (
            "image/svg+xml",
            b'<?xml version="1.0" encoding="UTF-8"?>' + SVG,
        ),
    ],
)
def test_valid_images_pass(content_type, data):
    validate_image_content(content_type, data)


def test_allowed_types_are_exactly_the_five():
    assert ALLOWED_CONTENT_TYPES == {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/svg+xml",
    }


@pytest.mark.parametrize(
    "content_type,data",
    [
        ("image/png", JPEG),
        ("image/jpeg", PNG),
        ("image/gif", b"GIF90a" + b"\x00" * 8),
        ("image/webp", b"RIFF\x00\x00\x00\x00WAVE"),
        ("text/html", b"<html></html>"),
        ("image/bmp", b"BM" + b"\x00" * 16),
    ],
)
def test_mismatched_or_unsupported_rejected(content_type, data):
    with pytest.raises(ImageContentError):
        validate_image_content(content_type, data)


@pytest.mark.parametrize(
    "data",
    [
        b'<!DOCTYPE svg [<!ENTITY a "x">]><svg xmlns="http://www.w3.org/2000/svg"/>',
        b'<?xml version="1.0"?><!ENTITY a "x"><svg/>',
        b"<html><body/></html>",
        b"<svg",
        b"not xml at all",
    ],
)
def test_bad_svg_rejected(data):
    with pytest.raises(ImageContentError):
        validate_image_content("image/svg+xml", data)


def test_svg_must_be_utf8():
    hidden = '<!DOCTYPE svg [<!ENTITY a "x">]><svg/>'.encode("utf-16")
    with pytest.raises(ImageContentError):
        validate_image_content("image/svg+xml", hidden)


def test_svg_utf16le_without_bom_rejected():
    payload = '<!DOCTYPE svg [<!ENTITY xxe "pwned">]><svg>&xxe;</svg>'
    data = payload.encode("utf-16-le")
    with pytest.raises(ImageContentError):
        validate_image_content("image/svg+xml", data)


def test_svg_declared_utf16_prolog_rejected():
    payload = (
        '<?xml version="1.0" encoding="UTF-16"?>'
        '<!DOCTYPE svg [<!ENTITY a "x">]><svg>&a;</svg>'
    )
    data = payload.encode("utf-16-le")
    with pytest.raises(ImageContentError):
        validate_image_content("image/svg+xml", data)


NS = b'xmlns="http://www.w3.org/2000/svg"'
XLINK = b'xmlns:xlink="http://www.w3.org/1999/xlink"'


@pytest.mark.parametrize(
    "data",
    [
        b"<svg " + NS + b"><script>alert(1)</script></svg>",
        b"<svg " + NS + b' onload="alert(1)"/>',
        b"<svg "
        + NS
        + b'><foreignObject><div xmlns="http://www.w3.org/1999/xhtml">x'
        + b"</div></foreignObject></svg>",
        b"<svg "
        + NS
        + b" "
        + XLINK
        + b'><a xlink:href="javascript:alert(1)"><rect/></a></svg>',
        b"<svg " + NS + b'><image href="https://evil.example/x.png"/></svg>',
        b"<svg " + NS + b'><a href=" JaVaScRiPt:alert(1)"/></svg>',
        b"<svg " + NS + b'><a href="&#x20;JaVaScRiPt:alert(1)"/></svg>',
        b"<svg "
        + NS
        + b'><animate attributeName="href" values="javascript:alert(1)"/></svg>',
        b"<svg " + NS + b"><SCRIPT/></svg>",
        b"<svg " + NS + b"><iframe/></svg>",
        b"<svg " + NS + b"><embed/></svg>",
        b"<svg " + NS + b"><object/></svg>",
        b"<svg " + NS + b'><rect OnClick="x"/></svg>',
        b"<svg " + NS + b'><image href="data:image/svg+xml;base64,PHN2Zy8+"/></svg>',
        b"<svg " + NS + b'><set to="java\tscript:alert(1)"/></svg>',
    ],
)
def test_svg_active_content_rejected(data):
    with pytest.raises(ImageContentError, match="scripts, event handlers"):
        validate_image_content("image/svg+xml", data)


@pytest.mark.parametrize(
    "data",
    [
        b"<svg " + NS + b'><use href="#a"/><rect id="a"/></svg>',
        b"<svg " + NS + b" " + XLINK + b'><use xlink:href=" #a"/></svg>',
        b"<svg " + NS + b'><image href="data:image/png;base64,iVBORw0KGgo="/></svg>',
        b"<svg " + NS + b'><image href="DATA:IMAGE/JPEG;base64,/9j/"/></svg>',
    ],
)
def test_svg_safe_references_pass(data):
    validate_image_content("image/svg+xml", data)
