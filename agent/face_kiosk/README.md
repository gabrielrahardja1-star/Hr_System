# Face-recognition attendance kiosk — prototype

A punch source that identifies an **enrolled employee from a webcam** and logs a
timestamped recognition event. Same role in the system as the planned pyzk agent
and `tools/mock_punch_source.py`.

```
webcam ─▶ YuNet detect ─▶ IOU track ─▶ SFace embed ─▶ gallery match
                                                          │
                              vote over N frames ◀────────┘
                                     │
                    randomized head-turn liveness check
                                     │
                            data/events.jsonl        ← this prototype stops here
                                     │
                     (Phase 2)  POST /api/v1/punches  ← same payload as a real device
```

**This prototype does not talk to HQ.** It writes `data/events.jsonl`, one JSON
object per recognition, already shaped like a punch. Wiring it to the ingest API
is a separate, small step once recognition accuracy looks good.

Nothing here trains a model. **YuNet** (detector) and **SFace** (128-d embedder)
are small pretrained ONNX nets from OpenCV's zoo, used as primitives. Enrolling
an employee stores a few embedding vectors — no retraining, any headcount, and
new hires cost one `enroll` run.

## Why not the CAP6411 reference approach

That project retrains a YOLOv5 classifier per person (`Unknown` / `Eric` /
`Hansi`) and needs CUDA. For a real roster with churn that means a training run
per hire. We took its **structure** — detect → track → vote per face → overlay —
and its tracking idea (minus the neural re-ID), not its recognition method.

## Setup (kiosk Mac only — not the HQ server)

```bash
python3.14 -m venv agent/face_kiosk/.venv
agent/face_kiosk/.venv/bin/pip install -r agent/face_kiosk/requirements.txt
cd agent/face_kiosk
.venv/bin/python -m facekiosk.fetch_models      # ~39 MB, one time
.venv/bin/python -m facekiosk.selftest          # camera-free sanity check
```

macOS will block the camera until you grant it. First camera command triggers
the prompt for the app that launched Python (Terminal / iTerm / VS Code). If
frames come back black: **System Settings → Privacy & Security → Camera**, enable
that app, then fully quit and reopen it.

## Use

```bash
cd agent/face_kiosk

# 1. which camera is the Logitech?
.venv/bin/python -m facekiosk.camera

# 2. enrol someone (uid = their Employee.device_user_id in HR). Get consent.
.venv/bin/python -m facekiosk.enroll --uid 1001 --name "Budi Santoso" --camera 1 --auto
.venv/bin/python -m facekiosk.enroll --list
.venv/bin/python -m facekiosk.enroll --uid 1001 --name "Budi Santoso" --from-images ./photos/

# 3. run the kiosk
.venv/bin/python -m facekiosk.run --camera 1
.venv/bin/python -m facekiosk.run --camera 1 --no-liveness --match-cosine 0.42
.venv/bin/python -m facekiosk.run --camera 1 --no-window --seconds 120
```

In the window: a face gets an amber box + name guess + a vote bar; once the vote
settles it asks for a head turn (`Turn your head left ←`); on success the box
goes green and a line is logged. Press **Q** to quit.

### Event line

```json
{"ts":"2026-09-10T08:42:11+07:00","device_user_id":"1001","name":"Budi Santoso",
 "similarity":0.71,"liveness":"pass","track_id":4,"source":"face-kiosk-proto"}
```

## Tuning (`facekiosk/config.py`)

| knob | default | raise it to… |
|---|---|---|
| `match_cosine` | 0.38 | reject look-alikes (fewer false accepts, more "unknown") |
| `detect_score` | 0.85 | ignore blurry/side faces |
| `min_face_px` | 90 | require people to step closer |
| `vote_frames` | 12 | demand more agreement before a punch (slower, safer) |
| `debounce_seconds` | 120 | widen the "already checked in" window |
| `liveness_yaw_delta` | 0.16 | need a bigger head turn |
| `liveness_invert` | false | flip if "turn left" registers as a right turn on your camera |

Expect to adjust `match_cosine` and `detect_score` on-site — entrance lighting,
hi-vis, hats and motion blur all move the numbers.

## Known limits

- **Liveness is a deterrent, not a guarantee.** A held photo fails, a random
  direction beats a casual video replay, but a determined replay attack needs a
  dedicated anti-spoof model (a planned option).
- **Biometric data.** Face embeddings are *data pribadi spesifik* under UU PDP
  27/2022. The gallery (`data/faces.gallery`) is Fernet-encrypted at rest with a
  local `data/faces.key` (chmod 600); nothing leaves the machine. Enrol only
  with consent; keep vectors, not photos.
- **One camera, one entrance.** Multi-kiosk enrollment sync would move the
  gallery to HQ (designed for, not built).
- Continuity Camera (your iPhone) shows up as a camera index — pick the C615.

## Files

| file | role |
|---|---|
| `facekiosk/config.py` | all paths and thresholds |
| `facekiosk/fetch_models.py` | download YuNet + SFace ONNX |
| `facekiosk/camera.py` | macOS camera enumeration + capture |
| `facekiosk/engine.py` | detect + embed (wraps OpenCV YuNet/SFace) |
| `facekiosk/tracker.py` | IOU tracker, per-face vote state |
| `facekiosk/gallery.py` | encrypted enrollment store + matching |
| `facekiosk/liveness.py` | randomized head-turn challenge |
| `facekiosk/enroll.py` | enrol / list / remove CLI |
| `facekiosk/run.py` | the kiosk loop |
| `facekiosk/selftest.py` | camera-free pipeline check |
