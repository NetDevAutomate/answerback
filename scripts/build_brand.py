"""Build the Answerback identity from its hand-made sources; no network access.

Sources, all in assets/: mark.svg (the Tally mark), wordmark.svg and tagline.svg (lettering
already converted to outlines, so neither this script nor a viewer needs the font).
Every other file in assets/, and the monitor's favicons, is generated here.
"""

import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
# The monitor serves WEB; LEGACY_WEB is the copy kept readable under the old package
# name. A test requires the two folders to hold equal files, so both are written.
WEB = ROOT / "src/answerback_bridge/bus/assets"
LEGACY_WEB = ROOT / "src/codex_herdr_bridge/bus/assets"
WEB_FOLDERS = (WEB, LEGACY_WEB)

XMLNS = 'xmlns="http://www.w3.org/2000/svg"'
LABEL = 'role="img" aria-label="Answerback"'
TILE, INK, PAPER, MUTED, NIGHT = "#a5dfb7", "#17221e", "#eef2f6", "#92a0b0", "#101317"
# Lockup geometry in mark units. The mark is 64 square and its glyph spans 12 to 52, so the
# 40-unit-high wordmark sits exactly between the glyph's top and bottom edges.
PAD, GAP, WORD_W = 12, 20, 302
LOGO_W, LOGO_H = PAD + 64 + GAP + WORD_W + PAD, PAD + 64 + PAD
ICON_SIZES = (16, 32, 64, 256)
FRAME = "<style>body{margin:0}svg{display:block;width:100vw;height:100vh}</style>"


def inner(name: str) -> str:
    """Return the markup inside the root element of a source SVG."""
    text = (ASSETS / name).read_text()
    return text[text.index(">") + 1 : text.rindex("</svg>")].strip()


def lockup(mark: str, word: str, colour: str) -> str:
    """Place the mark and the wordmark side by side, in the lockup's own coordinates."""
    return (
        f'<g transform="translate({PAD} {PAD})">{mark}</g>'
        f'<g transform="translate({PAD + 64 + GAP} {PAD + 12})" fill="{colour}">{word}</g>'
    )


def svgs() -> dict[Path, str]:
    """Compose every generated SVG from the three sources."""
    mark, word, tagline = inner("mark.svg"), inner("wordmark.svg"), inner("tagline.svg")
    halves = mark[mark.index("/>") + 2 :].strip()  # the glyph without its tile
    favicon = (ASSETS / "mark.svg").read_text()
    files = {folder / "favicon.svg": favicon for folder in WEB_FOLDERS}
    for name, colour in (("logo.svg", INK), ("logo-dark.svg", PAPER)):
        files[ASSETS / name] = (
            f'<svg {XMLNS} viewBox="0 0 {LOGO_W} {LOGO_H}" {LABEL}>'
            f"{lockup(mark, word, colour)}</svg>\n"
        )
    # Full-bleed square for avatars. GitHub rounds or circles it, so the glyph is drawn smaller.
    files[ASSETS / "avatar.svg"] = (
        f'<svg {XMLNS} viewBox="0 0 64 64" {LABEL}><rect width="64" height="64" fill="{TILE}"/>'
        f'<g transform="translate(4.8 4.8) scale(0.85)">{halves}</g></svg>\n'
    )
    # Social preview: the lockup at 2.5x, centred, with the tagline set to the same width.
    files[ASSETS / "social-preview.svg"] = (
        f'<svg {XMLNS} width="1280" height="640" viewBox="0 0 1280 640" {LABEL}>'
        f'<rect width="1280" height="640" fill="{NIGHT}"/>'
        f'<g transform="translate(127.5 162) scale(2.5)">{lockup(mark, word, PAPER)}'
        f'<g transform="translate({PAD} {PAD + 64 + GAP})" fill="{MUTED}">{tagline}</g></g></svg>\n'
    )
    return files


def shot(page, svg: str, width: int, height: int, path: Path, *, opaque: bool = False) -> bytes:
    """Screenshot one SVG at the given CSS size and save it as a PNG."""
    page.set_viewport_size({"width": width, "height": height})
    page.set_content(FRAME + svg)
    png = page.screenshot(omit_background=not opaque)
    path.write_bytes(png)
    return png


def write_icon(icon: bytes) -> None:
    """Save the ICO in assets/ and, with the same bytes, in both web folders."""
    for folder in (ASSETS, *WEB_FOLDERS):
        folder.joinpath("favicon.ico").write_bytes(icon)


def write_svgs(files: dict[Path, str]) -> None:
    """Save every generated SVG at its path."""
    for path, text in files.items():
        path.write_text(text)


def rasterise(files: dict[Path, str]) -> None:
    """Render the PNG and ICO files with headless Chromium."""
    from playwright.sync_api import sync_playwright

    mark = files[WEB / "favicon.svg"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(device_scale_factor=1)
            images = []
            for size in ICON_SIZES:
                png = shot(page, mark, size, size, ASSETS / f"icon-{size}.png")
                if size <= 64:
                    images.append((size, png))
            offset = 6 + 16 * len(images)
            directory = b""
            for size, image in images:
                directory += struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(image), offset)
                offset += len(image)
            icon = struct.pack("<HHH", 0, 1, len(images)) + directory
            icon += b"".join(image for _, image in images)
            write_icon(icon)
            shot(page, files[ASSETS / "avatar.svg"], 512, 512, ASSETS / "avatar.png", opaque=True)
            social = files[ASSETS / "social-preview.svg"]
            shot(page, social, 1280, 640, ASSETS / "social-preview.png", opaque=True)
            sharp = browser.new_page(device_scale_factor=4)
            for name in ("logo", "logo-dark"):
                shot(sharp, files[ASSETS / f"{name}.svg"], LOGO_W, LOGO_H, ASSETS / f"{name}.png")
        finally:
            browser.close()


def main() -> None:
    files = svgs()
    write_svgs(files)
    rasterise(files)


if __name__ == "__main__":
    main()
