#!/usr/bin/env python3
"""Render `macos/Ghostdeck.app/Contents/Resources/Ghostdeck.icns`.

The launcher bundle had no icon, so Finder and the Dock showed the generic app glyph and there was
nothing to tell the ghostdeck window apart from any other Python app. The icon is drawn from the
window's own palette (INK background, LIME mark) and an SF Symbol, so there is no artwork to keep
in sync by hand. Re-run after changing it:

    ~/.ghostdeck/venv/bin/python tools/make_app_icon.py

Needs pyobjc (the `gui` extra) and `iconutil` (Xcode CLT).
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "macos" / "Ghostdeck.app" / "Contents" / "Resources" / "Ghostdeck.icns"
SYMBOL = "play.rectangle.fill"
# (file name, pixel size) as `iconutil` expects them in an .iconset.
SIZES = (
    ("icon_16x16.png", 16), ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32), ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024),
)


def render(size: int, path: Path) -> None:
    from AppKit import (
        NSBezierPath,
        NSBitmapImageRep,
        NSCalibratedRGBColorSpace,
        NSColor,
        NSCompositingOperationSourceOver,
        NSGraphicsContext,
        NSImage,
        NSImageSymbolConfiguration,
        NSMakeRect,
        NSPNGFileType,
    )

    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, size, size, 8, 4, True, False, NSCalibratedRGBColorSpace, 0, 0
    )
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
    # macOS app-icon grid: the rounded square sits inside a ~10% margin.
    inset = size * 0.1
    tile = NSMakeRect(inset, inset, size - 2 * inset, size - 2 * inset)
    NSColor.colorWithCalibratedRed_green_blue_alpha_(0.035, 0.035, 0.04, 1.0).setFill()
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(tile, size * 0.18, size * 0.18).fill()
    lime = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.784, 1.0, 0.278, 1.0)
    config = NSImageSymbolConfiguration.configurationWithPointSize_weight_(size * 0.42, 0.3)
    config = config.configurationByApplyingConfiguration_(
        NSImageSymbolConfiguration.configurationWithHierarchicalColor_(lime)
    )
    glyph = NSImage.imageWithSystemSymbolName_accessibilityDescription_(SYMBOL, None)
    if glyph is None:
        raise SystemExit(f"SF Symbol {SYMBOL!r} is not available on this macOS")
    glyph = glyph.imageWithSymbolConfiguration_(config)
    gw, gh = glyph.size().width, glyph.size().height
    glyph.drawInRect_fromRect_operation_fraction_(
        NSMakeRect((size - gw) / 2, (size - gh) / 2, gw, gh),
        NSMakeRect(0, 0, 0, 0),
        NSCompositingOperationSourceOver,
        1.0,
    )
    NSGraphicsContext.restoreGraphicsState()
    data = rep.representationUsingType_properties_(NSPNGFileType, {})
    if not data.writeToFile_atomically_(str(path), True):
        raise SystemExit(f"could not write {path}")


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        iconset = Path(scratch) / "Ghostdeck.iconset"
        iconset.mkdir()
        for name, size in SIZES:
            render(size, iconset / name)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(OUT)], check=True)
    print(OUT.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
