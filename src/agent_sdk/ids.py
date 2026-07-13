from uuid import uuid4


def new_id(prefix: str) -> str:
    if not prefix or not prefix.replace("_", "").isalnum():
        raise ValueError("prefix must be alphanumeric/underscore")
    return f"{prefix}_{uuid4().hex}"
