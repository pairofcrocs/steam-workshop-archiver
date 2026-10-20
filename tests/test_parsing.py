import gzip
import os

from app.core import archive, scraper, steam_api
from conftest import FIXTURES


def test_ids():
    assert steam_api.is_id("123")
    assert not steam_api.is_id("123\n")
    assert not steam_api.is_id("١٢٣")  # Arabic-Indic digits
    assert not steam_api.is_id("²")
    assert not steam_api.is_id("../1")


def test_extract_workshop_id():
    assert steam_api.extract_workshop_id("https://steamcommunity.com/sharedfiles/filedetails/?id=42&search=x") == "42"
    assert steam_api.extract_workshop_id(" 42 ") == "42"
    assert steam_api.extract_workshop_id("https://example.com/?id=abc") is None


def test_parse_item_list():
    text = "1, 2\nhttps://steamcommunity.com/sharedfiles/filedetails/?id=3 junk 2"
    assert steam_api.parse_item_list(text) == ["1", "2", "3"]


def test_parse_app_ref():
    assert steam_api.parse_app_ref("620") == "620"
    assert steam_api.parse_app_ref("https://store.steampowered.com/app/620/Portal_2/") == "620"
    assert steam_api.parse_app_ref("https://steamcommunity.com/workshop/browse/?appid=4000") == "4000"
    assert steam_api.parse_app_ref("hello") is None


def test_normalize_details_detects_collections():
    d = steam_api.normalize_details({"publishedfileid": "1", "result": 1, "creator_app_id": 766, "consumer_app_id": 4000})
    assert d["is_collection"] and d["appid"] == "4000"
    d = steam_api.normalize_details({"publishedfileid": "2", "file_description": "hi", "consumer_appid": 1})
    assert d["description"] == "hi" and d["appid"] == "1" and not d["is_collection"]


def test_browse_page_fixture():
    html = gzip.open(os.path.join(FIXTURES, "browse_page.html.gz"), "rt", encoding="utf-8").read()
    items = scraper.parse_workshop_browse_page(html)
    assert len(items) == 30
    assert all(steam_api.is_id(i["id"]) for i in items)
    assert sum(1 for i in items if i["title"]) >= 25
    meta = scraper.parse_workshop_browse_meta(html)
    assert meta["per_page"] == 30 and meta["max_pages"] == 1000
    assert scraper.game_name_from_browse_html(html) == "Garry's Mod"


def test_scrape_failure_is_incomplete(monkeypatch):
    import requests

    def boom(*a, **kw):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(scraper.SESSION, "get", boom)
    result = scraper.scrape_workshop("620", None, 0, 0, "trend_week", lambda m: None)
    assert not result.complete and result.items == [] and "down" in result.error


ACF = '''"AppWorkshop"
{
	"appid"		"620"
	"WorkshopItemsInstalled"
	{
		"111"
		{
			"size"		"500"
			"timeupdated"		"10"
		}
		"222"
		{
			"size"		"700"
			"timeupdated"		"20"
		}
	}
	"WorkshopItemDetails"
	{
		"111"
		{
			"manifest"		"1"
		}
		"222"
		{
			"manifest"		"2"
		}
	}
}
'''


def test_acf_parse_and_remove(env):
    path = archive.acf_path("620")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(ACF)
    assert archive.parse_acf("620") == {"111": {"size": 500, "timeupdated": 10}, "222": {"size": 700, "timeupdated": 20}}
    archive.remove_from_acf("620", ["111"])
    text = open(path).read()
    assert '"111"' not in text and '"222"' in text
    assert archive.parse_acf("620") == {"222": {"size": 700, "timeupdated": 20}}


def test_safe_name():
    assert archive.safe_name("../../etc/passwd", "x") == "___etc_passwd"
    assert archive.safe_name("Карта", "x") == "Карта"
    assert archive.safe_name("  ..  ", "fallback") == "fallback"
    assert archive.safe_name('a:b*c?"d', "x") == "a_b_c_d"


def test_excerpt_drops_media_and_urls():
    from app.routes.archive import _excerpt

    text = "[img]https://x/y.jpg[/img]\n[code]Nice [u]map[/u] https://a.b/c[/code] [previewyoutube=a;full][/previewyoutube]"
    assert _excerpt(text) == "Nice map"
    assert len(_excerpt("word " * 100)) <= 160


def test_image_magic(tmp_path):
    from app.core.net import _looks_like_image

    png = tmp_path / "a"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 10)
    html = tmp_path / "b"
    html.write_bytes(b"<html>not an image</html>")
    assert _looks_like_image(str(png)) and not _looks_like_image(str(html))
