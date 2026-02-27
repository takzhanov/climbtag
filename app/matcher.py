import csv
import re
from pathlib import Path


class ProtocolMatcher:
    """Matches OCR-recognized bib numbers with participants from a CSV/TXT protocol."""

    def __init__(self, file_path: Path):
        self.db: dict[str, str] = {}
        self.file_path = Path(file_path)
        if self.file_path.exists():
            self._load()

    def _load(self):
        raw = self.file_path.read_bytes()
        if not raw:
            return

        text = None
        for enc in ("utf-8", "utf-8-sig", "cp1251"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            return

        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;")
            delim = dialect.delimiter
        except csv.Error:
            delim = ","

        rows = csv.DictReader(text.splitlines(), delimiter=delim)
        if rows.fieldnames is None:
            self._load_plain_text(text)
            return

        headers = {h.lower().strip(): h for h in rows.fieldnames if h}
        num_col = self._pick(headers, ["number", "номер", "num", "id", "no", "№"])
        name_col = self._pick(headers, ["name", "фио", "атлет", "athlete", "имя"])

        if not num_col or not name_col:
            self._load_plain_text(text)
            return

        for row in rows:
            raw_num = str(row.get(num_col, "")).strip().split(".")[0]
            num = raw_num.lstrip("0") or ("0" if raw_num == "0" else "")
            name = str(row.get(name_col, "")).strip()
            if num and name:
                self.db[num] = name

    def _load_plain_text(self, text: str):
        for line in text.splitlines():
            raw = line.strip()
            if not raw:
                continue

            if ";" in raw:
                left, right = raw.split(";", 1)
                raw_num = left.strip()
                name = right.strip()
            else:
                match = re.match(r"^\s*(?:#|№)?\s*(\d{1,4})[\s,\-:]+(.+?)\s*$", raw)
                if not match:
                    continue
                raw_num = match.group(1).strip()
                name = match.group(2).strip()

            if not name:
                continue
            num = raw_num.split(".")[0].lstrip("0") or ("0" if raw_num == "0" else "")
            if num:
                self.db[num] = name

    @staticmethod
    def _pick(headers: dict[str, str], aliases: list[str]) -> str | None:
        for alias in aliases:
            if alias in headers:
                return headers[alias]
        return None

    def find_participant(self, raw_ocr_text: str):
        if not raw_ocr_text:
            return None, None

        text = str(raw_ocr_text).upper().strip()
        replacements = {
            "Z": "2", "O": "0", "I": "1", "L": "1",
            "S": "5", "B": "8", "G": "6", "T": "7",
        }
        for src, dst in replacements.items():
            text = text.replace(src, dst)

        digits = "".join(ch for ch in text if ch.isdigit()).lstrip("0")
        if not digits or len(digits) > 4:
            return None, None

        name = self.db.get(digits)
        return (digits, name) if name else (None, None)
