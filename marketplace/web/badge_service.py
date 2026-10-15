"""
Dynamic SVG Badge Generator for Talos plugins and builders.
Produces clean, Shields.io/Raycast-compatible SVG badges for GitHub READMEs,
documentation, and social embeds with sub-millisecond response time.
"""

def generate_svg_badge(
    label: str,
    message: str,
    color: str = "#4f46e5",
    label_color: str = "#1e293b",
    logo_svg: str | None = None
) -> str:
    """
    Renders an accessible, pixel-crisp SVG badge adhering to standard flat badge specs.
    
    label: Left title (e.g. 'talos plugin', 'rating', 'verified builder')
    message: Right value (e.g. 'v1.2.0', '★ 4.9', 'installed')
    color: Right background color hex
    label_color: Left background color hex
    """
    # Character width heuristics (Verdana 11px)
    def calc_width(text: str) -> int:
        return max(24, int(len(text) * 7.0 + 14))

    label_w = calc_width(label)
    msg_w = calc_width(message)
    total_w = label_w + msg_w
    height = 20

    label_x = label_w / 2
    msg_x = label_w + (msg_w / 2)

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{total_w}" height="{height}" role="img" aria-label="{label}: {message}">
  <title>{label}: {message}</title>
  <linearGradient id="s" x2="0" y2="100%">
    <stop offset="0" stop-color="#bbb" stop-opacity=".1"/>
    <stop offset="1" stop-opacity=".1"/>
  </linearGradient>
  <clipPath id="r">
    <rect width="{total_w}" height="{height}" rx="3.5" fill="#fff"/>
  </clipPath>
  <g clip-path="url(#r)">
    <rect width="{label_w}" height="{height}" fill="{label_color}"/>
    <rect x="{label_w}" width="{msg_w}" height="{height}" fill="{color}"/>
    <rect width="{total_w}" height="{height}" fill="url(#s)"/>
  </g>
  <g fill="#fff" text-anchor="middle" font-family="-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif" text-rendering="geometricPrecision" font-size="11">
    <text aria-hidden="true" x="{label_x}" y="15" fill="#010101" fill-opacity=".3">{label}</text>
    <text x="{label_x}" y="14" fill="#f8fafc">{label}</text>
    <text aria-hidden="true" x="{msg_x}" y="15" fill="#010101" fill-opacity=".3">{message}</text>
    <text x="{msg_x}" y="14" font-weight="600" fill="#ffffff">{message}</text>
  </g>
</svg>"""
    return svg.strip()
