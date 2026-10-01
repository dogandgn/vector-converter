"""Build a reproducible QGIS install ZIP using only the standard library."""
from pathlib import Path
import argparse
import ast
import configparser
import hashlib
import io
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "parsel_donusturucu"


def build(output):
    metadata = configparser.ConfigParser()
    metadata.read(PACKAGE / "metadata.txt", encoding="utf-8")
    assert metadata["general"]["version"] == "0.9.0"
    files = sorted(p for p in PACKAGE.iterdir() if p.is_file()
                   and (p.suffix in {".py", ".svg", ".txt", ".md", ".html"}
                        or p.name == "LICENSE"))
    assert len(files) == 27 and sum(p.suffix == ".py" for p in files) == 21
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            if path.suffix == ".py":
                ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 9))
            entry = zipfile.ZipInfo("parsel_donusturucu/" + path.name,
                                    (2026, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, path.read_bytes())
    raw = stream.getvalue()
    if output.exists():
        if output.read_bytes() != raw:
            raise SystemExit("Destination differs; choose a new --output path.")
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("xb") as target:
            target.write(raw)
    print(output.name, hashlib.sha256(raw).hexdigest(), len(raw), "bytes")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path,
                        default=ROOT / "dist/parsel_donusturucu-0.9.0.zip")
    build(parser.parse_args().output)
