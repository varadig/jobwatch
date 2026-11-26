"""Builds static/material-symbols.woff2: the Material Symbols Outlined icons jobwatch uses, and nothing else.

    pip install fonttools brotli
    python tools/make_symbols.py "MaterialSymbolsOutlined[FILL,GRAD,opsz,wght].ttf"

The font comes from Google Fonts (Material Symbols Outlined, Apache License 2.0). The icons are the ICONS table in
jobwatch/web/components.py: add one there, then run this again. Each icon is drawn from its own code point, so the ligature table is
dropped; the weight, grade and optical size are fixed, and only the FILL axis is kept (filled icons: class "ms fill").
"""
import ast
import os
import sys

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "static", "material-symbols.woff2")


def icons():
    tree = ast.parse(open(os.path.join(HERE, "jobwatch", "web", "components.py"), encoding="utf-8").read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "ICONS" for t in node.targets):
            return ast.literal_eval(node.value)
    raise SystemExit("No ICONS table in jobwatch/web/components.py")


def main(src):
    table = icons()
    font = TTFont(src)
    cmap = font.getBestCmap()
    missing = [n for n, cp in table.items() if cp not in cmap]
    if missing:
        raise SystemExit("Not in this font: %s" % ", ".join(missing))
    opts = subset.Options()
    opts.layout_features = []
    opts.name_IDs = ["*"]
    opts.notdef_outline = True
    sub = subset.Subsetter(opts)
    sub.populate(unicodes=sorted(set(table.values())))
    sub.subset(font)
    axes = {a.axisTag for a in font["fvar"].axes} if "fvar" in font else set()
    pin = {k: v for k, v in {"wght": 400, "GRAD": 0, "opsz": 20}.items() if k in axes}
    if pin:
        font = instancer.instantiateVariableFont(font, pin)
    font.flavor = "woff2"
    font.save(OUT)
    print("%s: %d icons, %d bytes" % (OUT, len(table), os.path.getsize(OUT)))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
