"""Face-recognition attendance kiosk (prototype).

A punch source that identifies an enrolled employee from a webcam and logs a
timestamped recognition event. Structurally the same role as the planned pyzk
agent and tools/mock_punch_source.py: it will (Phase 2) POST the exact
PunchBatch payload to HQ's /api/v1/punches. This prototype stops at the event
log so recognition accuracy can be judged first.

Pipeline:  frame -> YuNet detect -> IOU track -> SFace embed -> gallery match
           -> vote over N frames -> randomized head-turn liveness -> event

Nothing here trains a model. YuNet (detector) and SFace (128-d embedder) are
small pretrained ONNX nets used as primitives; an employee is enrolled by
storing a few embedding vectors, never by retraining.
"""

__version__ = "0.1.0"
