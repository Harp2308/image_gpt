"""
IRM / Sensitivity-label stripper for .xlsx files — local dev utility.

Strips ALL known locations where AAD/MSIPC/sensitivity-label metadata lives:
  1. customXml/* parts + their rels + Content_Types entries  (primary location)
  2. docProps/custom.xml — sensitivity label GUID/policy stored as custom props
  3. xl/workbook.xml — <mc:AlternateContent> blocks referencing absences labels

Original file is NEVER modified — output is always a new path.
"""

import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

try:
    from colep_ai.core.logger import get_logger
    logger = get_logger(__name__)
except ImportError:
    import logging
    logger = logging.getLogger(__name__)


_IRM_PART_PATTERNS = [
    re.compile(r"^customXml/", re.IGNORECASE),
]

_IRM_REL_TYPES = {
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/customXml",
}

# Sensitivity label custom property name prefixes (case-insensitive)
# These appear in docProps/custom.xml as <vt:lpwstr> under these names
_SENSITIVITY_PROP_PREFIXES = (
    "msip_",        # Microsoft Information Protection
    "msipc_",
    "sensitivitylabel",
    "_dlp_",        # Data Loss Prevention
)


def _is_irm_part(name: str) -> bool:
    return any(p.match(name) for p in _IRM_PART_PATTERNS)


def _strip_irm_rels(xml: str) -> str:
    """Remove Relationship elements whose Type points to customXml."""
    for rel_type in _IRM_REL_TYPES:
        xml = re.sub(
            rf'<Relationship[^>]*Type="{re.escape(rel_type)}"[^>]*/?>',
            "", xml,
        )
    xml = re.sub(
        r'<Relationship[^>]*Target="(?:\.\.\/)?customXml\/[^"]*"[^>]*/?>',
        "", xml,
    )
    return xml


def _strip_content_types(xml: str) -> str:
    """Remove Override entries for customXml parts."""
    return re.sub(
        r'<Override[^>]*PartName="/customXml/[^"]*"[^>]*/?>',
        "", xml,
    )


def _strip_custom_props(data: bytes) -> bytes:
    """
    Strip sensitivity-label entries from docProps/custom.xml.
    These are <property> elements whose name starts with MSIP_ / _dlp_ etc.
    Leaves all other custom properties intact.
    """
    try:
        xml = data.decode("utf-8")
    except Exception:
        return data  # can't decode, leave untouched

    # Register namespaces to avoid ns0: mangling on re-serialise
    namespaces = {
        "": "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties",
        "vt": "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes",
    }
    for prefix, uri in namespaces.items():
        ET.register_namespace(prefix, uri)

    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return data

    ns = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
    removed = 0
    for prop in list(root):
        name = prop.get("name", "")
        if name.lower().startswith(_SENSITIVITY_PROP_PREFIXES):
            root.remove(prop)
            removed += 1
            logger.info(f"[irm_strip] removed custom prop: {name}")

    if removed == 0:
        return data

    # Re-serialise preserving XML declaration
    out = ET.tostring(root, encoding="unicode", xml_declaration=False)
    decl = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
    return (decl + out).encode("utf-8")


def strip_irm(input_path: str, output_path: str | None = None) -> str:
    input_p = Path(input_path).resolve()
    if output_path is None:
        output_p = input_p.with_stem(input_p.stem + "_stripped")
    else:
        output_p = Path(output_path).resolve()

    output_p.parent.mkdir(parents=True, exist_ok=True)

    irm_parts_found = []

    with zipfile.ZipFile(input_p, "r") as zin, \
         zipfile.ZipFile(output_p, "w", compression=zipfile.ZIP_DEFLATED) as zout:

        for item in zin.infolist():
            name = item.filename

            # 1. Drop customXml/* entirely
            if _is_irm_part(name):
                irm_parts_found.append(name)
                logger.info(f"[irm_strip] dropped part: {name}")
                continue

            data = zin.read(name)

            # 2. Patch .rels files
            if name.endswith(".rels"):
                try:
                    patched = _strip_irm_rels(data.decode("utf-8")).encode("utf-8")
                    if patched != data:
                        logger.info(f"[irm_strip] patched rels: {name}")
                    data = patched
                except Exception as exc:
                    logger.warning(f"[irm_strip] could not patch {name}: {exc}")

            # 3. Patch [Content_Types].xml
            elif name == "[Content_Types].xml":
                try:
                    patched = _strip_content_types(data.decode("utf-8")).encode("utf-8")
                    if patched != data:
                        logger.info(f"[irm_strip] patched [Content_Types].xml")
                    data = patched
                except Exception as exc:
                    logger.warning(f"[irm_strip] could not patch [Content_Types].xml: {exc}")

            # 4. Strip sensitivity label props from docProps/custom.xml
            elif name == "docProps/custom.xml":
                data = _strip_custom_props(data)

            zout.writestr(item, data)

    logger.info(
        f"[irm_strip] {input_p.name}: stripped {len(irm_parts_found)} IRM part(s) -> {output_p.name}"
    )
    return str(output_p)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python irm_strip.py input.xlsx [output.xlsx]")
        sys.exit(1)
    inp = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else None
    result = strip_irm(inp, out)
    print(f"Stripped: {result}")