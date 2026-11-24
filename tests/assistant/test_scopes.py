import pytest

from src.services.assistant.bl.scopes import has_scope


@pytest.mark.parametrize(
    ("scopes", "scope", "expected"),
    [
        (None, "write:alert", True),  # unknown: the gateway decides
        (["write:alert"], "write:alert", True),
        (["read:*", "write:alert"], "write:alert", True),
        (["write:*"], "write:alert", True),
        (["read:*"], "write:alert", False),
        ([], "write:alert", False),
        (["*"], "write:alert", False),
        (["*:*"], "write:alert", False),
        (["*:alert"], "write:alert", False),
        (["write:alerts"], "write:alert", False),
        (["write"], "write:alert", False),
        # The requested scope must be exactly verb:resource to use a wildcard.
        (["write:*"], "write", False),
        (["write:*"], "write:alert:x", False),
        (["write:alert:x"], "write:alert:x", True),
    ],
)
def test_gateway_wildcard_semantics(scopes, scope, expected):
    assert has_scope(scopes, scope) is expected
