"""A small but realistic fake catalogue used by the pipeline tests."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List

from tests.fakes import FakeMusicBrainz, FakeSpotify

TODAY = date(2026, 9, 18)


def _d(days_ago: int) -> str:
    return (TODAY - timedelta(days=days_ago)).isoformat()


def _album(
    album_id: str,
    name: str,
    artist_ids: List[str],
    artist_names: List[str],
    label: str,
    released: str,
    track_ids: List[str],
    track_names: List[str],
    upc: str = "",
    copyrights: List[str] | None = None,
) -> Dict[str, Any]:
    return {
        "id": album_id,
        "name": name,
        "album_type": "album",
        "release_date": released,
        "release_date_precision": "day",
        "total_tracks": len(track_ids),
        "label": label,
        "copyrights": [{"text": c, "type": "P"} for c in (copyrights or [])],
        "external_ids": {"upc": upc} if upc else {},
        "images": [{"url": f"https://img.example/{album_id}.jpg", "height": 640, "width": 640}],
        "artists": [{"id": i, "name": n} for i, n in zip(artist_ids, artist_names)],
        "tracks": {
            "items": [
                {
                    "id": tid,
                    "name": tname,
                    "track_number": index + 1,
                    "duration_ms": 200000,
                    "artists": [{"id": i, "name": n} for i, n in zip(artist_ids, artist_names)],
                }
                for index, (tid, tname) in enumerate(zip(track_ids, track_names))
            ]
        },
    }


def _artist(artist_id: str, name: str, genres: List[str]) -> Dict[str, Any]:
    return {
        "id": artist_id,
        "name": name,
        "genres": genres,
        "popularity": 50,
        "followers": {"total": 1000},
        "images": [{"url": f"https://img.example/{artist_id}.jpg"}],
    }


def _track(track_id: str, name: str, isrc: str, artist_ids: List[str], artist_names: List[str]) -> Dict[str, Any]:
    return {
        "id": track_id,
        "name": name,
        "external_ids": {"isrc": isrc},
        "artists": [{"id": i, "name": n} for i, n in zip(artist_ids, artist_names)],
        "track_number": 1,
        "duration_ms": 200000,
    }


def build() -> tuple[FakeSpotify, FakeMusicBrainz]:
    spotify = FakeSpotify()
    mb = FakeMusicBrainz()

    # A - Swedish artist on a curated Swedish independent label.
    spotify.albums["alb_a"] = _album(
        "alb_a", "Nattens Ljus", ["art_se"], ["Vinterhav"], "Adrian Recordings", _d(3),
        ["trk_a1", "trk_a2"], ["Jag saknar dej", "Hjärtat mitt"], upc="7320470260001",
    )
    spotify.artists["art_se"] = _artist("art_se", "Vinterhav", ["swedish indie pop"])
    spotify.tracks["trk_a1"] = _track("trk_a1", "Jag saknar dej", "SEBGK2600001", ["art_se"], ["Vinterhav"])
    spotify.tracks["trk_a2"] = _track("trk_a2", "Hjärtat mitt", "SEBGK2600002", ["art_se"], ["Vinterhav"])
    mb.spotify_to_mbid["art_se"] = "mb_se"
    mb.mb_artists["mb_se"] = {
        "id": "mb_se", "name": "Vinterhav", "country": "SE",
        "area": {"name": "Sweden", "iso-3166-1-codes": ["SE"]},
        "begin-area": {"name": "Göteborg"},
    }

    # B - American artist on a major.
    spotify.albums["alb_b"] = _album(
        "alb_b", "Big Budget", ["art_us"], ["Chart Machine"], "Universal Music AB", _d(1),
        ["trk_b1"], ["Money"],
    )
    spotify.artists["art_us"] = _artist("art_us", "Chart Machine", ["pop"])
    spotify.tracks["trk_b1"] = _track("trk_b1", "Money", "USUM72600001", ["art_us"], ["Chart Machine"])
    mb.spotify_to_mbid["art_us"] = "mb_us"
    mb.mb_artists["mb_us"] = {"id": "mb_us", "name": "Chart Machine", "country": "US", "area": {"name": "United States", "iso-3166-1-codes": ["US"]}}

    # C - Norwegian artist on a Norwegian label with a Nordic-sounding name.
    spotify.albums["alb_c"] = _album(
        "alb_c", "Fjord", ["art_no"], ["Nordlys"], "Fysisk Format", _d(5),
        ["trk_c1"], ["Jeg elsker deg"],
    )
    spotify.artists["art_no"] = _artist("art_no", "Nordlys", ["norwegian indie"])
    spotify.tracks["trk_c1"] = _track("trk_c1", "Jeg elsker deg", "NOXXX2600001", ["art_no"], ["Nordlys"])
    mb.spotify_to_mbid["art_no"] = "mb_no"
    mb.mb_artists["mb_no"] = {"id": "mb_no", "name": "Nordlys", "country": "NO", "area": {"name": "Norway", "iso-3166-1-codes": ["NO"]}}

    # D - British artist, major label, but written by Swedish songwriters.
    spotify.albums["alb_d"] = _album(
        "alb_d", "Written Elsewhere", ["art_gb"], ["London Voice"], "RCA Records", _d(2),
        ["trk_d1", "trk_d2"], ["Shine", "Glow"],
    )
    spotify.artists["art_gb"] = _artist("art_gb", "London Voice", ["uk pop"])
    spotify.tracks["trk_d1"] = _track("trk_d1", "Shine", "GBARL2600001", ["art_gb"], ["London Voice"])
    spotify.tracks["trk_d2"] = _track("trk_d2", "Glow", "GBARL2600002", ["art_gb"], ["London Voice"])
    mb.spotify_to_mbid["art_gb"] = "mb_gb"
    mb.mb_artists["mb_gb"] = {"id": "mb_gb", "name": "London Voice", "country": "GB", "area": {"name": "United Kingdom", "iso-3166-1-codes": ["GB"]}}
    mb.isrc_to_works["GBARL2600001"] = ["work_1"]
    mb.isrc_to_works["GBARL2600002"] = ["work_2"]
    for work_id, title in (("work_1", "Shine"), ("work_2", "Glow")):
        mb.works[work_id] = {
            "id": work_id,
            "title": title,
            "relations": [
                {"type": "composer", "target-type": "artist", "artist": {"id": "mb_w1", "name": "Max Exempel"}},
                {"type": "lyricist", "target-type": "artist", "artist": {"id": "mb_w2", "name": "Shellbeck"}},
            ],
        }
    mb.mb_artists["mb_w1"] = {"id": "mb_w1", "name": "Max Exempel", "country": "SE", "area": {"name": "Sweden", "iso-3166-1-codes": ["SE"]}}
    mb.mb_artists["mb_w2"] = {"id": "mb_w2", "name": "Shellbeck", "country": "SE", "area": {"name": "Sweden", "iso-3166-1-codes": ["SE"]}}

    # E - Swedish artist self-releasing through a DIY distributor.
    spotify.albums["alb_e"] = _album(
        "alb_e", "Hemma", ["art_se2"], ["Klara Ström"], "Klara Ström", _d(6),
        ["trk_e1"], ["Vägen hem"], copyrights=["℗ 2026 Klara Ström"],
    )
    spotify.artists["art_se2"] = _artist("art_se2", "Klara Ström", ["swedish singer-songwriter"])
    spotify.tracks["trk_e1"] = _track("trk_e1", "Vägen hem", "SEYYY2600001", ["art_se2"], ["Klara Ström"])
    mb.spotify_to_mbid["art_se2"] = "mb_se2"
    mb.mb_artists["mb_se2"] = {"id": "mb_se2", "name": "Klara Ström", "country": "SE", "area": {"name": "Sweden", "iso-3166-1-codes": ["SE"]}}

    # F - Swedish, but released outside the discovery window.
    spotify.albums["alb_f"] = _album(
        "alb_f", "Gammal", ["art_se"], ["Vinterhav"], "Adrian Recordings", _d(120),
        ["trk_f1"], ["Förr"],
    )
    spotify.tracks["trk_f1"] = _track("trk_f1", "Förr", "SEBGK2500001", ["art_se"], ["Vinterhav"])

    # G - label only findable through MusicBrainz (not on the curated list).
    spotify.albums["alb_g"] = _album(
        "alb_g", "Okänd Skiva", ["art_se3"], ["Testbandet"], "Obskyra Skivor", _d(4),
        ["trk_g1"], ["Låt ett"], upc="7320470260007",
    )
    spotify.artists["art_se3"] = _artist("art_se3", "Testbandet", [])
    spotify.tracks["trk_g1"] = _track("trk_g1", "Låt ett", "SEZZZ2600001", ["art_se3"], ["Testbandet"])
    mb.barcode_to_labels["7320470260007"] = ["mb_lab_1"]
    mb.mb_labels["mb_lab_1"] = {
        "id": "mb_lab_1", "name": "Obskyra Skivor", "country": "SE",
        "area": {"name": "Sweden", "iso-3166-1-codes": ["SE"]}, "type": "Original Production",
    }

    spotify.new_releases["SE"] = ["alb_a", "alb_b", "alb_c", "alb_d", "alb_e", "alb_f", "alb_g"]
    spotify.searches['label:"Adrian Recordings" tag:new'] = ["alb_a"]
    spotify.searches["tag:new year:2026"] = ["alb_a", "alb_b", "alb_c", "alb_d", "alb_e", "alb_g"]
    return spotify, mb
