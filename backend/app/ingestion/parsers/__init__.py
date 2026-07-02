"""Parser registry — maps parser keys and file extensions to parser instances.

All built-in parsers are auto-registered at import time.  Third-party code can
call :func:`register` to add custom parsers.

Example::

    from app.ingestion.parsers import register, get_by_key
    register(MyCustomParser())
    parser = get_by_key("my_custom_parser")
    articles = parser.parse(Path("data/inbox/article.html"))
"""
from __future__ import annotations

from app.ingestion.parsers.base import ParsedArticle, ParserBase  # noqa: F401 — re-exported

_REGISTRY: dict[str, ParserBase] = {}
_EXT_MAP: dict[str, str] = {}  # lowercase extension -> parser_key


def register(parser: ParserBase) -> None:
    """Register a parser instance, overwriting any previous entry for its key."""
    _REGISTRY[parser.PARSER_KEY] = parser
    for ext in parser.SUPPORTED_EXTENSIONS:
        _EXT_MAP[ext.lower()] = parser.PARSER_KEY


def get_by_key(parser_key: str) -> ParserBase:
    """Return the parser registered under *parser_key*.

    Raises:
        KeyError: if no parser with that key has been registered.
    """
    try:
        return _REGISTRY[parser_key]
    except KeyError:
        raise KeyError(
            f"No parser registered for key '{parser_key}'. "
            f"Available: {list(_REGISTRY)}"
        )


def get_by_extension(ext: str) -> ParserBase | None:
    """Return the parser for a file extension, or ``None`` if none is registered."""
    key = _EXT_MAP.get(ext.lower())
    return _REGISTRY[key] if key else None


def registered_keys() -> list[str]:
    """Return a sorted list of all registered parser keys."""
    return sorted(_REGISTRY)


# ---------------------------------------------------------------------------
# Auto-register built-in parsers
# ---------------------------------------------------------------------------
from app.ingestion.parsers import snacks as _snacks_mod  # noqa: E402
from app.ingestion.parsers import generic as _generic_mod  # noqa: E402

register(_snacks_mod.SnacksParser())
register(_generic_mod.GenericTextParser())
register(_generic_mod.GenericHtmlParser())
register(_generic_mod.GenericEmailParser())
