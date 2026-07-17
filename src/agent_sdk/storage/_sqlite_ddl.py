"""Private SQLite lexical comparison for trusted DDL validation."""

_SQLITE_ASCII_WHITESPACE = frozenset("\t\n\f\r ")
_SQLITE_TWO_CHARACTER_OPERATORS = frozenset(
    {"||", "->", "<<", ">>", "<=", ">=", "==", "!=", "<>"}
)


def _sqlite_identifier_start(character: str) -> bool:
    return (
        character == "_"
        or "A" <= character <= "Z"
        or "a" <= character <= "z"
        or ord(character) >= 0x80
    )


def _sqlite_identifier_continue(character: str) -> bool:
    return (
        _sqlite_identifier_start(character)
        or "0" <= character <= "9"
        or character == "$"
    )


def _sqlite_ascii_digit(character: str) -> bool:
    return "0" <= character <= "9"


def _sqlite_ascii_casefold(value: str) -> str:
    return "".join(
        chr(ord(character) + 32) if "A" <= character <= "Z" else character
        for character in value
    )


def _quoted_sql_token(value: str, index: int) -> tuple[str, int]:
    opening = value[index]
    closing = "]" if opening == "[" else opening
    start = index
    index += 1
    while index < len(value):
        if value[index] != closing:
            index += 1
            continue
        if opening != "[" and index + 1 < len(value) and value[index + 1] == closing:
            index += 2
            continue
        index += 1
        return f"quoted:{value[start:index]}", index
    raise ValueError("malformed SQLite SQL")


def _sqlite_digit_sequence(
    value: str,
    index: int,
    *,
    hexadecimal: bool,
) -> int:
    digits = "0123456789abcdefABCDEF" if hexadecimal else "0123456789"
    if index >= len(value) or value[index] not in digits:
        raise ValueError("malformed SQLite SQL")
    index += 1
    while index < len(value):
        if value[index] in digits:
            index += 1
            continue
        if value[index] == "_":
            if index + 1 >= len(value) or value[index + 1] not in digits:
                raise ValueError("malformed SQLite SQL")
            index += 2
            continue
        break
    return index


def _numeric_sql_token(value: str, index: int) -> tuple[str, int]:
    start = index
    if value[index] == ".":
        index = _sqlite_digit_sequence(value, index + 1, hexadecimal=False)
    else:
        if value.startswith(("0x", "0X"), index):
            index = _sqlite_digit_sequence(value, index + 2, hexadecimal=True)
            if index < len(value) and _sqlite_identifier_continue(value[index]):
                raise ValueError("malformed SQLite SQL")
            return f"number:{_sqlite_ascii_casefold(value[start:index])}", index
        index = _sqlite_digit_sequence(value, index, hexadecimal=False)
        if index < len(value) and value[index] == ".":
            index += 1
            if index < len(value) and _sqlite_ascii_digit(value[index]):
                index = _sqlite_digit_sequence(value, index, hexadecimal=False)
    if index < len(value) and value[index] in {"e", "E"}:
        index += 1
        if index < len(value) and value[index] in {"+", "-"}:
            index += 1
        index = _sqlite_digit_sequence(value, index, hexadecimal=False)
    if index < len(value) and _sqlite_identifier_continue(value[index]):
        raise ValueError("malformed SQLite SQL")
    return f"number:{_sqlite_ascii_casefold(value[start:index])}", index


def _parameter_sql_token(value: str, index: int) -> tuple[str, int]:
    start = index
    prefix = value[index]
    index += 1
    if prefix == "?":
        while index < len(value) and _sqlite_ascii_digit(value[index]):
            index += 1
        return f"parameter:{value[start:index]}", index
    name_start = index
    while index < len(value) and _sqlite_identifier_continue(value[index]):
        index += 1
    if index == name_start:
        raise ValueError("malformed SQLite SQL")
    return f"parameter:{value[start:index]}", index


def _normalized_sql(value: str) -> str:
    tokens: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character in _SQLITE_ASCII_WHITESPACE:
            index += 1
            continue
        if value.startswith("--", index):
            newline = value.find("\n", index + 2)
            index = len(value) if newline < 0 else newline + 1
            continue
        if value.startswith("/*", index):
            closing = value.find("*/", index + 2)
            if closing < 0:
                raise ValueError("malformed SQLite SQL")
            index = closing + 2
            continue
        if character in {"x", "X"} and index + 1 < len(value) and value[index + 1] == "'":
            closing = value.find("'", index + 2)
            if closing < 0:
                raise ValueError("malformed SQLite SQL")
            hexadecimal = value[index + 2 : closing]
            if len(hexadecimal) % 2 or any(
                digit not in "0123456789abcdefABCDEF" for digit in hexadecimal
            ):
                raise ValueError("malformed SQLite SQL")
            tokens.append(f"blob:x{value[index + 1:closing + 1]}")
            index = closing + 1
            continue
        if character in {"'", '"', "`", "["}:
            token, index = _quoted_sql_token(value, index)
            tokens.append(token)
            continue
        if _sqlite_ascii_digit(character) or (
            character == "."
            and index + 1 < len(value)
            and _sqlite_ascii_digit(value[index + 1])
        ):
            token, index = _numeric_sql_token(value, index)
            tokens.append(token)
            continue
        if character in {"?", ":", "@", "$"}:
            token, index = _parameter_sql_token(value, index)
            tokens.append(token)
            continue
        if _sqlite_identifier_start(character):
            start = index
            index += 1
            while index < len(value) and _sqlite_identifier_continue(value[index]):
                index += 1
            tokens.append(f"word:{_sqlite_ascii_casefold(value[start:index])}")
            continue
        three_characters = value[index : index + 3]
        two_characters = value[index : index + 2]
        if three_characters == "->>":
            tokens.append("operator:->>")
            index += 3
            continue
        if two_characters in _SQLITE_TWO_CHARACTER_OPERATORS:
            tokens.append(f"operator:{two_characters}")
            index += 2
            continue
        if character in {"+", "-", "*", "/", "%", "<", ">", "=", "~", "&", "|"}:
            tokens.append(f"operator:{character}")
            index += 1
            continue
        if character in {"(", ")", ",", ".", ";"}:
            tokens.append(f"punctuation:{character}")
            index += 1
            continue
        raise ValueError("malformed SQLite SQL")
    return "\x1f".join(tokens)


def _sql_shapes_equal(actual: str, expected: str) -> bool:
    try:
        return _normalized_sql(actual) == _normalized_sql(expected)
    except ValueError:
        return False
