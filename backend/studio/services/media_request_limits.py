"""Database-owned platform safety envelopes for media request text."""

from studio.services.security_boundaries import boundary_int


def media_prompt_absolute_max_chars() -> int:
    return boundary_int("security.prompt.media_max_characters")
