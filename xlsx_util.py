"""엑셀(.xlsx) 쓰기·읽기 — 외부 라이브러리 없이 Python 표준 라이브러리(zipfile, xml)만 사용합니다.

    write_xlsx(headers, rows, sheet="Sheet1", widths=None, choices=None) -> bytes
        choices = {열번호(0부터): ["값1", "값2", ...]} 이면 그 열에 드롭다운(데이터 유효성 검사)을 붙입니다.
    read_xlsx(data: bytes, max_rows=1000) -> [(엑셀 행 번호, [셀 문자열, ...]), ...]
        첫 번째 시트에서 내용이 있는 줄만 돌려줍니다. 서식만 있고 글자가 없는 줄은 건너뛰고,
        max_rows 는 내용이 있는 줄만 셉니다. 줄 안의 빈 칸은 "" 입니다.

셀은 모두 '텍스트'로 씁니다. 그래서 '=' 로 시작하는 값도 수식으로 실행되지 않습니다.
"""

import io
import re
import zipfile
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
      "pr": "http://schemas.openxmlformats.org/package/2006/relationships"}
MAX_PART = 20 * 1024 * 1024        # 압축을 푼 XML 한 개 최대 크기 (압축 폭탄 방지)
_BAD_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


class XlsxError(ValueError):
    """사용자에게 보여 줄 수 있는 엑셀 오류."""


def _col(n):
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell(ref, value, style=0):
    text = escape(_BAD_CHARS.sub("", "" if value is None else str(value)))
    st = f' s="{style}"' if style else ""
    return f'<c r="{ref}" t="inlineStr"{st}><is><t xml:space="preserve">{text}</t></is></c>'


def write_xlsx(headers, rows, sheet="Sheet1", widths=None, choices=None, validate_rows=1000):
    sheet = escape(re.sub(r"[\[\]:*?/\\]", "", sheet)[:31] or "Sheet1")
    last = _col(len(headers) - 1)
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           f'<worksheet xmlns="{NS["m"]}" xmlns:r="{NS["r"]}">',
           f'<dimension ref="A1:{last}{len(rows) + 1}"/>',
           '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>']
    if widths:
        out.append("<cols>" + "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths)) + "</cols>")
    out.append("<sheetData>")
    out.append('<row r="1">' + "".join(_cell(f"{_col(i)}1", h, 1) for i, h in enumerate(headers)) + "</row>")
    for r, row in enumerate(rows, 2):
        out.append(f'<row r="{r}">' + "".join(_cell(f"{_col(i)}{r}", v) for i, v in enumerate(row)) + "</row>")
    out.append("</sheetData>")
    if choices:
        end = max(validate_rows, len(rows) + 1)
        dv = []
        for i, values in sorted(choices.items()):
            formula = escape('"' + ",".join(values) + '"')
            dv.append(f'<dataValidation type="list" allowBlank="1" showErrorMessage="1" errorTitle="값 확인" '
                      f'error="목록에 있는 값만 넣을 수 있습니다." sqref="{_col(i)}2:{_col(i)}{end}"><formula1>{formula}</formula1></dataValidation>')
        out.append(f'<dataValidations count="{len(dv)}">' + "".join(dv) + "</dataValidations>")
    out.append("</worksheet>")

    files = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '</Types>',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{NS["pr"]}">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>',
        "xl/workbook.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<workbook xmlns="{NS["m"]}" xmlns:r="{NS["r"]}"><sheets><sheet name="{sheet}" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{NS["pr"]}">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '</Relationships>',
        # 스타일 0 = 기본, 1 = 머리글(굵게, 회색 배경)
        "xl/styles.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<styleSheet xmlns="{NS["m"]}">'
            '<fonts count="2"><font><sz val="11"/><name val="맑은 고딕"/></font><font><b/><sz val="11"/><name val="맑은 고딕"/></font></fonts>'
            '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
            '<fill><patternFill patternType="solid"><fgColor rgb="FFF0F0F0"/><bgColor indexed="64"/></patternFill></fill></fills>'
            '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
            '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
            '</styleSheet>',
        "xl/worksheets/sheet1.xml": "".join(out),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            z.writestr(name, text.encode("utf-8"))
    return buf.getvalue()


# ---------------------------------------------------------------- 읽기

def _part(z, name):
    try:
        info = z.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_PART:
        raise XlsxError("엑셀 파일 안의 내용이 너무 큽니다.")
    data = z.read(info)
    if b"<!DOCTYPE" in data[:2048].upper() or b"<!ENTITY" in data.upper():
        raise XlsxError("지원하지 않는 형식의 엑셀 파일입니다.")
    return ET.fromstring(data)


def _text(el):
    """<si> 또는 <is> 안의 글자를 모은다 (서식이 섞인 글자 포함, 윗주 rPh 제외)."""
    parts = []

    def walk(node):
        for child in node:
            if child.tag == f"{{{NS['m']}}}rPh":
                continue
            if child.tag == f"{{{NS['m']}}}t":
                parts.append(child.text or "")
            else:
                walk(child)

    walk(el)
    return "".join(parts)


def _col_index(ref):
    letters = re.match(r"[A-Z]+", ref or "")
    if not letters:
        return None
    n = 0
    for ch in letters.group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def read_xlsx(data, max_rows=1000):
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise XlsxError("엑셀(.xlsx) 파일이 아닙니다. 엑셀에서 '다른 이름으로 저장 → Excel 통합 문서(*.xlsx)'로 저장해 올려 주세요.")
    with z:
        try:
            wb = _part(z, "xl/workbook.xml")
            rels = _part(z, "xl/_rels/workbook.xml.rels")
        except ET.ParseError:
            raise XlsxError("엑셀 파일을 읽지 못했습니다. 파일이 손상되었을 수 있습니다.")
        if wb is None:
            raise XlsxError("엑셀(.xlsx) 파일이 아닙니다.")
        first = wb.find("m:sheets/m:sheet", NS)
        if first is None:
            raise XlsxError("엑셀 파일에 시트가 없습니다.")
        rid = first.get(f"{{{NS['r']}}}id")
        target = "worksheets/sheet1.xml"
        for rel in (rels if rels is not None else []):
            if rel.get("Id") == rid:
                target = rel.get("Target", target)
        path = target.lstrip("/") if target.startswith("/") else "xl/" + target
        path = re.sub(r"[^/]+/\.\./", "", path)
        try:
            sst = _part(z, "xl/sharedStrings.xml")
            ws = _part(z, path)
        except ET.ParseError:
            raise XlsxError("엑셀 파일을 읽지 못했습니다. 파일이 손상되었을 수 있습니다.")
        if ws is None:
            raise XlsxError("첫 번째 시트를 찾지 못했습니다.")
        strings = [_text(si) for si in sst.findall("m:si", NS)] if sst is not None else []

        rows, last_r = [], 0
        for row in ws.findall("m:sheetData/m:row", NS):
            try:
                r = int(row.get("r") or last_r + 1)
            except ValueError:
                r = last_r + 1
            last_r = r
            cells = {}
            for i, c in enumerate(row.findall("m:c", NS)):
                idx = _col_index(c.get("r"))
                idx = i if idx is None else idx
                if idx > 200:
                    continue
                t = c.get("t")
                v = c.find("m:v", NS)
                if t == "inlineStr":
                    is_ = c.find("m:is", NS)
                    val = _text(is_) if is_ is not None else ""
                elif t == "s" and v is not None:
                    try:
                        val = strings[int(v.text)]
                    except (ValueError, IndexError, TypeError):
                        val = ""
                else:
                    val = v.text if v is not None and v.text else ""
                    if t is None and re.fullmatch(r"-?\d+\.0", val):
                        val = val[:-2]
                if val.strip():               # 서식만 있는 빈 칸은 담지 않음
                    cells[idx] = val
            if not cells:
                continue                      # 내용이 없는 줄(서식만 있는 줄 포함)은 건너뜀
            rows.append((r, [cells.get(i, "") for i in range(max(cells) + 1)]))
            if len(rows) > max_rows + 1:      # +1 = 머리글 줄
                raise XlsxError(f"한 번에 {max_rows}행까지만 올릴 수 있습니다. (내용이 있는 줄 기준)")
        return rows
