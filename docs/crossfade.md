# Local audio crossfade

Mopidy's local crossfade feature overlaps the end of the current track with the beginning of the next track in the playback queue. It is implemented in the Mopidy audio pipeline using GStreamer's `audiomixer` and a dynamically created decoder branch. This implementation is currently part of this modified Mopidy source tree, **not** a separately installable Mopidy extension.

## Configuration

Set the following options in your Mopidy configuration:

```ini
[audio]
crossfade_duration = 5
crossfade_curve = equal_power
```

- `crossfade_duration`: Non-negative integer number of seconds to overlap tracks. The default, `0`, disables crossfade and retains the normal playback pipeline.
- `crossfade_curve`: `linear` (default) or `equal_power`.

You can override the values on the command line:

```bash
mopidy -o mpd/enabled=false -o http/port=6681 \
  -o audio/crossfade_duration=5 \
  -o audio/crossfade_curve=equal_power
```

The `mpd/enabled=false` option is optional for crossfade: it disables the MPD frontend and is useful if that separately installed extension is incompatible with your Mopidy version. Crossfade does not depend on MPD. Once a compatible web frontend such as Mopidy-Mobile is installed, users can control the queue from `http://127.0.0.1:6681/mobile/` without running Python playback scripts.

The crossfade options are read when Mopidy starts. They cannot currently be changed live from the frontend or set separately per playlist.

## Playback behavior

1. Near the end of a track with a known duration, Audio asks Core for the next eligible track in the playback queue.
2. Core resolves its playback URI using the corresponding backend.
3. For a supported `file://` URI, the audio pipeline creates a new GStreamer decode branch and links it to `audiomixer`.
4. A timer updates the outgoing and incoming track gains during the overlap.
5. When the transition completes, the incoming decoder branch becomes the active audio source. Core is notified of the corresponding tracklist ID (`TLID`) so that current-track reporting and subsequent transitions remain consistent.
6. Pending and retired dynamic branches are cleaned up when appropriate, including on stop or when a transition is cancelled.

The `linear` curve uses outgoing/incoming gains of `1-t` / `t`, where `t` is transition progress from 0 to 1. The `equal_power` curve uses `cos(pi*t/2)` / `sin(pi*t/2)`.

Seek operations after a crossfade are directed to the promoted dynamic source, rather than the retired `playbin` source. The dynamic source's running-time offset must be set consistently with the new GStreamer segment, so that audio continues after seeking and remains aligned with the mixer.

## Current scope and limitations

- **Local files only:** crossfade candidates must resolve to `file://` URIs. Crossfade for remote HTTP(S) streams, Spotify and other streaming backends is not implemented.
- **Known duration required for fade scheduling:** tracks with no known length cannot reliably enter the final-seconds fade window.
- **Global settings:** duration and curve are selected when the Mopidy process starts; there is no runtime settings API or frontend control.
- **Audio support depends on GStreamer:** files must have usable decoders and a working audio output.
- Automated tests do not prove every playback scenario. Extended playback, manual next/previous, end-of-queue, pauses and repeated seeks should also be validated on the target system.

## Verification

Run the full test suite from the project virtual environment:

```bash
python -m pytest -q
git diff --check
```

For an interactive integration check, prepare a queue with at least three entries (a source file may be repeated), then verify A -> B -> C transitions, pause/resume, and seeking within B before B -> C. The development smoke test `crossfade_handoff_patch/check_crossfade.py` can be used locally if it is available, but it is a development helper and is not required for normal playback or distribution.

## Relevant source files

- `src/mopidy/_app/config.py` and `src/mopidy/_app/default.conf`: configuration schemas and defaults.
- `src/mopidy/audio/_gst/crossfade.py`: gain curves and fade-window/EOS helpers.
- `src/mopidy/audio/_gst/pipeline.py`: GStreamer mixer, dynamic source and seeking logic.
- `src/mopidy/audio/_gst/audio.py`: transition scheduling and audio actor coordination.
- `src/mopidy/audio/_api.py` and `src/mopidy/core/_playback.py`: coordination with Core and track handoff.
- `tests/audio/test_crossfade.py`, `tests/audio/test_crossfade_eos.py` and `tests/_app/test_config.py`: regression and configuration tests.
