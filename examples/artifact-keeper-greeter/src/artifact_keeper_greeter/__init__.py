"""A tiny greeting package for the Artifact Keeper example repository."""

__version__ = "2.0.0"


def greet(name: str, *, enthusiastic: bool = False) -> str:
    """Return a friendly greeting, optionally with extra enthusiasm."""
    punctuation = "!!!" if enthusiastic else "!"
    return f"Hello, {name}{punctuation}"


def greet_many(names: list[str]) -> list[str]:
    """Return a greeting for every supplied name."""
    return [greet(name) for name in names]
