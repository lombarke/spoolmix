import json
from pathlib import Path

import httpx

from spoolmix.spoolman import SpoolmanClient, decode_extra, encode_extra

FIX = Path(__file__).parent / "fixtures"


def _transport(fields=None, recorded=None):
    filaments = json.loads((FIX / "spoolman_filaments.json").read_text())
    spools = json.loads((FIX / "spoolman_spools.json").read_text())
    filaments[0]["extra"] = {"td": "5.5", "td_source": '"measured:td1:2026-10-16"'}
    spools[1]["archived"] = True
    spools[2]["remaining_weight"] = 0.0
    fields = fields if fields is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if recorded is not None and request.method != "GET":
            recorded.append((request.method, path, json.loads(request.content or b"null")))
            return httpx.Response(200, json={})
        if path == "/api/v1/filament":
            return httpx.Response(200, json=filaments)
        if path == "/api/v1/spool":
            return httpx.Response(200, json=spools)
        if path == "/api/v1/field/filament":
            return httpx.Response(200, json=fields)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_extra_roundtrip():
    assert decode_extra(encode_extra(5.5)) == 5.5
    assert decode_extra(encode_extra("hello")) == "hello"
    assert decode_extra(None) is None


def test_filaments_parsed_with_td_and_stock():
    with SpoolmanClient("http://spoolman.test", transport=_transport()) as sm:
        fils = sm.filaments()
    by_id = {f.id: f for f in fils}
    assert by_id[1].td == 5.5 and by_id[1].td_source == "measured:td1:2026-10-16"
    assert by_id[1].rgb == (0x39, 0xD0, 0xF1)
    assert by_id[2].td is None
    # spool for filament 2 archived, spool for filament 3 empty
    assert not by_id[2].in_stock
    assert not by_id[3].in_stock
    assert by_id[4].in_stock


def test_missing_fields():
    with SpoolmanClient("http://spoolman.test", transport=_transport(fields=[{"key": "td"}])) as sm:
        assert list(sm.missing_fields()) == ["td_source"]


def test_set_td_patches_json_encoded_extra():
    calls = []
    with SpoolmanClient("http://spoolman.test", transport=_transport(recorded=calls)) as sm:
        sm.set_filament_td(7, 4.2, "measured:seashell:2026-10-16")
    assert calls == [
        ("PATCH", "/api/v1/filament/7", {"extra": {"td": "4.2", "td_source": '"measured:seashell:2026-10-16"'}})
    ]
