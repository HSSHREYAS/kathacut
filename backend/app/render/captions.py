"""Animated word-by-word captions: page grouping, per-word highlight, pop-in."""
import bisect

from . import text

STYLES = {
    # big uppercase, active word coloured
    "pop": {"label": "Pop (viral)", "font": "poppins-xb", "size": 0.088, "upper": True, "stroke": 0.14,
            "mode": "active", "max_words": 3, "max_chars": 16},
    # active word sits on a coloured box
    "box": {"label": "Box highlight", "font": "poppins-xb", "size": 0.08, "upper": True, "stroke": 0.12,
            "mode": "box", "max_words": 3, "max_chars": 16},
    # condensed tall type, good for horror / drama
    "impact": {"label": "Tall drama", "font": "anton", "size": 0.105, "upper": True, "stroke": 0.1,
               "mode": "active", "max_words": 2, "max_chars": 14},
    # sentence-style karaoke for documentaries
    "karaoke": {"label": "Karaoke line", "font": "poppins-sb", "size": 0.058, "upper": False, "stroke": 0.12,
                "mode": "karaoke", "max_words": 6, "max_chars": 30},
}


def _clean(w: str, upper: bool) -> str:
    w = w.strip().strip('"“”')
    if upper:
        w = w.rstrip(",.;:").upper()
    return w


def build_pages(words: list[dict], max_words: int, max_chars: int, gap: float = 0.55) -> list[dict]:
    pages, cur = [], []
    for w in words:
        if not str(w.get("w", "")).strip():
            continue
        if cur:
            chars = sum(len(x["w"]) for x in cur) + len(w["w"])
            ends = cur[-1]["w"].strip()[-1:] in ".?!"
            # let a sentence's last word join a full page rather than sit alone on the next one
            closes = w["w"].strip()[-1:] in ".?!" and len(cur) == max_words and chars <= max_chars * 1.25
            full = len(cur) >= max_words and not closes
            if full or (chars > max_chars and not closes) or w["t0"] - cur[-1]["t1"] > gap or ends:
                pages.append(cur)
                cur = []
        cur.append(w)
    if cur:
        pages.append(cur)
    out = []
    for i, pg in enumerate(pages):
        t0, t1 = pg[0]["t0"], pg[-1]["t1"]
        if i + 1 < len(pages):
            nxt = pages[i + 1][0]["t0"]
            t1 = nxt if nxt - t1 < 0.4 else t1 + 0.15
        else:
            t1 += 0.25
        out.append({"t0": t0, "t1": t1, "words": pg})
    return out


class CaptionTrack:
    def __init__(self, spec: dict, W: int, H: int):
        st = dict(STYLES.get(spec.get("style", "pop"), STYLES["pop"]))
        self.st = st
        self.W, self.H = W, H
        vertical = H > W
        base = min(W, H) * (1.0 if vertical else 0.72)
        self.size = max(14, int(base * st["size"] * float(spec.get("scale", 1.0))))
        self.stroke = max(2, int(self.size * st["stroke"]))
        self.max_width = int(W * (0.84 if vertical else 0.7))
        self.highlight = spec.get("highlight", "#FFE14A")
        self.color = spec.get("color", "#FFFFFF")
        self.cy = int(H * float(spec.get("position", 0.68 if vertical else 0.84)))
        max_words = int(spec.get("max_words") or st["max_words"])
        max_chars = st["max_chars"] if vertical else int(st["max_chars"] * 1.8)
        if not vertical and st["mode"] != "karaoke":
            max_words += 1
        self.pages = build_pages(spec.get("words", []), max_words, max_chars)
        self.starts = [p["t0"] for p in self.pages]
        self._cache_page = -1
        self._cache: dict = {}

    def _sprite(self, pi: int, active: int):
        if pi != self._cache_page:
            self._cache, self._cache_page = {}, pi
        if active not in self._cache:
            st = self.st
            ws = [_clean(w["w"], st["upper"]) for w in self.pages[pi]["words"]]
            mode = st["mode"]
            if mode == "karaoke":
                cols = [self.highlight if i <= active else self.color for i in range(len(ws))]
            elif mode == "box":
                cols = ["#111111" if i == active else self.color for i in range(len(ws))]
            else:
                cols = [self.highlight if i == active else self.color for i in range(len(ws))]
            self._cache[active] = text.words_sprite(
                ws, cols, st["font"], self.size, self.max_width, stroke=self.stroke,
                box_idx=active if (mode == "box" and active >= 0) else None, box_color=self.highlight)
        return self._cache[active]

    def draw(self, frame, t: float) -> None:
        pi = bisect.bisect_right(self.starts, t) - 1
        if pi < 0:
            return
        pg = self.pages[pi]
        if t >= pg["t1"]:
            return
        active = -1
        for i, w in enumerate(pg["words"]):
            if t >= w["t0"]:
                active = i
        dt = t - pg["t0"]
        scale = 0.82 + 0.18 * min(1.0, dt / 0.09) if self.st["mode"] != "karaoke" else 1.0
        text.blit_center(frame, self._sprite(pi, active), self.W // 2, self.cy, 1.0, scale)
