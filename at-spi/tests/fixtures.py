"""
tests/fixtures.py -- hand-built AT-SPI trees for labeling_store's pathological cases.

These are NOT captures. A capture is 0.5-5 MB of real markup in which the interesting node is one
row among thousands, which makes it a poor regression test: it is slow, it changes whenever the
site changes, and when it breaks it does not say what broke. Each tree here isolates exactly one
decision, so a failure names the rule that regressed.

The XML dialect is OSWorld's a11y-tree serialization: element tag = AT-SPI role with spaces as
dashes, `name` = accessible name, and namespaced attributes for state/geometry/DOM.
"""

NS = (
    'xmlns:st="https://accessibility.ubuntu.example.org/ns/state" '
    'xmlns:attr="https://accessibility.ubuntu.example.org/ns/attributes" '
    'xmlns:cp="https://accessibility.ubuntu.example.org/ns/component"'
)

_SHOWING = 'st:showing="true" st:visible="true" st:enabled="true" st:sensitive="true"'


def node(role: str, x: int, y: int, w: int, h: int, name: str = "", *,
         cls: str = None, node_id: str = None, name_from: str = None,
         children: str = "", extra: str = "") -> str:
    """One element. Geometry is `cp:screencoord`/`cp:size`, the pair _node_bbox() reads."""
    tag = role.replace(" ", "-")
    bits = [_SHOWING, f'cp:screencoord="({x}, {y})"', f'cp:size="({w}, {h})"']
    if name:
        bits.append(f'name="{name}"')
    if cls is not None:
        bits.append(f'attr:class="{cls}"')
    if node_id is not None:
        bits.append(f'attr:id="{node_id}"')
    if name_from is None and name:
        name_from = "contents"
    if name_from is not None:
        bits.append(f'attr:name-from="{name_from}"')
    if extra:
        bits.append(extra)
    attrs = " ".join(bits)
    return f"<{tag} {attrs}>{children}</{tag}>" if children else f"<{tag} {attrs}/>"


def page(body: str, *, w: int = 1850, h: int = 966, x: int = 70, y: int = 114) -> str:
    """A full desktop frame wrapping one document-web content root, which is what
    resolve_content_roots() looks for on a web capture."""
    doc = node("document web", x, y, w, h, "Test page", name_from="related-element", children=body)
    return (f'<desktop-frame {NS}>'
            f'<application name="gsd-color"/>'
            f'<application name="Google Chrome">'
            f'<frame {_SHOWING} name="Test page - Google Chrome" '
            f'cp:screencoord="(0, 0)" cp:size="(1920, 1080)">{doc}</frame>'
            f'</application></desktop-frame>')


def frame_page(inner: str, *, fx: int = 70, fy: int = 114, fw: int = 1850, fh: int = 966) -> str:
    """A page containing a nested cross-origin frame (a second document-web), the shape ad
    SafeFrames and consent managers arrive in."""
    sub = node("document web", fx, fy, fw, fh, "", children=inner)
    return page(node("section", fx, fy, fw, fh, "", cls="ad-slot", children=sub))
