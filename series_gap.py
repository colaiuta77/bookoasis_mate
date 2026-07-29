# BookOasis 도서 제목에서 명시적인 권차를 추출해 시리즈 중간 누락을 찾습니다.
import re
from collections import defaultdict
from pathlib import PurePosixPath


_RANGE_PATTERN = re.compile(r"(?<!\d)(\d{1,4})\s*[~-]\s*(\d{1,4})\s*(?:권|화|편)", re.IGNORECASE)
_EPISODE_PATTERN = re.compile(r"(?<![A-Z0-9])E(?:P)?\.?\s*0*(\d{1,6})(?!\d)", re.IGNORECASE)
_LEADING_BRACKET_PATTERN = re.compile(r"^\s*\[\s*0*(\d{1,4})\s*\]")
_PAGE_COUNT_SUFFIX_PATTERN = re.compile(r"(?<![\d-])0*(\d{1,3})\s*#\s*\d{1,6}\s*$")
_PARENTHESIZED_RANGE_BEFORE_PAGE_PATTERN = re.compile(
    r"\(\s*0*(\d{1,3})\s*[~-]\s*0*(\d{1,3})\s*\)\s*#\s*\d{1,6}\s*$"
)
_EXPLICIT_PATTERNS = (
    re.compile(r"(?:제\s*)?(\d{1,4})\s*(?:권|화|편)", re.IGNORECASE),
    re.compile(r"\b(?:vol(?:ume)?\.?|v)\s*(\d{1,4})\b", re.IGNORECASE),
)


def _strip_book_id_suffix(title, book_id):
    text = str(title or "").strip()
    if book_id is None:
        return text
    return re.sub(rf"\s*#\s*{re.escape(str(book_id))}\s*$", "", text).strip()


def _file_name_without_extension(file_path):
    normalized = str(file_path or "").replace("\\", "/").strip()
    if not normalized:
        return ""
    name = PurePosixPath(normalized).name
    return name.rsplit(".", 1)[0] if "." in name else name


def _parse_text(text, book_id):
    original_text = str(text or "").strip()
    page_count_suffix = _PAGE_COUNT_SUFFIX_PATTERN.search(original_text)
    parenthesized_range = _PARENTHESIZED_RANGE_BEFORE_PAGE_PATTERN.search(original_text)
    text = _strip_book_id_suffix(text, book_id)
    ranges = []
    for match in _RANGE_PATTERN.finditer(text):
        start, end = int(match.group(1)), int(match.group(2))
        if 0 < start <= end and end - start <= 100:
            ranges.append(set(range(start, end + 1)))
    if len(ranges) > 1:
        return {"volumes": set(), "ambiguous": True}

    explicit_values = []
    for pattern in _EXPLICIT_PATTERNS:
        explicit_values.extend(int(match.group(1)) for match in pattern.finditer(text))
    explicit_values = {value for value in explicit_values if value > 0}

    episode_values = {int(match.group(1)) for match in _EPISODE_PATTERN.finditer(text) if int(match.group(1)) > 0}
    if len(episode_values) > 1:
        return {"volumes": set(), "ambiguous": True}
    if episode_values:
        return {"volumes": {next(iter(episode_values))}, "ambiguous": False}

    leading_bracket = _LEADING_BRACKET_PATTERN.match(text)
    if leading_bracket and int(leading_bracket.group(1)) > 0:
        return {"volumes": {int(leading_bracket.group(1))}, "ambiguous": False}

    if parenthesized_range:
        start, end = int(parenthesized_range.group(1)), int(parenthesized_range.group(2))
        if 0 < start <= end and end - start <= 100:
            return {"volumes": set(range(start, end + 1)), "ambiguous": False}

    if page_count_suffix and int(page_count_suffix.group(1)) > 0:
        return {"volumes": {int(page_count_suffix.group(1))}, "ambiguous": False}

    if len(ranges) == 1:
        return {"volumes": ranges[0], "ambiguous": False}
    values = sorted(explicit_values)
    if len(values) == 1:
        return {"volumes": {values[0]}, "ambiguous": False}
    return {"volumes": set(), "ambiguous": len(values) > 1}


def parse_volume_markers(title, book_id=None, file_path=None):
    """DB 제목을 우선 분석하고 필요한 경우 원본 파일명을 보조 입력으로 사용합니다."""
    parsed = _parse_text(title, book_id)
    if parsed["volumes"] or parsed["ambiguous"]:
        return parsed
    filename = _file_name_without_extension(file_path)
    return _parse_text(filename, book_id) if filename else parsed


def find_series_gaps(rows):
    """같은 보관함과 시리즈 안에서 최소·최대 권차 사이의 빈 번호를 찾습니다."""
    groups = defaultdict(list)
    for row in rows:
        series_name = str(row.get("series_name") or "").strip()
        if not series_name:
            continue
        groups[(row.get("library_id"), series_name)].append(row)

    results = []
    for (library_id, series_name), books in groups.items():
        present = set()
        parsed_count = 0
        ambiguous_count = 0
        for book in books:
            parsed = parse_volume_markers(book.get("title"), book.get("id"), book.get("file_path"))
            if parsed["volumes"]:
                parsed_count += 1
                present.update(parsed["volumes"])
            elif parsed["ambiguous"]:
                ambiguous_count += 1

        if len(present) < 2:
            continue
        missing = sorted(set(range(min(present), max(present) + 1)) - present)
        if not missing:
            continue
        representative = next(
            (
                book for book in books
                if str(book.get("cover_image") or "").strip() not in {"", "NO_COVER"}
            ),
            books[0],
        )
        results.append({
            "id": representative.get("id"),
            "title": str(representative.get("title") or series_name),
            "library_id": library_id,
            "library_name": str(books[0].get("library_name") or "보관함 미상"),
            "series_name": series_name,
            "cover_image": str(representative.get("cover_image") or ""),
            "present": sorted(present),
            "missing": missing,
            "book_count": len(books),
            "parsed_count": parsed_count,
            "unparsed_count": len(books) - parsed_count,
            "ambiguous_count": ambiguous_count,
            "confidence": "high" if parsed_count == len(books) and not ambiguous_count else "review",
        })

    return sorted(results, key=lambda item: (-len(item["missing"]), item["series_name"].lower()))
