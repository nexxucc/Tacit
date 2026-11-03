"""Build the dependency-free local VS Code extension as a VSIX archive."""

import argparse
import json
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile


def build(output: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "editors/vscode"
    package = json.loads((source / "package.json").read_text())
    namespace = "http://schemas.microsoft.com/developer/vsx-schema/2011"
    manifest = ET.Element("PackageManifest", {"Version": "2.0.0", "xmlns": namespace})
    metadata = ET.SubElement(manifest, "Metadata")
    ET.SubElement(
        metadata,
        "Identity",
        {
            "Language": "en-US",
            "Id": package["name"],
            "Version": package["version"],
            "Publisher": package["publisher"],
        },
    )
    ET.SubElement(metadata, "DisplayName").text = package["displayName"]
    ET.SubElement(metadata, "Description").text = package["description"]
    ET.SubElement(metadata, "Categories").text = "Other"
    properties = ET.SubElement(metadata, "Properties")
    ET.SubElement(
        properties,
        "Property",
        {
            "Id": "Microsoft.VisualStudio.Code.Engine",
            "Value": package["engines"]["vscode"],
        },
    )
    ET.SubElement(
        properties,
        "Property",
        {
            "Id": "Microsoft.VisualStudio.Code.ExtensionKind",
            "Value": "workspace",
        },
    )
    installation = ET.SubElement(manifest, "Installation")
    ET.SubElement(installation, "InstallationTarget", {"Id": "Microsoft.VisualStudio.Code"})
    ET.SubElement(manifest, "Dependencies")
    assets = ET.SubElement(manifest, "Assets")
    ET.SubElement(
        assets,
        "Asset",
        {
            "Type": "Microsoft.VisualStudio.Code.Manifest",
            "Path": "extension/package.json",
            "Addressable": "true",
        },
    )
    content = ET.Element(
        "Types", {"xmlns": "http://schemas.openxmlformats.org/package/2006/content-types"}
    )
    for extension, mime in [
        ("json", "application/json"),
        ("js", "application/javascript"),
        ("vsixmanifest", "text/xml"),
    ]:
        ET.SubElement(content, "Default", {"Extension": extension, "ContentType": mime})
    # Exclusive creation prevents accidental replacement of a prior release.
    with output.open("xb") as stream, ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        archive.writestr("extension.vsixmanifest", ET.tostring(manifest, encoding="utf-8"))
        archive.writestr("[Content_Types].xml", ET.tostring(content, encoding="utf-8"))
        for name in ("package.json", "extension.js"):
            archive.write(source / name, "extension/" + name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    build(parser.parse_args().output)
