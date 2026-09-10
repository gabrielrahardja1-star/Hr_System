# Face-recognition attendance kiosk — prototype

Identifies an **enrolled person from a webcam** and records the time they were
seen. Eventually a punch source alongside the planned pyzk agent; for now a
standalone tool for judging recognition accuracy.

```
webcam ─▶ YuNet detect ─▶ IOU track ─▶ SFace embed ─▶ gallery match
                                                          │
                              vote over N frames ◀────────┘
                                     │
                    randomized head-turn liveness check
                                     │
                       person is now a "candidate"
                                     │
              ┌──────────────────────┴──────────────────────┐
        Check In / Check Out tap                     --auto-log (opt-in)
              └──────────────────────┬──────────────────────┘
                                     │
                     data/sightings.db  +  data/events.jsonl
                                     │
                     (later)  POST /api/v1/punches   ← same payload as a real device
```

**This prototype does not talk to HQ.** Wiring the events to the ingest API is a
separate, small step once recognition accuracy looks good.

## The app

```bash
.venv/bin/python -m facekiosk.app --camera 1      # then open http://localhost:8770
```

- **/** (Kiosk) — live camera, a readout of who's recognised right now, and big
  **Check In / Check Out** buttons. Nothing is logged until someone taps one;
  the tap stamps the current time with a direction. `--auto-log` also logs
  automatically on recognition (direction `auto`).
- **/log** — attendance roll-up per person (check-in / check-out / tap count), a
  day picker, and the enrolled list with a *Forget* button.
- **/register** — capture a few webcam shots, type a name (+ optional ID), Save.
  The running scanner picks up the new face immediately.

One background thread owns the camera and runs recognition. A person becomes a
tappable *candidate* once the vote settles and the head-turn passes; the button
writes the event to `data/sightings.db` (SQLite, this app's own store — nothing
to do with the HQ database) and `data/events.jsonl`.

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

# which camera is the Logitech?
.venv/bin/python -m facekiosk.camera

# the web app — register + scan + log, all in the browser
.venv/bin/python -m facekiosk.app --camera 1
.venv/bin/python -m facekiosk.app --camera 1 --no-liveness --match-cosine 0.42
```

### CLI, without the web app

```bash
# enrol from the terminal (uid: give an ID or leave it to auto-assign Knnnn)
.venv/bin/python -m facekiosk.enroll --uid 1001 --name "Budi Santoso" --camera 1 --auto
.venv/bin/python -m facekiosk.enroll --list
.venv/bin/python -m facekiosk.enroll --from-images ./photos/ --uid 1001 --name "Budi Santoso"

# the OpenCV-window scanner (writes events.jsonl only, no web UI / sightings.db).
# auto-logs on recognition by default; --no-auto-log parks candidates instead.
.venv/bin/python -m facekiosk.run --camera 1
.venv/bin/python -m facekiosk.run --camera 1 --no-window --seconds 120
```

In the scanner view: a face gets an amber box + name guess + a vote bar; once the
vote settles it asks for a head turn (`Turn your head left ←`); on success the box
goes green and (in auto-log mode) the sighting is logged.

> When mapping to HR later, enrol each person with `--uid` = their
> `Employee.device_user_id` so a face punch and a fingerprint punch coincide.
> Always get consent before enrolling.

### Event record

Written to both `data/sightings.db` and `data/events.jsonl` per logged event:

```json
{"ts":"2026-09-10T08:42:11+07:00","device_user_id":"1001","emp_id":"1042",
 "name":"Budi Santoso","direction":"in","similarity":0.71,"liveness":"pass",
 "track_id":4,"source":"face-kiosk-proto"}
```

`direction` is `in` / `out` from the buttons, or `auto` from `--auto-log`. The
`/log` roll-up takes the first `in` as check-in and the last `out` as check-out.

## Tuning (`facekiosk/config.py`)

| knob | default | raise it to… |
|---|---|---|
| `match_cosine` | 0.38 | reject look-alikes (fewer false accepts, more "unknown") |
| `detect_score` | 0.85 | ignore blurry/side faces |
| `min_face_px` | 90 | require people to step closer |
| `vote_frames` | 12 | demand more agreement before a punch (slower, safer) |
| `debounce_seconds` | 120 | auto-log: widen the "already logged" window |
| `capture_debounce_seconds` | 8 | manual: ignore a repeated Check In/Out tap this soon |
| `liveness_yaw_delta` | 0.10 | need a bigger head turn to count |
| `liveness_return_frac` | 0.4 | make them turn back further before it passes |

Expect to adjust `match_cosine` and `detect_score` on-site — entrance lighting,
hi-vis, hats and motion blur all move the numbers.

## Known limits

- **Liveness is a deterrent, not a guarantee.** The head turn is direction-
  agnostic ("moved, then came back"); a held photo fails it, a determined video
  replay would need a dedicated anti-spoof model (a planned option). Run with
  `--no-liveness` to skip it while tuning recognition.
- **Biometric data.** Face embeddings are *data pribadi spesifik* under UU PDP
  27/2022. The gallery (`data/faces.gallery`) is Fernet-encrypted at rest with a
  local `data/faces.key` (chmod 600); nothing leaves the machine. Enrol only
  with consent; keep vectors, not photos.
- **One camera, one entrance.** The web app binds to `127.0.0.1` and holds the
  camera exclusively. Multi-kiosk enrollment sync would move the gallery to HQ
  (designed for, not built).
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
| `facekiosk/store.py` | SQLite sightings log + daily roll-up |
| `facekiosk/run.py` | the `Kiosk` pipeline (+ auto/manual modes) + OpenCV-window scanner |
| `facekiosk/app.py` | the web app (camera thread + FastAPI) |
| `facekiosk/web/` | `kiosk` / `log` / `register` templates + static assets |
| `facekiosk/enroll.py` | enrol / list / remove CLI |
| `facekiosk/selftest.py` | camera-free pipeline check (21 checks) |
