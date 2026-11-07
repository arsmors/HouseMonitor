"""Focused parser and matching regression checks."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from backend.sources import City24Adapter, RentInRigaAdapter, location_ids_for_text, match, parse_area, parse_city24, parse_latio, parse_rentinriga, parse_ss, parse_ss_image


class SourceTests(unittest.TestCase):
    """Exercise the critical source-data boundary."""

    def test_area_formats(self):
        """Normalize published numbers without accepting land units or ranges."""
        for raw, expected in [("165 m²", 165), ("1 250 m2", 1250), ("165,5", 165.5), ("165.5", 165.5)]:
            self.assertEqual(parse_area(raw), (expected, "parsed"))
        for raw in ["1 ha", "100-200", "NaN", "0", "plot 1200"]:
            self.assertEqual(parse_area(raw)[1], "unparseable")
        self.assertEqual(parse_area("-"), (None, "missing"))

    def test_verified_ss_columns(self):
        """Select 330 square metres of house, never 1248 square metres of land."""
        html = '''<table><tr id="head_line"><td colspan="3">Sludinājumi datums</td><td>Iela</td><td>m²</td><td>Stāvi</td><td>Ist.</td><td>Zem. pl.</td><td>Cena</td></tr>
        <tr id="tr_58242094"><td></td><td><img class="isfoto" src="https://i.ss.lv/photo.jpg"></td><td><a class="am" href="/msg/lv/real-estate/homes-summer-residences/riga/vecaki/anklj.html">House</a></td><td>Šalkones 6</td><td>330</td><td>3</td><td>20</td><td>1248 m²</td><td>250,000 €</td></tr></table>'''
        listing = parse_ss(html, "https://www.ss.lv/", {"ss:riga/vecaki": {"label": "Vecāķi"}})[0]
        self.assertEqual(listing.id, "ss:anklj")
        self.assertEqual(listing.area, 330)
        self.assertEqual(listing.area_raw, "330")
        self.assertEqual(listing.location_ids, ["ss:riga", "ss:riga/vecaki"])
        self.assertEqual(listing.address, "Šalkones 6")

    def test_ss_sell_feed_excludes_buyer_requests(self):
        """Do not show wanted ads returned in the SS.lv sale-category feed."""
        html = '''<table><tr id="head_line"><td colspan="3">Sludinājumi datums</td><td>Iela</td><td>m²</td><td>Stāvi</td><td>Ist.</td><td>Zem. pl.</td><td>Cena</td></tr>
        <tr id="tr_1"><td></td><td></td><td><a class="am" href="/msg/lv/real-estate/homes-summer-residences/riga/vecaki/house.html">Pārdod māju</a></td><td>Street 1</td><td>165</td><td>2</td><td>5</td><td>600</td><td>250,000 €</td></tr>
        <tr id="tr_2"><td></td><td></td><td><a class="am" href="/msg/lv/real-estate/homes-summer-residences/riga/vecaki/wanted.html">Nopirksim māju vai vasarnīcu</a></td><td>Vecāķi</td><td>-</td><td>-</td><td>-</td><td>-</td><td>pērku</td></tr></table>'''
        listings = parse_ss(html, "https://www.ss.lv/", {"ss:riga/vecaki": {"label": "Vecāķi"}})
        self.assertEqual([listing.id for listing in listings], ["ss:house"])

    def test_ss_gallery_image(self):
        """Select an actual full-size gallery link and reject thumbnails and other hosts."""
        html = '''<a onclick="big_pic(1, event);return false;" href="https://untrusted.example/gallery/photo.800.jpg"></a>
        <a onclick="big_pic(1, event);return false;" href="https://i.ss.lv/gallery/house.th2.jpg"></a>
        <a onclick="big_pic(1, event);return false;" href="https://i.ss.lv/gallery/house.800.jpg"><img src="https://i.ss.lv/gallery/house.th2.jpg"></a>'''
        self.assertEqual(parse_ss_image(html, "https://www.ss.lv/msg/house.html"), "https://i.ss.lv/gallery/house.800.jpg")
        self.assertIsNone(parse_ss_image('<img src="https://i.ss.lv/gallery/house.th2.jpg">', "https://www.ss.lv/msg/house.html"))

    def test_filter_boundaries_and_missing(self):
        """Keep inclusive limits and exclude missing area only when filtered."""
        search = {"min": 100, "max": 250, "locations": ["ss:riga/vecaki"]}
        for area in [100, 250]:
            self.assertTrue(match({"area": area, "location_ids": search["locations"]}, search))
        for area in [99, 251, None]:
            self.assertFalse(match({"area": area, "location_ids": search["locations"]}, search))
        self.assertTrue(match({"area": None, "location_ids": []}, {"min": None, "max": None, "locations": []}))
        self.assertFalse(match({"area": 165, "location_ids": ["ss:riga"]}, search))

    def test_city24_house_area_and_geography(self):
        """Use explicit house-area metadata, not the larger plot-area value."""
        html = '''<div class="object--result"><a href="/real-estate/houses-for-sale/riga-vecaki/123?i=0"></a>
        <div class="object__address">Street 1, Vecāķi</div><div class="object__area">Rīga</div><div class="object__slogan">House</div>
        <img src="/assets/img/placeholder/house.svg" alt="Pārdod māju - Rīga, Street 1 - 169 m², 6 istabas, 260 000 €">
        <img src="https://static.img-city24.lv/house.jpg" alt="Pārdod māju - Rīga, Street 1 - 169 m², 6 istabas, 260 000 €">
        <span>169 m²</span><span>803 m²</span><span>260 000 €</span></div>'''
        listing = parse_city24(html, {"ss:riga/vecaki": {"label": "Vecāķi"}})[0]
        self.assertEqual(listing.id, "city24:123")
        self.assertEqual(listing.area, 169)
        self.assertEqual(listing.image, "https://static.img-city24.lv/house.jpg")
        self.assertEqual(listing.address, "Street 1, Vecāķi")
        self.assertIn("ss:riga/vecaki", listing.location_ids)
        self.assertEqual(listing.url, "https://www.city24.lv/real-estate/houses-for-sale/riga-vecaki/123")

    def test_city24_geographic_search_validation(self):
        """Resolve geographic labels and disallow keyword, foreign-host and broad cached URLs."""
        locations = {"ss:riga/vecaki": {"label": "Vecāķi"}, "ss:riga-region/carnikavas-nov": {"label": "Carnikavas nov."}, "ss:riga/mangalsala": {"label": "Mangaļsala"}, "ss:riga/vecdaugava": {"label": "Vecdaugava"}}
        adapter = City24Adapter(None, locations, list(locations))
        self.assertEqual(adapter.location_labels(), ["Vecāķi", "Carnikavas pag.", "Mangaļu pussala", "Vecdaugava"])
        self.assertEqual(len(adapter.search_urls), len(locations))
        self.assertEqual({url.rsplit('/id=', 1)[-1] for url in adapter.search_urls}, {"270711-district", "245490-parish", "270744-district", "270696-district"})
        self.assertTrue(all(adapter.valid_search_url(url) for url in adapter.search_urls))
        self.assertIn("ss:riga/mangalsala", location_ids_for_text("Street 1, Mangaļu pussala, Rīga", locations))
        self.assertTrue(adapter.valid_search_url("https://www.city24.lv/real-estate-search/houses-for-sale/adazu-nov-carnikavas-pag/id=245490-parish"))
        for url in ["https://untrusted.example/real-estate-search/houses-for-sale/id=245490-parish", "https://www.city24.lv/real-estate-search/houses-for-sale", "https://www.city24.lv/real-estate-search/houses-for-sale/kw=Vecaki", "http://www.city24.lv/real-estate-search/houses-for-sale/id=245490-parish"]:
            self.assertFalse(adapter.valid_search_url(url))

    def test_rentinriga_sale_house_area_and_geography(self):
        """Use the house area, not land or monthly payment, and keep source identity."""
        html = '''<a class="real-estate-item__link" href="/lv/maju-pardosana/street-1/123">
        <img src="https://production-riga.fra1.digitaloceanspaces.com/properties/123/photo.webp">
        <div class="real-estate-item__name">Street 1 / Ādažu nov., Carnikavas pag., Garciems</div>
        <div class="real-estate-item__price">250 000 €</div><div class="real-estate-item__price-per-month">1000 €/mēn.</div>
        <div class="real-estate-item__info-item"><span class="real-estate-item__info-label">Zemes platība:</span><span class="real-estate-item__info-value">1200 m²</span></div>
        <div class="real-estate-item__info-item"><span class="real-estate-item__info-label">Platība:</span><span class="real-estate-item__info-value">165 m²</span></div>
        <time datetime="2026-11-05T10:00:00+02:00"></time></a>
        <a class="real-estate-item__link" href="/lv/maju-ire/street/456"><div class="real-estate-item__name">Rental</div></a>'''
        result = parse_rentinriga(html, {})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, "rentinriga:123")
        self.assertEqual(result[0].source, "RentInRiga.lv")
        self.assertEqual(result[0].area, 165)
        self.assertEqual(result[0].price, "250 000 €")
        self.assertEqual(result[0].address, "Street 1")
        self.assertIn("ss:riga-region/carnikavas-nov", result[0].location_ids)
        self.assertEqual(result[0].published_at, "2026-11-05T10:00:00+02:00")

    def test_rentinriga_missing_area_and_outside_locations(self):
        """Allow missing area only without limits and exclude unselected geography."""
        html = '''<a class="real-estate-item__link" href="/lv/maju-pardosana/street/124"><div class="real-estate-item__name">Street 1 / Jūrmala, Dzintari</div></a>'''
        result = parse_rentinriga(html, {})[0].record()
        self.assertIsNone(result["area"])
        self.assertEqual(result["area_status"], "missing")
        self.assertFalse(match(result, {"locations": ["ss:riga/vecaki"], "min": None, "max": None}))
        self.assertFalse(match(result, {"locations": [], "min": 100, "max": None}))
        self.assertTrue(match(result, {"locations": [], "min": None, "max": None}))

    def test_rentinriga_filter_url_and_valid_empty_results(self):
        """Keep all four selected location IDs and accept a genuine empty search."""
        selected = ["ss:riga-region/carnikavas-nov", "ss:riga/vecaki", "ss:riga/mangalsala", "ss:riga/vecdaugava"]
        adapter = RentInRigaAdapter(None, {}, selected)
        urls = adapter.default_search_urls()
        self.assertEqual(len(urls), 4)
        self.assertTrue(all(adapter.valid_search_url(url) for url in urls))
        self.assertEqual({url.rsplit("location_id=", 1)[-1] for url in urls}, {"20", "709", "698", "710"})
        self.assertFalse(adapter.valid_search_url(urls[0] + "&location_id=709"))
        self.assertFalse(adapter.valid_search_url("https://www.rentinriga.lv/lv/maju-pardosana"))
        self.assertFalse(adapter.valid_search_url("https://untrusted.example/lv/maju-pardosana?transaction_type=sales&object_type=House&location_id=20"))
        self.assertEqual(parse_rentinriga('<h1>Pārdod mājas</h1><p>Nav rezultātu ar izvēlētajiem filtriem.</p>', {}), [])
        with self.assertRaises(ValueError):
            parse_rentinriga('<h1>Access unavailable</h1>', {})

    def test_latio_house_sale_only(self):
        """Retain sale houses and distinguish the labelled building and land icons."""
        html = '''<div class="results__item"><a class="results__link" href="/lv/ipasumi/123"></a><div class="results__title">Street 1, Vecāķi, Rīga</div>
        <div class="results__location">Pirkt | Māja | Vecāķi</div><span class="results__price">250 000 €</span>
        <ul><li><img data-src="/img/svg/ic_apartaments_area.svg"><span>226 m<sup>2</sup></span></li><li><img data-src="/img/svg/ic-zeme.svg"><span>527 m2</span></li></ul></div>
        <div class="results__item"><a class="results__link" href="/lv/ipasumi/456"></a><p>Īrēt | Māja | Rīga</p></div>'''
        listings = parse_latio(html, {"ss:riga/vecaki": {"label": "Vecāķi"}})
        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0].area, 226)
        self.assertEqual(listings[0].id, "latio:123")
        self.assertEqual(listings[0].address, "Street 1, Vecāķi, Rīga")

    def test_carnikava_does_not_expand_to_all_adazi(self):
        """Map the current parish name without treating all Ādaži as Carnikava."""
        self.assertIn("ss:riga-region/carnikavas-nov", location_ids_for_text("Gauja, Carnikavas pagasts, Ādažu novads", {}))
        self.assertNotIn("ss:riga-region/carnikavas-nov", location_ids_for_text("Ādaži, Ādažu novads", {}))


class SourceAsyncTests(unittest.IsolatedAsyncioTestCase):
    """Exercise asynchronous source readiness independently of external websites."""

    async def test_rentinriga_waits_for_hydration_before_accepting_empty(self):
        """A transient pre-hydration empty message must not finish the source check."""
        readiness = SimpleNamespace(wait_for=AsyncMock())
        cards = SimpleNamespace(or_=lambda empty: SimpleNamespace(first=readiness))
        page = SimpleNamespace(wait_for_load_state=AsyncMock(), locator=lambda selector: cards,
                               get_by_text=lambda text, exact: readiness, content=AsyncMock(return_value="rendered"))
        adapter = RentInRigaAdapter(None, {})
        result = await adapter.rendered_page(page)
        page.wait_for_load_state.assert_awaited_once_with("networkidle", timeout=20000)
        readiness.wait_for.assert_awaited_once_with(timeout=30000)
        self.assertEqual(result, "rendered")


if __name__ == "__main__":
    unittest.main()