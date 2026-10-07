"""NTU format validation; does not claim to verify enrollment or ownership.

Rules: https://www.aca.ntu.edu.tw/w/aca/UAADService_21070518321568773
Department codes: bundled snapshot of the official NTU course catalog.
"""
import json
import re
from pathlib import Path

from .service import RuleError

PREFIXES = "BRD"
CATALOG = json.loads((Path(__file__).parent / "data/ntu_departments.json").read_text())
STUDENT_ID_PATTERN = rf"[{PREFIXES}brd][0-9]{{2}}[1-9ABCEHIJKZabcehijkz][0-9][0-9A-Za-z][0-9]{{3}}"


def validate_student_id(value):
    # ASCII only. Uppercasing Unicode first could turn an invalid character into ASCII.
    value = value.strip()
    if not re.fullmatch(STUDENT_ID_PATTERN, value, flags=re.ASCII):
        raise RuleError("請輸入 B、R 或 D 開頭的臺大 9 碼一般生學號，例如 B15901001；勿使用全形字元或空格。")
    value = value.upper()
    if value[3:6] not in CATALOG["departments"]:
        raise RuleError("學號的學院／系所代碼不在臺大官方代碼表中，請確認學號或聯絡管理員。")
    if (value[0] == "B" and value[4] not in "01") or (value[0] in "RD" and value[4] in "01"):
        raise RuleError("學號的學制與系所代碼不一致，請確認學號。")
    return value
