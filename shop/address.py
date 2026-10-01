# shop/address.py
import re


def split_street_num(line: str) -> tuple[str, str]:
    """
    Split 'ул. Обориште 70', 'ul Oborishte №70', 'бул. Витоша 12А', 'ул Пирин 5/7'
    -> ('ул. Обориште', '70'), etc. Only used as a fallback for legacy orders that do not
    have structured street/number fields.
    """
    if not line:
        return "", ""
    s = str(line).strip()

    m = re.search(r"(?:№\s*)(\d+[A-Za-zА-Яа-я\-\/]*)\s*$", s)
    if not m:
        m = re.search(r"\s(\d+[A-Za-zА-Яа-я\-\/]*)\s*$", s)

    if m:
        num = m.group(1)
        street = s[: m.start(1)].rstrip(" ,№")
        return street.strip(), num.strip()

    return s, ""


_STREET_PREFIX = re.compile(r"^\s*(?:улица|ulitsa|ул|ul)(?:\.\s*|\s+)", re.IGNORECASE)


def street_variants(street: str) -> list[str]:
    """
    Spellings of a street to try against Econt, most literal first. Econt's validator is picky about street-type
    prefixes: [demo] "ул. Витоша" (София) is rejected as ambiguous while "Витоша" is accepted; "ул. Княз Александър"
    (Пловдив) is rejected as unknown while "Княз Александър" is accepted; other streets accept both.
    """
    s = " ".join((street or "").split())
    out = [s] if s else []
    bare = _STREET_PREFIX.sub("", s).strip()
    if bare and bare not in out:
        out.append(bare)
    return out
