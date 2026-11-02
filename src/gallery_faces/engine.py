"""SCRFD face detection + ArcFace embeddings with onnxruntime (InsightFace model packs).

Model packs (``ODYSSEUS_GALLERY_FACE_MODEL``):

* ``antelopev2`` (default) — SCRFD-10G detector + ArcFace ResNet-100 (Glint360K), the most accurate.
* ``buffalo_l`` — same detector + ArcFace ResNet-50 (WebFace600K), faster.

Both are fetched once from Hugging Face (``immich-app/<pack>``) into the HF cache.
The pre/post-processing mirrors ``insightface.model_zoo.scrfd`` / ``arcface_onnx``
and ``insightface.utils.face_align`` so embeddings match the reference pipeline.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass

import numpy as np
from PIL import Image

MODEL_PACK = os.getenv("ODYSSEUS_GALLERY_FACE_MODEL", "antelopev2").strip() or "antelopev2"
DET_SIZE = 640                 # SCRFD input (square, letterboxed top-left)
DET_THRESH = 0.5
NMS_THRESH = 0.4
_STRIDES = (8, 16, 32)
_ANCHORS_PER_POSITION = 2
WORK_MAX_SIDE = 1920           # detect/align on at most this size (big photos are downscaled once)

# ArcFace 112x112 alignment template (insightface.utils.face_align.arcface_dst)
ARCFACE_DST = np.array(
    [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366], [41.5493, 92.3655], [70.7299, 92.2041]],
    dtype=np.float32,
)


@dataclass
class Face:
    box: tuple[float, float, float, float]   # x1, y1, x2, y2 in pixels of the analysed image
    score: float
    landmarks: np.ndarray                    # (5, 2) eyes, nose, mouth corners
    embedding: np.ndarray | None = None      # (512,) L2-normalised


class FaceEngine:
    """Lazily loaded, thread-safe detector + recognizer."""

    def __init__(self, model_pack: str = MODEL_PACK):
        self.model_pack = model_pack
        self._lock = threading.Lock()
        self._det = None
        self._rec = None

    # -- model loading -------------------------------------------------
    def _load(self):
        with self._lock:
            if self._det is not None:
                return
            import onnxruntime as ort
            from huggingface_hub import snapshot_download

            root = snapshot_download(
                f"immich-app/{self.model_pack}",
                allow_patterns=["detection/model.onnx", "recognition/model.onnx"],
            )
            opts = ort.SessionOptions()
            threads = os.getenv("ODYSSEUS_GALLERY_FACE_THREADS", "").strip()
            if threads.isdigit():
                opts.intra_op_num_threads = int(threads)
            providers = ["CPUExecutionProvider"]
            self._det = ort.InferenceSession(os.path.join(root, "detection", "model.onnx"), opts, providers=providers)
            self._rec = ort.InferenceSession(os.path.join(root, "recognition", "model.onnx"), opts, providers=providers)

    # -- detection -----------------------------------------------------
    def detect(self, img: Image.Image) -> list[Face]:
        self._load()
        width, height = img.size
        if height / width > 1.0:
            new_h, new_w = DET_SIZE, max(1, int(DET_SIZE * width / height))
        else:
            new_w, new_h = DET_SIZE, max(1, int(DET_SIZE * height / width))
        scale = new_h / height
        canvas = np.zeros((DET_SIZE, DET_SIZE, 3), dtype=np.float32)
        canvas[:new_h, :new_w] = np.asarray(img.convert("RGB").resize((new_w, new_h), Image.BILINEAR), dtype=np.float32)
        blob = ((canvas - 127.5) / 128.0).transpose(2, 0, 1)[None]
        outs = self._det.run(None, {self._det.get_inputs()[0].name: blob})

        boxes, scores, kpss = [], [], []
        n = len(_STRIDES)
        for i, stride in enumerate(_STRIDES):
            score = outs[i].reshape(-1)
            dist = outs[i + n].reshape(-1, 4) * stride
            kdist = outs[i + 2 * n].reshape(-1, 10) * stride
            fh = fw = DET_SIZE // stride
            centers = (np.stack(np.mgrid[:fh, :fw][::-1], axis=-1).astype(np.float32) * stride).reshape(-1, 2)
            centers = np.repeat(centers, _ANCHORS_PER_POSITION, axis=0)
            keep = np.where(score >= DET_THRESH)[0]
            if not len(keep):
                continue
            c, d, k = centers[keep], dist[keep], kdist[keep]
            boxes.append(np.stack([c[:, 0] - d[:, 0], c[:, 1] - d[:, 1], c[:, 0] + d[:, 2], c[:, 1] + d[:, 3]], axis=-1))
            kpss.append(np.stack([c[:, j % 2] + k[:, j] for j in range(10)], axis=-1).reshape(-1, 5, 2))
            scores.append(score[keep])
        if not boxes:
            return []
        boxes = np.concatenate(boxes) / scale
        kpss = np.concatenate(kpss) / scale
        scores = np.concatenate(scores)
        order = _nms(boxes, scores, NMS_THRESH)
        return [Face(box=tuple(float(v) for v in boxes[j]), score=float(scores[j]), landmarks=kpss[j]) for j in order]

    # -- recognition ---------------------------------------------------
    def embed(self, img: Image.Image, landmarks: np.ndarray) -> np.ndarray:
        self._load()
        aligned = align_face(img, landmarks)
        blob = ((np.asarray(aligned, dtype=np.float32) - 127.5) / 127.5).transpose(2, 0, 1)[None]
        vec = self._rec.run(None, {self._rec.get_inputs()[0].name: blob})[0].reshape(-1)
        return vec / (np.linalg.norm(vec) + 1e-12)

    def analyze(self, img: Image.Image) -> tuple[Image.Image, list[Face]]:
        """Detect and embed all faces. Returns the (possibly downscaled) image the boxes refer to."""
        img = img.convert("RGB")
        if max(img.size) > WORK_MAX_SIDE:
            img = img.copy()
            img.thumbnail((WORK_MAX_SIDE, WORK_MAX_SIDE), Image.LANCZOS)
        faces = self.detect(img)
        for face in faces:
            face.embedding = self.embed(img, face.landmarks)
        return img, faces


def _nms(boxes: np.ndarray, scores: np.ndarray, threshold: float) -> list[int]:
    x1, y1, x2, y2 = boxes.T
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size:
        i = order[0]
        keep.append(int(i))
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0.0, xx2 - xx1 + 1) * np.maximum(0.0, yy2 - yy1 + 1)
        iou = inter / (areas[i] + areas[order[1:]] - inter)
        order = order[1:][iou <= threshold]
    return keep


def _similarity_transform(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """2x3 similarity transform mapping ``src`` points onto ``dst`` (Umeyama, as skimage)."""
    src = src.astype(np.float64)
    dst = dst.astype(np.float64)
    num, dim = src.shape
    src_mean, dst_mean = src.mean(0), dst.mean(0)
    src_d, dst_d = src - src_mean, dst - dst_mean
    a = dst_d.T @ src_d / num
    d = np.ones(dim)
    if np.linalg.det(a) < 0:
        d[dim - 1] = -1
    u, s, vt = np.linalg.svd(a)
    rot = u @ np.diag(d) @ vt
    scale = (s @ d) / src_d.var(0).sum()
    m = np.zeros((2, 3))
    m[:, :2] = scale * rot
    m[:, 2] = dst_mean - scale * rot @ src_mean
    return m


def align_face(img: Image.Image, landmarks: np.ndarray, size: int = 112) -> Image.Image:
    """Warp the face to the ArcFace 112x112 template (same result as cv2.warpAffine)."""
    m = _similarity_transform(np.asarray(landmarks), ARCFACE_DST * (size / 112.0))
    inv = np.linalg.inv(np.vstack([m, [0, 0, 1]]))[:2]   # PIL maps output → input
    return img.transform((size, size), Image.AFFINE, data=tuple(inv.reshape(-1)), resample=Image.BILINEAR)


_engine: FaceEngine | None = None
_engine_lock = threading.Lock()


def get_engine() -> FaceEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = FaceEngine()
        return _engine
