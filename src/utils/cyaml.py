import yaml
from yaml import YAMLError

# Define what symbols are exported from this module
__all__ = ["YAMLError", "safe_load"]


class QuotedString(str):
    """A string that remembers if it was quoted in the original YAML."""

    quote_style: str | None = None
    block_style: str | None = None

    def __new__(cls, value, quote_style=None, block_style=None):
        instance = super().__new__(cls, value)
        instance.quote_style = quote_style
        instance.block_style = block_style
        return instance


class QuotePreservingLoader(yaml.CSafeLoader):
    """A YAML Loader that marks strings that were originally quoted."""

    def construct_scalar(self, node):
        # Get the scalar value
        value = super().construct_scalar(node)

        # If the node had quotes in the original YAML, mark it
        if node.style in ('"', "'"):
            # Use a custom class to remember that this string was quoted
            return QuotedString(value, quote_style=node.style)
        elif node.style == "|":
            # Handle block scalar indicator
            return QuotedString(value, block_style="|")

        return value


# Register a proper representer for QuotedString
# This is kept if we ever decide to bring back the dumper
# def represent_quoted_string(dumper, data):
#     style = data.block_style or data.quote_style
#     return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style=style)


def safe_load(stream):
    """Load YAML content safely, preserving information about quoted strings."""
    return yaml.load(stream, Loader=QuotePreservingLoader)
