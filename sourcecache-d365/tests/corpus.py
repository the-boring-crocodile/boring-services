"""A synthetic PackagesLocalDirectory with the real structure and no real code.

Every name and text is invented. What is real is the shape, and every trap
known from an earlier prototype: legacy `@SYS1` beside
`@SYS10` and `@SYS100`; new-style label keys referenced as `@File:Id`; label
files with a BOM; locale folders `en-US`, `en-us` and `en-Us`; CRLF; a
prefixed CamelCase name containing `AllowLocOverridePut`; the same element
name in two models; Microsoft, ISV and customer layers; binary files and a
`bin` folder that must be skipped.
"""

from __future__ import annotations

from pathlib import Path

BOM = "\ufeff"

MODELS = {
    # (package, model): (layer, publisher, version, references)
    ("ApplicationPlatform", "ApplicationPlatform"): (0, "Microsoft Corporation", (7, 0, 7701, 12), []),
    ("ApplicationSuite", "Foundation"): (0, "Microsoft Corporation", (10, 0, 2177, 31), ["ApplicationPlatform"]),
    ("WarehouseCore", "WarehouseCore"): (0, "Microsoft Corporation", (10, 0, 2177, 31),
                                         ["ApplicationPlatform", "ApplicationSuite"]),
    ("FabrikamWms", "FabrikamWms"): (8, "Fabrikam ISV", (3, 2, 0, 0), ["ApplicationSuite", "WarehouseCore"]),
    ("ContosoExtensions", "ContosoExtensions"): (12, "Contoso", (1, 0, 0, 0),
                                                 ["ApplicationSuite", "WarehouseCore", "FabrikamWms"]),
}


def descriptor(model: str, package: str, layer: int, publisher: str, version: tuple[int, int, int, int],
               references: list[str]) -> str:
    refs = "".join(f"\n    <d2p1:string>{r}</d2p1:string>" for r in references)
    major, minor, build, revision = version
    return f"""<?xml version="1.0" encoding="utf-8"?>
<AxModelInfo xmlns:i="http://www.w3.org/2001/XMLSchema-instance">
  <Customization>Allow</Customization>
  <Description>{model} (synthetic)</Description>
  <DisplayName>{model}</DisplayName>
  <Id>{sum(map(ord, model)) * 7919}</Id>
  <Layer>{layer}</Layer>
  <Locked>false</Locked>
  <ModelModule>{package}</ModelModule>
  <ModuleReferences xmlns:d2p1="http://schemas.microsoft.com/2003/10/Serialization/Arrays">{refs}
  </ModuleReferences>
  <Name>{model}</Name>
  <Publisher>{publisher}</Publisher>
  <VersionBuild>{build}</VersionBuild>
  <VersionMajor>{major}</VersionMajor>
  <VersionMinor>{minor}</VersionMinor>
  <VersionRevision>{revision}</VersionRevision>
</AxModelInfo>
"""


def element(kind: str, name: str, body: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<{kind} xmlns:i="http://www.w3.org/2001/XMLSchema-instance">
  <Name>{name}</Name>
{body}
</{kind}>
"""


ELEMENTS = {
    # path below PackagesLocalDirectory: text
    "ApplicationSuite/Foundation/AxClass/InventLocationValidator.xml": element("AxClass", "InventLocationValidator", """  <SourceCode>
    <Declaration><![CDATA[
class InventLocationValidator
{
    // Checks WHSAllowLocOverridePut before a put is confirmed.
    boolean check(WHSWorkLine _line)
    {
        if (!_line.WHSAllowLocOverridePut)
        {
            return checkFailed("@SYS100");
        }
        info(strFmt("@SYS1 %1", _line.ItemId));
        warning("@SYS10");
        return true;
    }
}
]]></Declaration>
  </SourceCode>"""),
    "ApplicationSuite/Foundation/AxTable/WHSPutRule.xml": element("AxTable", "WHSPutRule", """  <Label>@SYS10</Label>
  <Fields>
    <AxTableField xmlns="" i:type="AxTableFieldEnum">
      <Name>AllowLocOverridePut</Name>
      <ExtendedDataType>WHSLocOverride</ExtendedDataType>
    </AxTableField>
    <AxTableField xmlns="" i:type="AxTableFieldString">
      <Name>LimitText</Name>
      <Label>@WHSLabels:PutLimit</Label>
    </AxTableField>
  </Fields>"""),
    "ApplicationSuite/Foundation/AxEdt/WHSLocOverride.xml": element("AxEdt", "WHSLocOverride",
                                                                    "  <Label>@WHSLabels:AllowLocOverride</Label>"),
    "WarehouseCore/WarehouseCore/AxClass/WHSHelper.xml": element("AxClass", "WHSHelper", """  <SourceCode>
    <Declaration><![CDATA[
class WHSHelper
{
    public static str putLimitText() { return "@WHSLabels:PutLimit"; }
    public static str putLimitTextLong() { return "@WHSLabels:PutLimitExceeded"; }
}
]]></Declaration>
  </SourceCode>"""),
    "FabrikamWms/FabrikamWms/AxClass/FabrikamPutHandler.xml": element("AxClass", "FabrikamPutHandler", """  <SourceCode>
    <Declaration><![CDATA[
class FabrikamPutHandler
{
    // Fabrikam honours AllowLocOverridePut as well.
    void run() { info("@FabrikamLabels:PutDone"); }
}
]]></Declaration>
  </SourceCode>"""),
    "ContosoExtensions/ContosoExtensions/AxClass/WHSHelper.xml": element("AxClass", "WHSHelper", """  <SourceCode>
    <Declaration><![CDATA[
[ExtensionOf(classStr(WHSHelper))]
final class WHSHelper_Contoso_Extension
{
    // Contoso's own WHSHelper: same element name, other model.
    public static str putLimitText() { return "@ContosoLabels:PutOverride"; }
}
]]></Declaration>
  </SourceCode>"""),
    "ContosoExtensions/ContosoExtensions/AxTableExtension/WHSPutRule.ContosoExtensions.xml": element(
        "AxTableExtension", "WHSPutRule.ContosoExtensions", "  <Label>@SYS10</Label>"),
}

LABEL_FILES = {
    # path below PackagesLocalDirectory: (text, with BOM)
    "ApplicationSuite/Foundation/AxLabelFile/LabelResources/en-US/SYS.en-US.label.txt": (
        "@SYS1=Customer\n ;Short name of a customer.\n@SYS10=Customer account\n"
        "@SYS100=Location ID is not valid\n@SYS1000=Customer group\n", True),
    "ApplicationSuite/Foundation/AxLabelFile/LabelResources/de/SYS.de.label.txt": (
        "@SYS1=Debitor\n@SYS10=Debitorenkonto\n@SYS100=Lagerplatz-ID ist ungültig\n@SYS1000=Debitorengruppe\n",
        True),
    "WarehouseCore/WarehouseCore/AxLabelFile/LabelResources/en-us/WHSLabels.en-us.label.txt": (
        "AllowLocOverride=Allow location override\n ;Shown on the put rule.\nPutLimit=Put limit %1 exceeded\n"
        "PutLimitExceeded=Put limit exceeded for location %1\n", True),
    "FabrikamWms/FabrikamWms/AxLabelFile/LabelResources/en-Us/FabrikamLabels.en-Us.label.txt": (
        "PutDone=Put completed\n", False),
    "ContosoExtensions/ContosoExtensions/AxLabelFile/LabelResources/en-US/ContosoLabels.en-US.label.txt": (
        "PutOverride=Contoso location ID is not valid here\n", True),
}

BINARIES = {
    "ApplicationSuite/Foundation/Resources/icon.png": b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00",
    "ApplicationSuite/bin/Dynamics.AX.Foundation.dll": b"MZ\x90\x00\x03\x00\x00\x00 AllowLocOverridePut",
}


def write(root: Path) -> Path:
    """Write the corpus below `root/PackagesLocalDirectory` (CRLF, as on Windows)."""
    base = root / "PackagesLocalDirectory"
    for (package, model), (layer, publisher, version, references) in MODELS.items():
        path = base / package / "Descriptor" / f"{model}.xml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(descriptor(model, package, layer, publisher, version, references)
                         .replace("\n", "\r\n").encode("utf-8"))
    for relative, text in ELEMENTS.items():
        path = base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((BOM + text).replace("\n", "\r\n").encode("utf-8"))
    for relative, (text, with_bom) in LABEL_FILES.items():
        path = base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(((BOM if with_bom else "") + text).replace("\n", "\r\n").encode("utf-8"))
    for relative, data in BINARIES.items():
        path = base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return base


def text_files(base: Path) -> dict[str, str]:
    """The naive view every lookup is cross-checked against: path -> text with `\\n`."""
    found = {}
    for path in base.rglob("*"):
        if path.is_file() and path.suffix in (".xml", ".txt") and "bin" not in path.parts:
            found[path.relative_to(base).as_posix()] = (
                path.read_bytes().decode("utf-8-sig").replace("\r\n", "\n"))
    return found
