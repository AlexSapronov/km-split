"""Vector GS1 DataMatrix PDFs for one or more 58×40 mm labels."""
from io import BytesIO
from pathlib import Path
import re
from reportlab.pdfgen.canvas import Canvas
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.graphics.barcode.ecc200datamatrix import ECC200DataMatrix
from .codes import parse


class GS1Matrix(ECC200DataMatrix):
    """ECC200 44x44 with ASCII encoding and FNC1 in first position."""
    def _encode_c40(self, value):
        data, pos = [232], 0
        while pos < len(value):
            if value[pos:pos+2].isascii() and len(value[pos:pos+2]) == 2 and value[pos:pos+2].isdigit():
                data.append(130 + int(value[pos:pos+2])); pos += 2
            else:
                char = value[pos]
                if ord(char) > 127:
                    raise ValueError('КМ должен содержать только ASCII')
                data.append(ord(char) + 1); pos += 1
        if len(data) > self.cw_data:
            raise ValueError('КМ слишком длинный для макета этикетки')
        if len(data) < self.cw_data:
            data.append(129)
        while len(data) < self.cw_data:
            value = 129 + ((149 * (len(data) + 1)) % 253) + 1
            data.append(value if value <= 254 else value - 254)
        return data


def display_pn(pn: str) -> str:
    return re.sub(
        r'^\s*(?:(?:Артикул|Модель)\s*/\s*(?:Артикул|Модель)|Артикул|Модель)'
        r'(?:\s*:\s*|\s+)(?=\S)',
        '', pn, count=1, flags=re.IGNORECASE,
    ).strip()


def _draw_label(c, full_code: str, pn: str, quantity: int, demo=False):
    code = parse(full_code)
    barcode = GS1Matrix(code.full)
    barcode.validate()
    matrix = barcode.encode()
    w, h = len(matrix[0]), len(matrix)
    dots = int((24 * mm / (72 / 203)) // max(w, h))
    if dots < 2:
        raise ValueError('КМ слишком плотный для этикетки 58×40 мм при 203 dpi')
    module = dots * 72 / 203
    size = max(w, h) * module
    x, y = 2 * mm, 40 * mm - 2 * mm - size
    c.setFillColorRGB(0, 0, 0)
    for row in range(h):
        for col in range(w):
            if matrix[row][col]:
                c.rect(x + col * module, y + row * module, module, module, fill=1, stroke=0)
    text_x, width = 29 * mm, 27 * mm
    logo = Path(__file__).resolve().parents[1] / 'static/assets/chestny-znak.png'
    if logo.exists():
        c.drawImage(str(logo), text_x, 30 * mm, width=25 * mm, height=8.53 * mm, mask='auto')
    font_path = Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
    font = 'Helvetica'
    if font_path.exists():
        if 'LabelSans' not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont('LabelSans', str(font_path)))
        font = 'LabelSans'
    pn = display_pn(pn)
    chunks, chunk = [], ''
    for char in pn:
        if pdfmetrics.stringWidth(chunk + char, font, 9) > width:
            chunks.append(chunk); chunk = ''
        chunk += char
    chunks.append(chunk)
    if len(chunks) > 4:
        raise ValueError('PN не помещается на этикетке; требуется другой макет')
    c.setFont(font, 9)
    for i, line in enumerate(chunks):
        c.drawCentredString(text_x + width / 2, (25 - i * 3.8) * mm, line)
    c.setFont(font, 10)
    c.drawCentredString(text_x + width / 2, 8 * mm, f'{quantity} шт.' if font != 'Helvetica' else f'{quantity} pcs')
    hri_size = 5.4
    while max(pdfmetrics.stringWidth(code.cis[:19], font, hri_size),
              pdfmetrics.stringWidth(code.cis[19:], font, hri_size)) > size:
        hri_size -= 0.1
    c.setFont(font, hri_size)
    c.drawCentredString(x + size / 2, y - 3 * mm, code.cis[:19])
    c.drawCentredString(x + size / 2, y - 5.8 * mm, code.cis[19:])
    if demo:
        c.setFont(font, 6)
        c.drawCentredString(29 * mm, 1.4 * mm, 'DEMO - НЕ ДЛЯ ОБОРОТА' if font != 'Helvetica' else 'DEMO - NOT FOR USE')


def render_labels(entries, demo=False) -> bytes:
    if not entries:
        raise ValueError('Нет этикеток для печати')
    buf = BytesIO()
    c = Canvas(buf, pagesize=(58 * mm, 40 * mm), pageCompression=1)
    for entry in entries:
        _draw_label(c, entry['code'], entry['pn'], entry['quantity'], demo)
        c.showPage()
    c.save()
    return buf.getvalue()


def render_label(full_code: str, pn: str, quantity: int, demo=False) -> bytes:
    return render_labels([{'code': full_code, 'pn': pn, 'quantity': quantity}], demo)
