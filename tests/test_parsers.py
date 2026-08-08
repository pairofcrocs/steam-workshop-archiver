"""Unit tests for the Steam HTML / ACF / URL parsers in app.core.utils."""

import json

from app.core.utils import (
    _acf_vdf_block,
    build_workshop_url,
    extract_workshop_id,
    format_bytes,
    game_name_from_browse_html,
    parse_acf_items,
    parse_workshop_browse_meta,
    parse_workshop_browse_page,
)

ACF_SAMPLE = """\
"AppWorkshop"
{
\t"appid"\t\t"620"
\t"SizeOnDisk"\t\t"104857600"
\t"WorkshopItemsInstalled"
\t{
\t\t"100200300"
\t\t{
\t\t\t"size"\t\t"52428800"
\t\t\t"timeupdated"\t\t"1700000000"
\t\t\t"manifest"\t\t"111"
\t\t}
\t\t"400500600"
\t\t{
\t\t\t"size"\t\t"1024"
\t\t\t"timeupdated"\t\t"1690000000"
\t\t\t"manifest"\t\t"222"
\t\t}
\t}
\t"WorkshopItemDetails"
\t{
\t\t"100200300"
\t\t{
\t\t\t"timetouched"\t\t"1700000001"
\t\t}
\t}
}
"""


def test_acf_vdf_block_extracts_nested_section():
    body = _acf_vdf_block(ACF_SAMPLE, "WorkshopItemsInstalled")
    assert '"100200300"' in body
    assert '"400500600"' in body
    assert "WorkshopItemDetails" not in body


def test_acf_vdf_block_missing_key():
    assert _acf_vdf_block(ACF_SAMPLE, "NoSuchSection") == ""
    assert _acf_vdf_block("", "WorkshopItemsInstalled") == ""


def test_parse_acf_items(tmp_path):
    acf = tmp_path / "appworkshop_620.acf"
    acf.write_text(ACF_SAMPLE, encoding="utf-8")
    items = parse_acf_items(str(acf))
    assert items == {
        "100200300": {"size": 52428800, "timeupdated": 1700000000},
        "400500600": {"size": 1024, "timeupdated": 1690000000},
    }


def test_parse_acf_items_missing_file(tmp_path):
    assert parse_acf_items(str(tmp_path / "nope.acf")) == {}


def test_extract_workshop_id():
    assert extract_workshop_id(
        "https://steamcommunity.com/sharedfiles/filedetails/?id=123456789"
    ) == "123456789"
    assert extract_workshop_id(
        "https://steamcommunity.com/sharedfiles/filedetails/?id=42&searchtext=x"
    ) == "42"
    assert extract_workshop_id("https://example.com/no-id-here") is None
    assert extract_workshop_id("https://example.com/?id=notanumber") is None


def test_build_workshop_url_sort_keys():
    url = build_workshop_url("620", page=3, sort_key="trend_week")
    assert "appid=620" in url and "p=3" in url
    assert "browsesort=trend" in url and "days=7" in url

    url = build_workshop_url("620", page=1, sort_key="mostrecent")
    assert "browsesort=mostrecent" in url and "days=" not in url

    # Unknown sort key falls back to weekly trend
    url = build_workshop_url("620", page=1, sort_key="bogus")
    assert "browsesort=trend" in url and "days=7" in url


def test_format_bytes():
    assert format_bytes(0) == "0.00 B"
    assert format_bytes(1536) == "1.50 KB"
    assert format_bytes(52428800) == "50.00 MB"
    assert format_bytes(3 * 1024**4) == "3.00 TB"


def _ssr_page(header: dict, query: dict) -> str:
    parts = ["nav", json.dumps(header), json.dumps(query)]
    return f"<html><script>window.SSR.loaderData = {json.dumps(parts)};</script></html>"


def test_game_name_from_ssr():
    html = _ssr_page({"appHubHeader": {"name": "Portal 2"}}, {})
    assert game_name_from_browse_html(html) == "Portal 2"


def test_game_name_from_title_fallback():
    html = "<html><title>The Steam Workshop for Left 4 Dead 2</title></html>"
    assert game_name_from_browse_html(html) == "Left 4 Dead 2"


def test_game_name_unavailable():
    assert game_name_from_browse_html("<html></html>") == ""


def test_parse_workshop_browse_meta():
    html = _ssr_page(
        {},
        {"workshopNumbers": {"total": 95}, "serverQuery": {"num_per_page": 30}},
    )
    meta = parse_workshop_browse_meta(html)
    assert meta == {"total": 95, "per_page": 30, "max_pages": 4}


def test_parse_workshop_browse_meta_caps_pages():
    html = _ssr_page(
        {},
        {"workshopNumbers": {"total": 90000}, "serverQuery": {"num_per_page": 30}},
    )
    assert parse_workshop_browse_meta(html)["max_pages"] == 1000


def test_parse_workshop_browse_page():
    html = """
    <a href="https://steamcommunity.com/sharedfiles/filedetails/?id=111"><img alt="First Map"></a>
    <div>By AuthorOne</div>
    <a href="https://steamcommunity.com/sharedfiles/filedetails/?id=222"><img alt="Second &amp; Map"></a>
    <div>By AuthorTwo</div>
    """
    titles, links, authors = parse_workshop_browse_page(html)
    assert links == [
        "https://steamcommunity.com/sharedfiles/filedetails/?id=111",
        "https://steamcommunity.com/sharedfiles/filedetails/?id=222",
    ]
    assert titles == ["First Map", "Second & Map"]
    assert authors == ["AuthorOne", "AuthorTwo"]


def test_parse_workshop_browse_page_dedupes_links():
    html = """
    <a href="https://steamcommunity.com/sharedfiles/filedetails/?id=111"><img alt="A"></a>
    <a href="https://steamcommunity.com/sharedfiles/filedetails/?id=111"><img alt="A"></a>
    """
    titles, links, authors = parse_workshop_browse_page(html)
    assert len(links) == 1
    assert len(authors) == 1  # padded to match links
