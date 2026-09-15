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
                    tap "Sync to HQ" (/admin/log)  ──▶  POST /api/v1/punches
```

**Biometric data never leaves the machine.** The Sync button only sends
sighting events (who, when) to HQ — never the face gallery. See "Sync to HQ"
below.

## The app

```bash
.venv/bin/python -m facekiosk.app --camera "HD Webcam C615"   # then open http://localhost:8770
```

First start prints an admin PIN (also saved to `data/admin.pin`, chmod 600) —
that's what gates `/admin`. Set `FACEKIOSK_ADMIN_PIN` to fix one instead of
letting it generate randomly.

- **/** (Kiosk) — fullscreen, no chrome, no admin functions reachable from it.
  Live camera (mirrored), a readout of who's recognised right now, and one big
  contextual button (**Check In** or **Check Out**, whichever the person's day
  actually needs next) plus a smaller "instead" button for the other direction.
  Nothing is logged until someone taps one; `--auto-log` also logs automatically
  on recognition (direction `auto`).
- **/admin** (PIN-gated — printed to the console on first start, or set
  `FACEKIOSK_ADMIN_PIN`) —
  - **/admin/log** — attendance roll-up per person (check-in / check-out / hours
    / record count), a day picker, CSV export, a *Void last* correction per row,
    and the enrolled list with a *Forget* button.
  - **/admin/register** — capture a few webcam shots (with per-shot thumbnails
    and delete), type a name (+ optional ID), Save. The running scanner picks up
    the new face immediately.

One background thread owns the camera and runs recognition. A person becomes a
tappable *candidate* once the vote settles and the head-turn passes; the button
writes the event to `data/sightings.db` (SQLite, this app's own store — nothing
to do with the HQ database) and `data/events.jsonl`.

**Cameras are addressed by NAME, never by index.** `GET /api/cameras` lists
connected devices, `POST /api/camera {"name": "HD Webcam C615"}` switches, and
both the kiosk and admin top bars carry that picker. Switching swaps the capture
device without restarting the server or losing the gallery; the in-progress
track/vote state for whoever was mid-recognition is cleared.

Why names: OpenCV's `VideoCapture` takes only an integer index and exposes no
device-name API, so a name shown beside an index has to come from a separate
enumeration zipped on by position — and that is not dependable. On this machine
`system_profiler` returned the two cameras in a *different order* between runs,
so the same index was labelled differently minutes apart and picking "the
Logitech" opened the built-in camera. Indices also reshuffle when a webcam drops
off the USB bus or a Continuity Camera appears, silently repointing a saved index
at a different physical device. Capture therefore runs through **ffmpeg's
avfoundation input, which takes the device name directly** — nothing translates a
name into a number, and an unplugged camera fails loudly instead of quietly
recording from the wrong one.

Capture defaults to **640x480@30**. Uncompressed capture is USB-bandwidth-bound:
this C615 delivers a real 10 fps at 1280x720 and 5 fps at 1080p, while 640x480
runs a true 30 — and the liveness head-turn plus the 12-frame identity vote need
frames far more than they need pixels.

Nothing here trains a model. **YuNet** (detector) and **SFace** (128-d embedder)
are small pretrained ONNX nets from OpenCV's zoo, used as primitives. Enrolling
an employee stores a few embedding vectors — no retraining, any headcount, and
new hires cost one `enroll` run.

## Sync to HQ

The **Sync to HQ** button on `/admin/log` (next to Export CSV) POSTs every
sighting not yet synced to HQ's `/api/v1/punches`, idempotently — safe to
click repeatedly, and re-clicking after a failed sync only resends what's
still pending (`SightingStore` tracks `synced_at` per row, so it's the
offline-tolerant queue: if HQ is unreachable, the click fails cleanly and
nothing is lost, nothing is double-sent).

Configure via environment before starting the app:

| env var | default | |
|---|---|---|
| `FACEKIOSK_HQ_URL` | `http://localhost:8000` | HQ base URL. Point at the real site/VPS URL for production — the default is deliberately local-only so a stray run never posts to production. |
| `FACEKIOSK_HQ_API_KEY` | *(none — sync fails until set)* | must be one of HQ's `HR_INGEST_API_KEYS`. |
| `FACEKIOSK_DEVICE_ID` | `FACE-KIOSK-01` | identifies this kiosk's punches in HQ; give each physical kiosk its own value. |

A synced sighting's `uid` becomes `PunchIn.device_user_id` — it must exactly
match the person's `Employee.device_user_id` in HQ (same rule as `enroll
--uid`, above) or the punch lands but never attributes to anyone for payroll.

## Packaging as a standalone executable

```bash
cd agent/face_kiosk
.venv/bin/pip install -r requirements-build.txt   # pyinstaller, build-only
.venv/bin/python -m facekiosk.fetch_models         # models get baked into the build
.venv/bin/python -m facekiosk.fetch_ffmpeg         # static ffmpeg, ditto
.venv/bin/pyinstaller facekiosk.spec
# → dist/facekiosk (macOS) — a single file, nothing else needed to run it
```

Copy `dist/facekiosk` anywhere and run it directly; it creates its own
`data/` folder next to itself (gallery, sightings db, admin PIN) — that
folder is what to back up / carry between machines, and what to delete for a
factory-reset kiosk. **Fully self-contained**: `fetch_ffmpeg.py` bundles a
static ffmpeg (evermeet.cx on macOS, gyan.dev "essentials" on Windows) into
the exe, verified by checking which binary the running exe's ffmpeg
subprocess actually resolves to — the one inside the bundle, not a system
install. No separate ffmpeg install needed on the target machine. (It's a
GPL-licensed full build, far more codecs than this app touches since neither
source ships a build with just device-capture input — a non-issue for an
internal tool, worth a look before external distribution.)

**Windows**: `facekiosk/camera.py` has a DirectShow (`dshow`) capture path
alongside the macOS one, and the same `facekiosk.spec` should produce a
`.exe` — but PyInstaller doesn't cross-compile, so it has to actually be
built and run on a Windows machine, which this codebase has never had a
chance to do. Treat the Windows path as **written, not verified**: run
`python -m facekiosk.camera` there first and confirm it lists real devices
before trusting it further.

## Why not the CAP6411 reference approach

That project retrains a YOLOv5 classifier per person (`Unknown` / `Eric` /
`Hansi`) and needs CUDA. For a real roster with churn that means a training run
per hire. We took its **structure** — detect → track → vote per face → overlay —
and its tracking idea (minus the neural re-ID), not its recognition method.

## Setup (kiosk Mac only — not the HQ server)

```bash
brew install ffmpeg                            # capture backend (selects cameras by name)
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

# list cameras BY NAME (pass one to --camera, quoted)
.venv/bin/python -m facekiosk.camera

# the web app — register + scan + log, all in the browser
.venv/bin/python -m facekiosk.app --camera "HD Webcam C615"
.venv/bin/python -m facekiosk.app --camera "HD Webcam C615" --no-liveness --match-cosine 0.42
```

### CLI, without the web app

```bash
# enrol from the terminal (uid: give an ID or leave it to auto-assign Knnnn)
.venv/bin/python -m facekiosk.enroll --uid 1001 --name "Budi Santoso" --camera "HD Webcam C615" --auto
.venv/bin/python -m facekiosk.enroll --list
.venv/bin/python -m facekiosk.enroll --from-images ./photos/ --uid 1001 --name "Budi Santoso"

# the OpenCV-window scanner (writes events.jsonl only, no web UI / sightings.db).
# auto-logs on recognition by default; --no-auto-log parks candidates instead.
.venv/bin/python -m facekiosk.run --camera "HD Webcam C615"
.venv/bin/python -m facekiosk.run --camera "HD Webcam C615" --no-window --seconds 120
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
`/admin/log` roll-up takes the first `in` as check-in and the last `out` as
check-out, and computes hours worked between them.

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
| `liveness_retries` | 2 | more attempts before showing "Didn't catch that" |
| `liveness_fail_hold_s` / `unrecognized_hold_s` | 4.0 | how long a dead-end message stays up before auto-retrying |

Overlay text/similarity numbers on the video are off by default on the kiosk
(`/`) — the HTML readout carries all language now. Add `?debug=1` to the URL
to bring back the on-video labels for an on-site tuning pass.

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
- Continuity Camera (your iPhone) shows up as a camera index too — use the top
  bar's dropdown to pick the right device, or re-pick it after a webcam
  reconnects and the index shuffles.

## Files

| file | role |
|---|---|
| `facekiosk/config.py` | all paths and thresholds |
| `facekiosk/fetch_models.py` | download YuNet + SFace ONNX |
| `facekiosk/fetch_ffmpeg.py` | download a static ffmpeg for packaging (vendor/, gitignored) |
| `facekiosk/camera.py` | camera enumeration + capture, addressed by device NAME (via ffmpeg; avfoundation on macOS, dshow on Windows — untested) |
| `facekiosk/engine.py` | detect + embed (wraps OpenCV YuNet/SFace) |
| `facekiosk/tracker.py` | IOU tracker, per-face vote state |
| `facekiosk/gallery.py` | encrypted enrollment store + matching |
| `facekiosk/liveness.py` | randomized head-turn challenge |
| `facekiosk/store.py` | SQLite sightings log + daily roll-up + sync tracking |
| `facekiosk/sync.py` | pushes unsynced sightings to HQ's `/api/v1/punches` |
| `facekiosk/run.py` | the `Kiosk` pipeline (+ auto/manual modes) + OpenCV-window scanner |
| `facekiosk/app.py` | the web app (camera thread + FastAPI + admin PIN gate) |
| `facekiosk/web/` | `kiosk` (public) and `admin_*` / `log` / `register` (PIN-gated) templates + static assets |
| `facekiosk/enroll.py` | enrol / list / remove CLI |
| `facekiosk/selftest.py` | camera-free pipeline check (sandboxed) |
| `run_kiosk.py` / `facekiosk.spec` | PyInstaller entry point + build spec for the standalone exe |
