from __future__ import annotations


def ruble_word(amount: int) -> str:
    amount = abs(int(amount))
    last_two = amount % 100
    last_one = amount % 10
    if 11 <= last_two <= 14:
        return "Рублей"
    if last_one == 1:
        return "Рубль"
    if 2 <= last_one <= 4:
        return "Рубля"
    return "Рублей"


def money(amount: int) -> str:
    return f"{amount} {ruble_word(amount)}"


def parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized in {"1", "yes", "y", "true", "да", "д", "on", "+"}


def parse_int(value: str, field: str, minimum: int | None = None) -> int:
    try:
        number = int(value.strip())
    except ValueError as exc:
        raise ValueError(f"{field}: нужно целое число.") from exc
    if minimum is not None and number < minimum:
        raise ValueError(f"{field}: минимум {minimum}.")
    return number
