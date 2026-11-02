"""Face detection and recognition for the gallery (people tagging).

InsightFace models (SCRFD detector + ArcFace recognizer) run through
onnxruntime, without the heavy ``insightface`` package. Face crops and
embeddings are stored in Odysseus's own data; source photos are only read.
"""
