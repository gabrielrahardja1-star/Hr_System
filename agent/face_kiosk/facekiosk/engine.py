"""Detection + embedding, wrapping OpenCV's YuNet and SFace.

    engine = FaceEngine()
    faces = engine.detect(frame)          # list[Face], each with box + 5 landmarks
    emb   = engine.embed(frame, face)     # 128-d float32 vector
    sim   = FaceEngine.cosine(emb_a, emb_b)

The models are ONNX; OpenCV's DNN backend runs them on CPU. No GPU, no torch.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .config import SFACE_PATH, T, YUNET_PATH

# Quiet OpenCV 5.0's "Targets are not supported by the new graph engine" notice
# that fires on every model load; real errors still print.
try:
    cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
except AttributeError:  # pragma: no cover - older/newer cv2 layout
    pass

# YuNet row layout: x, y, w, h, then 5 (x, y) landmarks, then score = 15 floats.
# Landmark order: right eye, left eye, nose tip, right mouth corner, left mouth.
_LMK_SLICE = slice(4, 14)


@dataclass
class Face:
    box: tuple[int, int, int, int]      # x, y, w, h  (pixels, top-left origin)
    score: float
    landmarks: np.ndarray               # shape (5, 2), float32
    row: np.ndarray                     # raw 15-float row, needed by alignCrop

    @property
    def center(self) -> tuple[float, float]:
        x, y, w, h = self.box
        return (x + w / 2, y + h / 2)

    @property
    def size(self) -> int:
        return min(self.box[2], self.box[3])


class FaceEngine:
    def __init__(self, detect_score: float | None = None) -> None:
        for path in (YUNET_PATH, SFACE_PATH):
            if not path.exists():
                raise FileNotFoundError(
                    f"missing model {path.name}. Run: python -m facekiosk.fetch_models"
                )
        self._detector = cv2.FaceDetectorYN.create(
            str(YUNET_PATH),
            "",
            (320, 320),
            score_threshold=detect_score if detect_score is not None else T.detect_score,
            nms_threshold=T.detect_nms,
            top_k=T.detect_topk,
        )
        self._recognizer = cv2.FaceRecognizerSF.create(str(SFACE_PATH), "")
        self._input_size: tuple[int, int] = (0, 0)

    def detect(self, frame: np.ndarray) -> list[Face]:
        h, w = frame.shape[:2]
        if (w, h) != self._input_size:
            self._detector.setInputSize((w, h))
            self._input_size = (w, h)
        _, raw = self._detector.detect(frame)
        if raw is None:
            return []
        faces: list[Face] = []
        for row in raw:
            x, y, bw, bh = (int(round(v)) for v in row[:4])
            faces.append(
                Face(
                    box=(x, y, bw, bh),
                    score=float(row[-1]),
                    landmarks=row[_LMK_SLICE].reshape(5, 2).astype(np.float32),
                    row=row.astype(np.float32),
                )
            )
        return faces

    def embed(self, frame: np.ndarray, face: Face) -> np.ndarray:
        aligned = self._recognizer.alignCrop(frame, face.row)
        feat = self._recognizer.feature(aligned)
        return np.asarray(feat, dtype=np.float32).flatten().copy()

    @staticmethod
    def cosine(a: np.ndarray, b: np.ndarray) -> float:
        denom = float(np.linalg.norm(a) * np.linalg.norm(b))
        return float(np.dot(a, b) / denom) if denom else 0.0

    @staticmethod
    def cosine_batch(gallery: np.ndarray, probe: np.ndarray) -> np.ndarray:
        """Cosine similarity of `probe` (128,) against each row of `gallery` (N, 128)."""
        g_norm = np.linalg.norm(gallery, axis=1)
        p_norm = float(np.linalg.norm(probe))
        denom = g_norm * p_norm
        denom[denom == 0] = 1e-9
        return (gallery @ probe) / denom
