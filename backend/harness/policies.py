"""Limits shared across product and experiment entry points."""


def check_input(text: str, *, free_text: bool = False) -> None:
    limit = 200 if free_text else 2000
    if len(text) > limit:
        raise ValueError(f"Input exceeds {limit} characters")
