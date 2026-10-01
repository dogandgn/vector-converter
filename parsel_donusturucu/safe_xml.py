"""Bounded XML parsing without document types, entities or external resources."""

from pathlib import Path

# Only the bounded, DTD-rejecting parser below reads XML.
import xml.etree.ElementTree as ET  # nosec B405

MAX_XML_BYTES = 64 * 1024 * 1024


class NoDTD(ET.TreeBuilder):
    def doctype(self, *args):
        raise ValueError("KML DTD/entity tanımı kabul edilmez.")


def parse_bytes(data):
    if len(data) > MAX_XML_BYTES:
        raise ValueError("KML 64 MiB sınırını aşıyor.")
    # TreeBuilder's DTD callback aborts before declarations or entity expansion.
    parser = ET.XMLParser(target=NoDTD())  # nosec B314
    return ET.fromstring(data, parser=parser)  # nosec B314


def parse(path):
    with Path(path).open("rb") as stream:
        return parse_bytes(stream.read(MAX_XML_BYTES + 1))
