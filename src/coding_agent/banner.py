"""CRT retro terminal startup banner."""

from __future__ import annotations

from typing import Callable


# ---------------------------------------------------------------------------
# ASCII art styles
# ---------------------------------------------------------------------------

# Style A: Phosphor Block (default) — full-block + box-drawing, ~44 chars wide
_ART_PHOSPHOR = [
    r"███████╗ ██████╗ ██████╗  ██████╗ ███████╗    ██████╗ ██████╗ ██████╗ ███████╗",
    r"██╔════╝██╔═══██╗██╔══██╗██╔════╝ ██╔════╝   ██╔════╝██╔═══██╗██╔══██╗██╔════╝",
    r"█████╗  ██║   ██║██████╔╝██║  ███╗█████╗     ██║     ██║   ██║██║  ██║█████╗  ",
    r"██╔══╝  ██║   ██║██╔══██╗██║   ██║██╔══╝     ██║     ██║   ██║██║  ██║██╔══╝  ",
    r"██║     ╚██████╔╝██║  ██║╚██████╔╝███████╗   ╚██████╗╚██████╔╝██████╔╝███████╗",
    r"╚═╝      ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚══════╝    ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝",
]

# Style B: Half-Block Compact — for narrow terminals (<60 cols), ~40 chars wide
_ART_COMPACT = [
    "\u2588\u2580\u2580 \u2588\u2580\u2588 \u2588\u2580\u2588 \u2588\u2580\u2580 \u2588\u2580\u2580"
    "  \u2588\u2580\u2580 \u2588\u2580\u2588 \u2588\u2580\u2584 \u2588\u2580\u2580",
    "\u2588\u2580  \u2588 \u2588 \u2588\u2580\u2584 \u2588 \u2588 \u2588\u2580 "
    "  \u2588   \u2588 \u2588 \u2588 \u2588 \u2588\u2580 ",
    "\u2580   \u2580\u2580\u2580 \u2580 \u2580 \u2580\u2580\u2580 \u2580\u2580\u2580"
    "  \u2580\u2580\u2580 \u2580\u2580\u2580 \u2580\u2580  \u2580\u2580\u2580",
]

# Style C: Pure ASCII — no Unicode, piped/no-color output
_ART_ASCII = [
    " _____  ___  ____   ____ _____    ____ ___  ____  _____",
    "|  ___// _ \\|  _ \\ / ___| ____|   / ___/ _ \\|  _ \\| ____|",
    "| |_  | | | | |_) | |  _|  _|   | |  | | | | | | |  _|",
    "|  _| | |_| |  _ <| |_| | |___  | |__| |_| | |_| | |___",
    "|_|    \\___/|_| \\_\\\\____|_____|   \\____\\___/|____/|_____|",
]


# ---------------------------------------------------------------------------
# Color palette — keyed by color level (0=none, 1=basic, 2=256, 3=truecolor)
# ---------------------------------------------------------------------------

_PALETTE: dict[int, dict[str, str]] = {
    0: {k: "" for k in [
        "GLOW_BRIGHT", "GLOW_MED", "GLOW_DIM", "LABEL", "VALUE",
        "BORDER", "SCANLINE", "VERSION", "HINT",
    ]},
    1: {
        "GLOW_BRIGHT": "1;32",
        "GLOW_MED":    "32",
        "GLOW_DIM":    "2;32",
        "LABEL":       "32",
        "VALUE":       "37",
        "BORDER":      "2;32",
        "SCANLINE":    "2;32",
        "VERSION":     "2;37",
        "HINT":        "2;36",
    },
    2: {
        "GLOW_BRIGHT": "1;38;5;82",
        "GLOW_MED":    "38;5;40",
        "GLOW_DIM":    "38;5;22",
        "LABEL":       "38;5;34",
        "VALUE":       "38;5;250",
        "BORDER":      "38;5;28",
        "SCANLINE":    "2;38;5;22",
        "VERSION":     "2;38;5;242",
        "HINT":        "2;38;5;37",
    },
    3: {
        "GLOW_BRIGHT": "1;38;2;57;255;20",
        "GLOW_MED":    "38;2;0;200;0",
        "GLOW_DIM":    "38;2;0;100;0",
        "LABEL":       "38;2;0;170;0",
        "VALUE":       "38;2;200;200;200",
        "BORDER":      "38;2;0;120;0",
        "SCANLINE":    "2;38;2;0;80;0",
        "VERSION":     "2;38;2;130;130;130",
        "HINT":        "2;38;2;0;150;150",
    },
}


def _pad(text: str, width: int) -> str:
    """Pad *text* with spaces on the right to fill *width* characters."""
    return text + " " * max(0, width - len(text))


def format_banner(
    *,
    version: str,
    model_name: str,
    provider: str,
    max_context_tokens: int,
    working_directory: str,
    dangerous_mode: str,
    session_id: str | None = None,
    resumed_tokens: int = 0,
    resumed_turns: int = 0,
    color_level: int = 0,
    colorize: Callable[[str, str], str] = lambda code, text: text,
    terminal_width: int = 80,
    banner_width: int = 80,
) -> list[str]:
    """Build the CRT retro startup banner.

    Returns a list of ready-to-print strings (one per line, no trailing
    newlines).  The *colorize* callback wraps ``(ansi_code, text)`` and
    is expected to be ``AgentIO._c``.
    """
    palette = _PALETTE.get(min(color_level, 3), _PALETTE[0])

    def c(role: str, text: str) -> str:
        code = palette.get(role, "")
        if not code:
            return text
        return colorize(code, text)

    # -- select art style ----------------------------------------------------
    if color_level == 0:
        art = _ART_ASCII
    elif terminal_width < 60:
        art = _ART_COMPACT
    else:
        art = _ART_PHOSPHOR

    # -- compute frame dimensions --------------------------------------------
    art_max = max(len(line) for line in art)
    # Content area: 3-char left pad + art + 3-char right pad minimum
    min_inner = art_max + 6
    # Also account for field lines: "   Model     : <value>   "
    sample_field = f"   Sandbox   : {working_directory}   "
    min_inner = max(min_inner, len(sample_field))
    # Clamp to terminal width (outer = inner + 2 for border chars)
    inner_w = min(max(min_inner, banner_width), terminal_width - 4)

    # -- border helpers ------------------------------------------------------
    if color_level > 0:
        h_char = "\u2550"  # ═
        tl, tr = "\u2554", "\u2557"  # ╔ ╗
        bl, br = "\u255a", "\u255d"  # ╚ ╝
        v_char = "\u2551"  # ║
        scan_unit = "\u2500\u2500 "  # ── (dash dash space)
    else:
        h_char = "="
        tl = tr = bl = br = "+"
        v_char = "|"
        scan_unit = "-- "

    top = c("BORDER", tl + h_char * inner_w + tr)
    bot = c("BORDER", bl + h_char * inner_w + br)

    def row(content: str, pad_w: int | None = None) -> str:
        """Wrap *content* in a bordered row, right-padded to inner_w."""
        if pad_w is None:
            pad_w = inner_w
        padded = content + " " * max(0, pad_w - len(content))
        return c("BORDER", v_char) + padded + c("BORDER", v_char)

    def blank() -> str:
        return row("", inner_w)

    def scanline() -> str:
        count = max(1, (inner_w - 6) // len(scan_unit))
        line = "   " + scan_unit * count
        return row(c("SCANLINE", _pad(line, inner_w)), inner_w)

    def field(label: str, value: str) -> str:
        lbl = f"   {label:<10s}: "
        val = value
        content = c("LABEL", lbl) + c("VALUE", val)
        # Compute the raw (uncolored) length for padding
        raw_len = len(lbl) + len(val)
        pad_needed = max(0, inner_w - raw_len)
        return c("BORDER", v_char) + content + " " * pad_needed + c("BORDER", v_char)

    # -- build lines ---------------------------------------------------------
    lines: list[str] = []
    lines.append(top)

    # ASCII art, centered within frame
    for art_line in art:
        left_pad = max(0, (inner_w - art_max) // 2)
        padded_art = " " * left_pad + _pad(art_line, art_max)
        padded_art = _pad(padded_art, inner_w)
        lines.append(
            c("BORDER", v_char)
            + c("GLOW_BRIGHT", padded_art)
            + c("BORDER", v_char)
        )

    # Version, right-aligned
    ver_str = f"v{version}"
    ver_line = " " * (inner_w - len(ver_str) - 3) + ver_str + "   "
    lines.append(
        c("BORDER", v_char)
        + c("VERSION", ver_line)
        + c("BORDER", v_char)
    )

    lines.append(scanline())

    # Status fields
    lines.append(field("Model", model_name))
    lines.append(field("Provider", provider))
    lines.append(field("Context", f"{max_context_tokens:,} tokens"))
    lines.append(field("Sandbox", working_directory))
    lines.append(field("Mode", dangerous_mode))

    if session_id is not None:
        lines.append(field("Session", session_id))

    if resumed_tokens > 0:
        lines.append(field("Resumed", f"{resumed_tokens:,} tokens ({resumed_turns} turns)"))

    lines.append(scanline())

    # Hint line
    hint_text = "   Type / for commands, /help for details, /quit to exit"
    hint_padded = _pad(hint_text, inner_w)
    lines.append(
        c("BORDER", v_char)
        + c("HINT", hint_padded)
        + c("BORDER", v_char)
    )

    lines.append(bot)

    return lines
