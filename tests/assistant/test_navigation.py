"""The pages a link may open, and what a keep-ui path shows: navigation.py against the
fixture's vectors, which keep-ui's navigation.ts reads too."""
import json
import string
from urllib.parse import quote

import pytest

from src.services.assistant import navigation
from src.services.assistant.navigation import PageObjects, build, page_objects, parse
from tests.assistant import golden_stream

VECTORS = json.loads(golden_stream.FIXTURE.read_bytes())["navigation"]
NOC = "noc"
ID = golden_stream.NAV_ID


def vector_id(vector: dict) -> str:
    return ascii(f"{vector['role']} {vector['path']}")


@pytest.mark.parametrize("vector", VECTORS["vectors"], ids=vector_id)
def test_a_vector(vector):
    parsed = parse(vector["path"])
    if parsed is None:
        assert (vector["page"], vector["param"]) == (None, None)
        assert vector["accepted"] is False and vector["noc_allowed"] is None
        return
    page, param = parsed
    assert (page, param) == (vector["page"], vector["param"])
    assert build(page, param) == vector["path"]
    assert navigation.noc_allowed(page) is vector["noc_allowed"]
    assert vector["accepted"] is (vector["role"] != NOC or vector["noc_allowed"])


@pytest.mark.parametrize("vector", VECTORS["built"], ids=lambda v: ascii(v["param"]))
def test_a_preset_link_names_its_lowercased_name(vector):
    path = build(vector["page"], vector["param"])
    assert path == vector["path"]
    assert parse(path) == (vector["page"], vector["param"].lower())


@pytest.mark.parametrize("vector", VECTORS["refused"], ids=ascii)
def test_no_link_is_built_from(vector):
    with pytest.raises(ValueError):
        build(vector["page"], vector["param"])


def test_the_vectors_are_the_modules():
    assert VECTORS["pages"] == list(navigation.PAGES)
    assert VECTORS["name_chars"] == navigation.MAX_NAME_CHARS == 256
    assert VECTORS["cel_chars"] == navigation.MAX_CEL_CHARS == 2000
    # The longest values there may be.
    assert build("dashboard", "x" * 256) == "/dashboard/" + "x" * 256
    assert build("alerts_search", "x" * 2000) == "/alerts/feed?cel=" + "x" * 2000


def test_every_page_has_vectors():
    vectors = VECTORS["vectors"]
    accepted = {v["page"] for v in vectors if v["accepted"]}
    assert accepted == set(navigation.PAGES)
    refused_to_noc = {v["page"] for v in vectors if v["page"] and not v["accepted"]}
    assert refused_to_noc == {p for p in navigation.PAGES if not navigation.noc_allowed(p)}
    assert all(v["role"] == NOC for v in vectors if v["page"] and not v["accepted"])
    assert {v["page"] for v in vectors if v["role"] == NOC and v["accepted"]} == {
        p for p in navigation.PAGES if navigation.noc_allowed(p)
    }


def test_the_page_set_is_the_one_v1_links_to():
    """Workflows, providers, settings, deduplication, extraction, mapping and topology
    are not linkable; a new page needs an amendment of rules/assistant.md."""
    assert set(navigation.PAGES) == {
        "alerts_feed",
        "alerts_preset",
        "alerts_search",
        "incidents",
        "rules",
        "rule",
        "dashboard",
    }


def test_a_noc_user_gets_what_keep_ui_lets_them_open():
    """keep-ui's middleware sends a NOC user outside /alerts back to the feed."""
    for name, page in navigation.PAGES.items():
        assert navigation.noc_allowed(name) is page.path.startswith("/alerts"), name
    for unknown in ("", "workflows", "incident", "ALERTS_FEED"):
        assert navigation.noc_allowed(unknown) is False


def test_the_encoding_is_encode_uri_components():
    unreserved = set(string.ascii_letters + string.digits + "-_.!~*'()")
    for code in range(0x20, 0x7F):
        char = chr(code)
        expected = char if char in unreserved else f"%{code:02X}"
        assert quote(char, safe=navigation.SAFE) == expected, char


def test_a_page_is_one_of_the_table():
    with pytest.raises(ValueError):
        build("workflows")
    with pytest.raises(ValueError):
        build("__class__")
    assert build("alerts_feed") == build("alerts_feed", None) == "/alerts/feed"


@pytest.mark.parametrize(
    "param",
    ["a\ud800b", 7, ["payments"], b"payments", "a\u0085b", "a\x7fb"],
    ids=ascii,
)
def test_a_parameter_that_is_no_usable_text(param):
    for page in ("alerts_preset", "alerts_search", "dashboard", "rule"):
        with pytest.raises(ValueError):
            build(page, param)


@pytest.mark.parametrize("path", [None, 7, b"/alerts/feed", ["/rules"]], ids=ascii)
def test_only_text_is_a_path(path):
    assert parse(path) is None
    assert page_objects(path) == PageObjects()


def test_every_link_reads_back_the_same():
    for page, param, *_ in golden_stream.NAVIGATION:
        if page is not None:
            assert parse(build(page, param)) == (page, param)


# --- what the user's page shows ---------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/alerts/payments", PageObjects(preset_name="payments")),
        ("/alerts/Prod%20(EU)!", PageObjects(preset_name="prod (eu)!")),
        ("/alerts/prod (eu)", PageObjects(preset_name="prod (eu)")),
        ("/alerts/%C3%89quipe", PageObjects(preset_name="équipe")),
        ("/alerts/50%25%20off", PageObjects(preset_name="50% off")),
        ("/alerts/a+b", PageObjects(preset_name="a+b")),
        ("/alerts/payments?cel=x", PageObjects(preset_name="payments")),
        ("/alerts/feed", PageObjects()),
        ("/alerts/FEED", PageObjects()),
        ("/alerts/feed?cel=name%20%3D%3D%201", PageObjects()),
        ("/alerts/", PageObjects()),
        ("/alerts/a/b", PageObjects()),
        ("/alerts/%20", PageObjects()),
        ("/alerts/a%0Ab", PageObjects()),
        ("/alerts/caf%C3", PageObjects()),
        ("/alerts/" + "x" * 257, PageObjects()),
        (f"/incidents/{ID}", PageObjects(incident_id=ID)),
        (f"/incidents/{ID}/alerts", PageObjects(incident_id=ID)),
        (f"/incidents/{ID}/timeline?tab=1", PageObjects(incident_id=ID)),
        (f"/incidents/{ID.upper()}", PageObjects()),
        (f"/incidents/{ID.replace('-', '')}", PageObjects()),
        ("/incidents/not-a-uuid", PageObjects()),
        ("/incidents", PageObjects()),
        ("/incidents/", PageObjects()),
        (f"/rules?id={ID}", PageObjects(rule_id=ID)),
        (f"/rules?tab=1&id={ID}", PageObjects(rule_id=ID)),
        (f"/rules?id={ID}&id={ID}", PageObjects()),
        ("/rules?id=not-a-uuid", PageObjects()),
        ("/rules", PageObjects()),
        ("/dashboard/Ops%20Overview", PageObjects(dashboard_name="Ops Overview")),
        ("/dashboard/Ops Overview", PageObjects(dashboard_name="Ops Overview")),
        ("/dashboard/feed", PageObjects(dashboard_name="feed")),
        ("/dashboard/", PageObjects()),
        ("/workflows", PageObjects()),
        ("", PageObjects()),
        ("alerts/payments", PageObjects()),
    ],
    ids=ascii,
)
def test_what_a_page_shows(path, expected):
    assert page_objects(path) == expected
