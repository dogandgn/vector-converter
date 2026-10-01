"""Verify install layout, metadata, portable syntax and source consistency."""
from pathlib import Path
import argparse
import ast
import configparser
import hashlib
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def verify(path):
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        names = archive.namelist()
        assert len(names) == len(set(names)) == 27
        assert all(n.startswith("parsel_donusturucu/")
                   and len(Path(n).parts) == 2 and ".." not in Path(n).parts
                   for n in names)
        for name in names:
            assert archive.read(name) == (ROOT / name).read_bytes(), name
            text = archive.read(name).decode("utf-8")
            assert not re.search(r"[A-Za-z]:[\\/](?:Users|Program Files)|/home/|/Users/", text), name
            assert not re.search(r"localhost|127\.0\.0\.1", text), name
            if name.endswith(".py"):
                ast.parse(text, filename=name, feature_version=(3, 9))
        metadata = configparser.ConfigParser()
        metadata.read_string(archive.read("parsel_donusturucu/metadata.txt").decode("utf-8"))
        general = metadata["general"]
        for key in ("name", "description", "about", "author", "email", "version",
                    "qgisMinimumVersion", "homepage", "repository", "tracker", "icon"):
            assert general.get(key), key
        assert general["version"] == "0.9.0"
        assert general["license"] == "GPL-3.0-or-later"
        assert general.getboolean("experimental")
        assert "parsel_donusturucu/LICENSE" in names
        assert len(archive.read("parsel_donusturucu/LICENSE")) > 30000
        assert "parsel_donusturucu/" + general["icon"] in names
    print("PASS", path.name, hashlib.sha256(path.read_bytes()).hexdigest())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("zip", nargs="?", type=Path,
                        default=ROOT / "dist/parsel_donusturucu-0.9.0.zip")
    verify(parser.parse_args().zip)
