"""Preset digs.

A dig is a saved corner of an archive worth rummaging through. These are aimed
squarely at the Dilla / Nujabes palette: dusty horns, unaccompanied piano
trios, string beds, gospel choirs, spoken-word interludes and drum breaks.

Each dig is just search parameters, so you can copy one, tweak the years or
subjects, and keep your own.
"""
from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Dig:
    slug: str
    name: str
    blurb: str
    source: str = "ia"
    params: dict[str, Any] = field(default_factory=dict)
    #: Roughly how deep the seam runs — used to pick a random page.
    depth: int = 40

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# The Great 78 Project (`georgeblood`) is the single richest seam: hundreds of
# thousands of 78rpm transfers, out of US copyright, already noisy in the way
# you would otherwise spend an afternoon faking.
DIGS: list[Dig] = [
    Dig(
        slug="dusty-78s",
        name="Dusty 78s",
        blurb="Pre-war jazz and blues off shellac. Surface noise included, free of charge.",
        params={
            "collections": ["georgeblood", "78rpm"],
            "subjects": ["Jazz", "Blues", "Big Band", "Dance Band"],
            "year_from": 1920, "year_to": 1955,
        },
        depth=120,
    ),
    Dig(
        slug="soul-45s",
        name="Soul & Funk 45s",
        blurb="Pre-1972 soul, funk and R&B — horn stabs, tambourines, tape hiss.",
        params={
            "collections": ["unlockedrecordings", "audio_music"],
            "subjects": ["Soul", "Funk", "Rhythm and Blues", "Motown"],
            "year_from": 1962, "year_to": 1978,
        },
        depth=80,
    ),
    Dig(
        slug="piano-trios",
        name="Piano Trios",
        blurb="Piano, upright bass, brushed drums. The Nujabes food group.",
        params={
            "collections": ["audio_music", "unlockedrecordings", "georgeblood"],
            "subjects": ["Jazz", "Piano", "Cool Jazz", "Jazz Trio"],
            "year_from": 1950, "year_to": 1975,
        },
        depth=60,
    ),
    Dig(
        slug="bossa",
        name="Bossa & Brazil",
        blurb="Nylon strings, soft snares, everything slightly behind the beat.",
        params={
            "subjects": ["Bossa Nova", "Samba", "Brazilian", "Latin Jazz"],
            "year_from": 1958, "year_to": 1980,
        },
        depth=40,
    ),
    Dig(
        slug="library-music",
        name="Library & Film Score",
        blurb="Production music written to be used. Odd, cinematic, unclaimed.",
        params={
            "subjects": ["Library Music", "Soundtrack", "Production Music", "Film Score"],
            "year_from": 1960, "year_to": 1985,
        },
        depth=40,
    ),
    Dig(
        slug="gospel",
        name="Gospel & Choir",
        blurb="Choirs, organ, room reverb. Chop the vocal, keep the room.",
        params={
            "collections": ["georgeblood", "audio_music"],
            "subjects": ["Gospel", "Spiritual", "Choir", "Sacred"],
            "year_from": 1925, "year_to": 1975,
        },
        depth=50,
    ),
    Dig(
        slug="strings",
        name="Strings & Easy",
        blurb="Lush orchestral beds and easy listening. Pitch them down and they glow.",
        params={
            "subjects": ["Easy Listening", "Orchestra", "Strings", "Mood Music"],
            "year_from": 1955, "year_to": 1978,
        },
        depth=50,
    ),
    Dig(
        slug="organ-rhodes",
        name="Organ & Rhodes",
        blurb="Hammond, Wurlitzer, electric piano — soul-jazz keys.",
        params={
            "subjects": ["Organ", "Soul Jazz", "Hammond", "Electric Piano"],
            "year_from": 1960, "year_to": 1978,
        },
        depth=40,
    ),
    Dig(
        slug="breaks",
        name="Drums & Breaks",
        blurb="Percussion-forward records. Sort by breakiness once they're analysed.",
        params={
            "subjects": ["Drum", "Percussion", "Latin Percussion", "Marching Band"],
            "year_from": 1950, "year_to": 1980,
        },
        depth=40,
    ),
    Dig(
        slug="spoken-word",
        name="Spoken Word",
        blurb="Interviews, sermons, old radio. Where the interludes come from.",
        params={
            "collections": ["oldtimeradio", "audio_podcast", "audio_bookspoetry"],
            "year_from": 1930, "year_to": 1985,
        },
        depth=60,
    ),
    Dig(
        slug="netlabel-instrumentals",
        name="Netlabel Instrumentals",
        blurb="Modern, Creative Commons, cleared for release. Safe crate.",
        params={"collections": ["netlabels"], "subjects": ["Instrumental", "Downtempo", "Hip Hop"]},
        depth=40,
    ),
    Dig(
        slug="cc-jazz",
        name="Open-Licence Jazz",
        blurb="Openly licensed jazz and lo-fi from FMA, Jamendo and ccMixter.",
        source="openverse",
        params={"q": "jazz piano instrumental", "license_type": "commercial,modification"},
        depth=20,
    ),
    Dig(
        slug="cc-drums",
        name="Open-Licence Drums",
        blurb="Openly licensed drum loops and breaks you can clear without a lawyer.",
        source="openverse",
        params={"q": "drum break loop", "license_type": "commercial,modification"},
        depth=20,
    ),
]

BY_SLUG = {d.slug: d for d in DIGS}


def get(slug: str) -> Dig | None:
    return BY_SLUG.get(slug)


def random_dig(rng: random.Random | None = None) -> Dig:
    return (rng or random).choice(DIGS)


def random_page(dig: Dig, rng: random.Random | None = None) -> int:
    """Pick a page somewhere down the seam.

    Archives sort by popularity, so page 1 is the same famous records everyone
    already has. Digging means going deeper than that.
    """
    return (rng or random).randint(1, max(1, dig.depth))
