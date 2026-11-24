"""Read the same inner recipe archives as Tandoor's Default importer."""

from io import BytesIO
from pathlib import Path
from zipfile import ZipFile


def open_tandoor_recipe(path: str | Path, member: str | None = None) -> ZipFile:
    with ZipFile(path) as outer:
        if member is None:
            [member] = outer.namelist()
        assert member.endswith(".zip")
        return ZipFile(BytesIO(outer.read(member)))
