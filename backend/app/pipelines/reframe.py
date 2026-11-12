"""Face-tracked reframing: analyse a clip and return a crop track for the render engine."""
import cv2
import numpy as np

from .. import config, ffutil

_YUNET = ("https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
          "face_detection_yunet_2023mar.onnx")


class _Detector:
    def __init__(self):
        self.yn = None
        self.haar = None
        try:
            path = ffutil.download(_YUNET, config.MODELS / "yunet.onnx", timeout=60)
            self.yn = cv2.FaceDetectorYN.create(str(path), "", (320, 320), 0.72, 0.3, 50)
        except Exception:
            self.haar = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

    def detect(self, img) -> list[tuple]:
        """[(cx, cy, w, h)] normalised."""
        h, w = img.shape[:2]
        out = []
        if self.yn is not None:
            self.yn.setInputSize((w, h))
            _, faces = self.yn.detect(img)
            for f in faces if faces is not None else []:
                out.append(((f[0] + f[2] / 2) / w, (f[1] + f[3] / 2) / h, f[2] / w, f[3] / h))
        else:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            for (x, y, fw, fh) in self.haar.detectMultiScale(gray, 1.15, 5, minSize=(w // 16, w // 16)):
                out.append(((x + fw / 2) / w, (y + fh / 2) / h, fw / w, fh / h))
        return [f for f in out if f[2] > 0.035]


def _smooth(xs: np.ndarray, alpha: float = 0.18) -> np.ndarray:
    f = xs.copy()
    for i in range(1, len(f)):
        f[i] = f[i - 1] + alpha * (f[i] - f[i - 1])
    b = f.copy()
    for i in range(len(b) - 2, -1, -1):
        b[i] = b[i + 1] + alpha * (b[i] - b[i + 1])
    return b


def _shot_segment(samples: list[dict], t0: float, t1: float, crop_frac: float) -> dict:
    """Decide framing for one shot from its face samples."""
    n = len(samples)
    with_face = [s for s in samples if s["faces"]]
    dur = t1 - t0
    if len(with_face) < max(1, 0.3 * n):
        return {"t0": t0, "t1": t1, "mode": "center" if dur < 4 else "blur", "boost": 1.5}
    # cluster faces by horizontal position
    clusters: list[dict] = []
    for s in with_face:
        for f in s["faces"]:
            for c in clusters:
                if abs(c["cx"] - f[0]) < 0.14:
                    c["n"] += 1
                    c["area"] += f[2] * f[3]
                    c["cx"] += (f[0] - c["cx"]) / c["n"]
                    break
            else:
                clusters.append({"cx": f[0], "n": 1, "area": f[2] * f[3]})
    strong = [c for c in clusters if c["n"] >= 0.4 * len(with_face)]
    if len(strong) >= 2:
        lo, hi = min(c["cx"] for c in strong), max(c["cx"] for c in strong)
        if hi - lo > crop_frac * 0.72:   # people too far apart for one vertical crop
            return {"t0": t0, "t1": t1, "mode": "blur", "boost": 1.55}
        mid = (lo + hi) / 2
        return {"t0": t0, "t1": t1, "mode": "track", "keys": [[t0, mid], [t1, mid]]}
    main = max(clusters, key=lambda c: c["area"])
    ts, xs = [], []
    for s in with_face:
        near = [f for f in s["faces"] if abs(f[0] - main["cx"]) < 0.2]
        if near:
            best = max(near, key=lambda f: f[2] * f[3])
            ts.append(s["t"])
            xs.append(best[0])
    if not xs:
        return {"t0": t0, "t1": t1, "mode": "center"}
    all_t = np.array([s["t"] for s in samples])
    x = _smooth(np.interp(all_t, ts, xs))
    if x.max() - x.min() < 0.05:        # nearly still: lock the camera off
        m = float(np.median(x))
        return {"t0": t0, "t1": t1, "mode": "track", "keys": [[t0, m], [t1, m]]}
    return {"t0": t0, "t1": t1, "mode": "track",
            "keys": [[round(float(t), 3), round(float(v), 4)] for t, v in zip(all_t, x)]}


def analyze(video_path, target_aspect: float, sample_fps: float = 6.0, progress=None) -> dict:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return {"mode": "center"}
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    sw, sh = cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    if not sw or not sh or (sw / sh) <= target_aspect * 1.08:
        cap.release()
        return {"mode": "center"}
    crop_frac = (sh * target_aspect) / sw
    step = max(1, int(round(fps / sample_fps)))
    det = _Detector()
    samples, cuts, prev_small = [], [], None
    i = 0
    while True:
        if not cap.grab():
            break
        if i % step == 0:
            ok, fr = cap.retrieve()
            if not ok:
                break
            small = cv2.resize(fr, (480, int(480 * sh / sw)), interpolation=cv2.INTER_AREA)
            tiny = cv2.cvtColor(cv2.resize(small, (48, 27)), cv2.COLOR_BGR2GRAY).astype(np.int16)
            t = i / fps
            if prev_small is not None and float(np.abs(tiny - prev_small).mean()) > 26:
                cuts.append(t)
            prev_small = tiny
            samples.append({"t": t, "faces": det.detect(small)})
            if progress and total and len(samples) % 20 == 0:
                progress(i / total, "Tracking faces")
        i += 1
    cap.release()
    if not samples:
        return {"mode": "center"}
    end = i / fps
    bounds = [0.0] + cuts + [end + 1.0]
    segments = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        shot = [s for s in samples if a <= s["t"] < b]
        if shot:
            segments.append(_shot_segment(shot, a, b, crop_frac))
    return {"segments": segments or [{"t0": 0, "t1": 1e9, "mode": "center"}]}
