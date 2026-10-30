"""Regression tests for PN display and printed label catalog-name cleanup."""
from io import BytesIO

import pytest
from pypdf import PdfReader

from km_split.codes import GS, with_quantity
from km_split.labels import display_pn, render_label


PN = '7864 6-3001'
SOURCE = '0104640638345218215DEMO000000000000000' + GS + '93dGVz'


@pytest.mark.parametrize('raw', [
    'Артикул ' + PN,
    'Модель ' + PN,
    'Модель/Артикул ' + PN,
    'Модель / Артикул ' + PN,
    'Модель/Артикул: ' + PN,
    'Модель / Артикул : ' + PN,
    'Артикул/Модель: ' + PN,
    'АРТИКУЛ: ' + PN,
    'модель: ' + PN,
    '  модель / артикул:  ' + PN + '  ',
])
def test_catalog_field_names_are_removed_from_pn(raw):
    assert display_pn(raw) == PN


@pytest.mark.parametrize('raw', [
    PN,
    'A-Артикул-42',
    'Модель-X1',
    'Модель/Артикул-001',
    'Артикул/Модель',
])
def test_actual_pn_is_not_modified(raw):
    assert display_pn(raw) == raw


@pytest.mark.parametrize('raw', [
    'Артикул ' + PN,
    'Модель ' + PN,
    'Модель / Артикул ' + PN,
    'Модель/Артикул: ' + PN,
])
def test_pdf_prints_pn_without_catalog_prefix(raw):
    pdf = render_label(with_quantity(SOURCE, 400), raw, 400)
    text = PdfReader(BytesIO(pdf)).pages[0].extract_text()

    assert PN in text
    assert 'Артикул' not in text
    assert 'Модель' not in text
