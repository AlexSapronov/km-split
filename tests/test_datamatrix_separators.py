"""Regressions for GS1 DataMatrix separators in 93 and 91/92 templates."""
import pytest

from km_split.codes import GS, parse, with_quantity
from km_split.labels import GS1Matrix, render_label


SERIAL_PREFIX = '0104640638345218215DEMO000000000000000'
TAILS = (
    '93dGVz',
    '91EE12' + GS + '92' + 'A' * 43 + '=',
)


@pytest.mark.parametrize('tail', TAILS, ids=['template-37-ai93', 'template-36-ai91-92'])
@pytest.mark.parametrize('quantity', [None, 354], ids=['unquantified', 'ai30'])
def test_literal_gs_separators_are_not_encoded_as_fnc1(tail, quantity):
    source = SERIAL_PREFIX + GS + tail
    full = with_quantity(source, quantity) if quantity is not None else source

    barcode = GS1Matrix(full)
    barcode.validate()
    codewords = barcode._encode_c40(barcode.validated)

    # 129 is the first padding codeword, so inspect only the data region.
    # A normal ASCII GS (0x1D) is codeword 30, while FNC1 is codeword 232.
    payload = codewords[:codewords.index(129)]
    assert payload[0] == 232  # GS1 symbology start
    assert payload.count(232) == 1
    assert payload.count(30) == full.count(GS)
    assert full.count(GS) == (2 if '91' in tail else 1) + (quantity is not None)


def test_pdf_uses_normalized_scan_value(monkeypatch):
    full = with_quantity(SERIAL_PREFIX + GS + '93dGVz', 354)
    scanned = ']d2' + full.replace(GS, '{GS}')
    received = []

    class RecordingMatrix(GS1Matrix):
        def __init__(self, value):
            received.append(value)
            super().__init__(value)

    monkeypatch.setattr('km_split.labels.GS1Matrix', RecordingMatrix)
    pdf = render_label(scanned, 'PN-TEST', 354)
    assert pdf.startswith(b'%PDF-')
    assert received == [parse(scanned).full] == [full]
