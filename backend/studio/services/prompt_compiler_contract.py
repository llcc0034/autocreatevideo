"""Version identifiers shared by every persisted prompt compiler path."""

PROMPT_COMPILER_VERSION = 11


class PromptBuildError(ValueError):
    """A configured prompt could not be compiled from authoritative inputs."""
