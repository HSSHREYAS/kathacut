"""PDF -> per-page text + embedded figures, using pypdfium2 (permissive licence)."""
import hashlib
import json
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as raw

MAX_FIGURES = 160


def parse(pdf_path, outdir, progress=None) -> dict:
    outdir = Path(outdir)
    cache = outdir / "source.json"
    if cache.exists():
        return json.loads(cache.read_text("utf-8"))
    figdir = outdir / "figures"
    figdir.mkdir(parents=True, exist_ok=True)
    pdf = pdfium.PdfDocument(str(pdf_path))
    pages, figures, seen = [], [], set()
    n = len(pdf)
    for i in range(n):
        page = pdf[i]
        try:
            tp = page.get_textpage()
            txt = tp.get_text_range() or ""
            tp.close()
        except Exception:
            txt = ""
        pages.append({"n": i + 1, "text": " ".join(txt.split())})
        if len(figures) < MAX_FIGURES:
            try:
                k = 0
                for obj in page.get_objects(filter=[raw.FPDF_PAGEOBJ_IMAGE], max_depth=3):
                    try:
                        pil = obj.get_bitmap(render=False).to_pil()
                    except Exception:
                        continue
                    w, h = pil.size
                    if min(w, h) < 280 or w * h < 160_000 or max(w / h, h / w) > 3.2:
                        continue
                    if pil.mode != "RGB":
                        pil = pil.convert("RGB")
                    digest = hashlib.sha1(pil.resize((32, 32)).tobytes()).hexdigest()
                    if digest in seen:
                        continue
                    seen.add(digest)
                    fid = f"f{i + 1}_{k}"
                    dst = figdir / f"{fid}.jpg"
                    pil.save(dst, quality=92)
                    figures.append({"id": fid, "page": i + 1, "file": f"figures/{fid}.jpg", "w": w, "h": h})
                    k += 1
            except Exception:
                pass
        page.close()
        if progress and i % 5 == 0:
            progress((i + 1) / n, f"Reading PDF page {i + 1}/{n}")
    pdf.close()
    if sum(len(p["text"]) for p in pages) < 200:
        raise RuntimeError("This PDF has no selectable text (it looks scanned). Run OCR on it first, "
                           "or paste the text instead.")
    out = {"pages": pages, "figures": figures}
    cache.write_text(json.dumps(out), "utf-8")
    return out
