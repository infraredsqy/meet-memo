# meeting

A local meeting recorder. `meeting web` starts a service bound to `127.0.0.1` only; open its port in
a browser. It records both speaking sources separately while the meeting runs, produces a provisional
transcript as it goes, and — on request, once the meeting is over — refined transcripts and minutes.

## What it does

- **Record.** Microphone and system loopback are captured as two independent tracks, written to the
  session directory as 16 kHz mono int16 PCM while recording, then converted segment by segment to
  MP3 at the end; the PCM is kept. If one track fails, the other keeps recording.
- **Follow along.** Recordings end and sessions are listed on one workbench; the live view shows the
  service state, per-track levels, the provisional transcript, and alerts.
- **Ask.** "Summarize" returns three sentences in ten to fifteen seconds: what was just said, where
  the discussion stands, and what was missed. This one feature calls an OpenAI-compatible endpoint you
  configure; everything else runs locally.
- **After the meeting.** Stopping a recording triggers nothing. Two independent buttons on the
  workbench each run their own job: a refined transcript with speaker labels, and minutes written by
  a locally running DeepSeek Harness (DSH) instance. Outputs land in the session directory, and
  `meeting post <session-dir>` runs the same pipeline from the command line.

## Requirements

- Python ≥ 3.13.
- `ffmpeg` on `PATH`, used for MP3 conversion. Without it the service still starts and still
  records: the conversion step is reported as `failed` and the PCM is kept as is.

## Install

```powershell
pip install -e .          # the service; provides the `meeting` command
# live and offline transcription need model dependencies, installed separately
# (the torch build below targets CUDA 12.8)
pip install --extra-index-url https://download.pytorch.org/whl/cu128 -e ".[asr]"
```

The service runs without the `asr` extra. Skipping `-e` also works: `python -m meeting web` from the
repository root is equivalent to `meeting web`.

## Run

```powershell
meeting web                             # http://127.0.0.1:8765, opens a browser
$env:MEETING_PORT=8766; meeting web     # another port; refuses to start if it is taken
$env:MEETING_NO_BROWSER=1; meeting web  # headless
```

A session is a directory you choose and name. The service does not create it, name it by date, or
have a default location. Starting again in a directory that already holds a recording continues that
session: new segments are appended and existing audio and transcripts are left untouched.

## Where things live

| What | Location |
|---|---|
| Configuration | `~/.config/meeting/config.json` |
| Session | the directory given at start: audio, transcripts, outputs, `session.json` |
| Service log | `~/.config/meeting/logs/meeting.log` |
| Session log | `<session-dir>/meeting.log` |

## Commands

```powershell
meeting web                  # run the service in the foreground until Ctrl+C
meeting post <session-dir>   # run the post-meeting pipeline only, then exit
meeting replay <session-dir> # replay verification: re-run the offline chain on a truncated copy
```

## Development

```powershell
python -m pytest -q                                   # server, quick and slow tests
cd web; npm test; npm run typecheck; npm run build    # interface, Node ≥ 20
```

Specifications and design rationale live in [`docs/`](docs/AGENTS.md).

## License and contact

MIT, see [LICENSE](LICENSE). Questions and commercial use: [infraredsaw@gmail.com](mailto:infraredsaw@gmail.com).
Bugs and feature requests are better opened as an issue.
