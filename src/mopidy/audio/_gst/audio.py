from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast, override

import pykka

from mopidy import exceptions
from mopidy._lib import logs, process
from mopidy._lib.gi import GLib, Gst, GstBase, GstPbutils
from mopidy._lib.gst import repr_tags, setup_proxy
from mopidy.audio._api import Audio
from mopidy.audio._gst.crossfade import (
    should_ignore_spurious_eos,
    should_start_crossfade,
)
from mopidy.audio._gst.mixer import GstSoftwareMixerAdapter
from mopidy.audio._gst.pipeline import GstPlaybackPipeline
from mopidy.audio._gst.types import (
    GstAsyncDone,
    GstBuffering,
    GstBusMessage,
    GstEndOfStream,
    GstError,
    GstMissingPlugin,
    GstState,
    GstStateChanged,
    GstStreamStart,
    GstTag,
    GstWarning,
)
from mopidy.audio._listener import AudioListener
from mopidy.types import DurationMs, PlaybackState

if TYPE_CHECKING:
    from mopidy._exts.softwaremixer.mixer import SoftwareMixerProxy
    from mopidy._lib.gst import GstTags
    from mopidy.config import Config
    from mopidy.mixer import MixerProxy

logger = logging.getLogger(__name__)

# This logger is only meant for debug logging of low level GStreamer info such
# as callbacks, event, messages and direct interaction with GStreamer such as
# set_state() on a pipeline.
gst_logger = logging.getLogger("mopidy.audio.gst")


class GstAudio(Audio, pykka.ThreadingActor):
    """Audio output through [GStreamer](https://gstreamer.freedesktop.org/)."""

    mixer: GstSoftwareMixerAdapter | None = None

    def __init__(
        self,
        config: Config,
        mixer: MixerProxy | None,
    ) -> None:
        super().__init__()

        self._config = config
        self._target_state: GstState = GstState.NULL
        self._buffering: bool = False
        self._live_stream: bool = False
        self._tags: GstTags = {}
        self._pending_uri: str | None = None
        self._pending_tags: GstTags | None = None

        self._pipeline: GstPlaybackPipeline | None = None
        self._crossfade_timer_id: int | None = None
        self._crossfade_window_detected = False
        self._crossfade_last_position = None
        self._crossfade_candidate = None
        self._crossfade_generation = 0
        self._crossfade_handoff_callback = None
        self._crossfade_prepare_callback = None
        self._about_to_finish_callback: Callable | None = None
        self._source_setup_callback: Callable | None = None

        if mixer and self._config["audio"]["mixer"] == "software":
            mixer = cast("SoftwareMixerProxy", mixer)
            self.mixer = pykka.traversable(GstSoftwareMixerAdapter(mixer))

    @override
    def on_start(self) -> None:
        self._thread = threading.current_thread()
        try:
            self._setup_preferences()
            self._pipeline = GstPlaybackPipeline(
                self._config,
                on_bus_message=self._on_gst_bus_message,
                on_position=self._on_gst_position,
                on_about_to_finish=self._on_gst_about_to_finish,
                on_source_setup=self._on_gst_source_setup,
            )
            if self._config["audio"].get("crossfade_duration", 0) > 0:
                self._crossfade_timer_id = GLib.timeout_add(
                    100,
                    self._request_crossfade_tick,
                )
            if self.mixer:
                self.mixer.setup(self._pipeline.volume, self.actor_ref.proxy().mixer)
        except (GLib.Error, exceptions.AudioException):
            logger.exception("Failed to set up the audio pipeline.")
            process.exit_process()

    @override
    def on_stop(self) -> None:
        if self.mixer:
            self.mixer.teardown()
        if self._crossfade_timer_id is not None:
            GLib.source_remove(self._crossfade_timer_id)
            self._crossfade_timer_id = None
        if self._pipeline is not None:
            self._pipeline.teardown()

    def _setup_preferences(self) -> None:
        # TODO: move out of audio actor?
        # Fix for https://github.com/mopidy/mopidy/issues/604
        registry = Gst.Registry.get()
        jacksink = registry.find_feature("jackaudiosink", Gst.ElementFactory)
        if jacksink:
            jacksink.set_rank(Gst.Rank.SECONDARY)

    def _on_gst_about_to_finish(self, _element: Gst.Element) -> None:
        if self._thread == threading.current_thread():
            logger.error("about-to-finish in actor, aborting to avoid deadlock.")
            return

        gst_logger.debug("Got about-to-finish event.")
        if self._pipeline and (
            self._pipeline.has_crossfade_source
            or self._pipeline.has_dynamic_track
        ):
            # B is already playing on the mixer's dynamic branch. Passing
            # about-to-finish to Core would start another copy on playbin.
            return
        if self._about_to_finish_callback:
            logger.debug("Running about-to-finish callback.")
            self._about_to_finish_callback()

    def _on_gst_source_setup(
        self,
        _element: Gst.Element,
        source: Gst.Element,
    ) -> None:
        gst_logger.debug(
            "Got source-setup signal: element=%s",
            source.__class__.__name__,
        )

        if self._source_setup_callback:
            logger.debug("Running source-setup callback")
            self._source_setup_callback(source)

        if self._live_stream and hasattr(source.props, "is_live"):
            gst_logger.debug("Enabling live stream mode")
            source = cast(GstBase.BaseSrc, source)
            source.set_live(True)

        setup_proxy(source, self._config["proxy"])

    def _on_gst_bus_message(self, message: GstBusMessage) -> None:
        # Runs on whichever GStreamer thread posted the message. Hand it to
        # the actor thread, so that all message handling is single threaded.
        try:
            self.actor_ref.tell(message)
        except pykka.ActorDeadError:
            # The pipeline outlives the actor for a moment at teardown.
            gst_logger.debug("Dropped bus message, the audio actor is gone.")

    def _request_crossfade_tick(self) -> bool:
        try:
            self.actor_ref.tell("crossfade-tick")
        except pykka.ActorDeadError:
            return False
        return True

    def set_crossfade_prepare_callback(self, callback: Callable) -> None:
        self._crossfade_prepare_callback = callback

    def set_crossfade_handoff_callback(self, callback: Callable) -> None:
        self._crossfade_handoff_callback = callback

    def _request_crossfade_candidate(self, generation: int) -> bool:
        """Run a blocking Core query from the GLib thread, not GstAudio."""
        if self._crossfade_prepare_callback is None:
            return False
        try:
            fade_ms = self._config["audio"].get("crossfade_duration", 0) * 1000
            candidate = self._crossfade_prepare_callback(fade_ms)
            if candidate is not None:
                self.actor_ref.tell(("crossfade-candidate", generation, candidate))
            else:
                logger.warning(
                    "Crossfade candidate unavailable; no eligible next track "
                    "or Core is no longer playing (generation=%s)", generation
                )
        except pykka.ActorDeadError:
            pass
        except Exception:
            logger.exception("Could not prepare crossfade candidate")
        return False

    def _reset_crossfade_tracking(self) -> None:
        self._crossfade_generation += 1
        self._crossfade_candidate = None
        self._crossfade_window_detected = False
        self._crossfade_last_position = None

    def _handle_crossfade_tick(self) -> None:
        if self._pipeline is None or self._target_state != GstState.PLAYING:
            return

        position = self._pipeline.get_position()
        duration = self._pipeline.get_duration()
        if duration is None:
            return

        fade_ms = self._config["audio"].get("crossfade_duration", 0) * 1000
        if self._pipeline.has_crossfade_source:
            progress = (position - (duration - fade_ms)) / fade_ms
            self._pipeline.update_crossfade(progress)

            # Promote once the fade has reached the track end. Allow a
            # single timer interval's tolerance for coarse duration metadata.
            if (
                position >= duration - 150
                or self._pipeline.next_source_elapsed_ms >= fade_ms
            ):
                candidate = self._crossfade_candidate
                if candidate is not None and self._pipeline.promote_crossfade():
                    logger.info(
                        "Crossfade handoff: TLID=%s, new position=%s",
                        candidate["tlid"], self._pipeline.get_position(),
                    )
                    self._reset_crossfade_tracking()
                    callback = self._crossfade_handoff_callback
                    if callback is not None:
                        # The callback only queues a Core actor message. It
                        # must not synchronously ask Core from GstAudio.
                        callback(candidate["tlid"])
                return

        if (
            self._crossfade_last_position is not None
            and position < self._crossfade_last_position - 1500
        ):
            self._crossfade_window_detected = False
        self._crossfade_last_position = position
        if self._crossfade_window_detected:
            return

        if should_start_crossfade(
            position_ms=position,
            duration_ms=duration,
            crossfade_seconds=self._config["audio"].get("crossfade_duration", 0),
            has_next_track=True,
        ):
            self._crossfade_window_detected = True
            logger.info(
                "Crossfade window reached: position=%s, duration=%s",
                position, duration,
            )
            GLib.idle_add(
                self._request_crossfade_candidate, self._crossfade_generation
            )

    @override
    def on_receive(self, message: Any) -> None:
        match message:
            case GstAsyncDone():
                self._on_gst_async_done()
            case GstBuffering(percent, mode):
                self._on_gst_buffering(percent, mode)
            case GstEndOfStream():
                self._on_gst_end_of_stream()
            case GstError(error, debug):
                self._on_gst_error(error, debug)
            case GstMissingPlugin(description, installer_detail):
                self._on_gst_missing_plugin(description, installer_detail)
            case GstStateChanged(old_state, new_state, pending_state):
                self._on_gst_state_changed(old_state, new_state, pending_state)
            case GstStreamStart():
                self._on_gst_stream_start()
            case GstTag(tags):
                self._on_gst_tag(tags)
            case GstWarning(error, debug):
                self._on_gst_warning(error, debug)
            case "crossfade-tick":
                self._handle_crossfade_tick()
            case ("crossfade-candidate", generation, candidate):
                if (
                    generation != self._crossfade_generation
                    or self._target_state != GstState.PLAYING
                    or self._pipeline is None
                ):
                    return
                try:
                    ok = self._pipeline.start_crossfade_with_uri(
                        candidate["uri"], candidate["duration_ms"]
                    )
                except Exception:
                    logger.exception("Failed to start crossfade source")
                    return
                if ok:
                    self._crossfade_candidate = candidate
                    logger.info(
                        "Crossfade candidate: TLID=%s URI=%s",
                        candidate["tlid"], candidate["uri"],
                    )
                else:
                    logger.warning("Crossfade source was not started")

    def _on_gst_state_changed(
        self,
        old_state: GstState,
        new_state: GstState,
        pending_state: GstState,
    ) -> None:
        gst_logger.debug(
            "Got STATE_CHANGED bus message: old=%s new=%s pending=%s",
            old_state.name,
            new_state.name,
            pending_state.name,
        )

        if new_state == GstState.READY and pending_state == GstState.NULL:
            # HACK: We're not called on the last state change when going down to
            # NULL, so we rewrite the second to last call to get the expected
            # behavior.
            new_state = GstState.NULL
            pending_state = GstState.VOID_PENDING

        if pending_state != GstState.VOID_PENDING:
            return  # Ignore intermediate state changes

        if (new_playback_state := new_state.playback_state) is None:
            return  # READY is GStreamer specific, Mopidy has no such state.

        # The underlying playbin still owns the pipeline after a handoff.
        # It can report a stopped state for its retired source while a dynamic
        # source is the current audible track. Never forward that stale event.
        if (
            new_playback_state == PlaybackState.STOPPED
            and self._target_state == GstState.PLAYING
            and self._is_retired_playbin_event()
        ):
            logger.warning(
                "Ignoring retired playbin STOPPED while the dynamic track "
                "is still audible"
            )
            return

        old_playback_state, self.state = self.state, new_playback_state

        if (target_playback_state := self._target_state.playback_state) is None:
            return  # We are heading for READY, that is between tracks.

        if target_playback_state == new_playback_state:
            target_playback_state = None

        logger.debug(
            "Audio event: state_changed(old_state=%s, new_state=%s, target_state=%s)",
            old_playback_state,
            new_playback_state,
            target_playback_state,
        )
        AudioListener.send(
            "state_changed",
            old_state=old_playback_state,
            new_state=new_playback_state,
            target_state=target_playback_state,
        )
        if new_playback_state == PlaybackState.STOPPED:
            logger.debug("Audio event: stream_changed(uri=None)")
            AudioListener.send("stream_changed", uri=None)

        if "GST_DEBUG_DUMP_DOT_DIR" in os.environ:
            assert self._pipeline
            self._pipeline.debug_to_dot_file("mopidy")

    def _on_gst_buffering(self, percent: int, mode: Gst.BufferingMode) -> None:
        assert self._pipeline

        if self._target_state in (GstState.NULL, GstState.READY):
            gst_logger.debug("Skip buffering during track change.")
            return

        if mode == Gst.BufferingMode.LIVE:
            return  # Live sources stall in paused.

        level = logs.TRACE_LOG_LEVEL
        if percent < 10 and not self._buffering:
            self._pipeline.set_state(GstState.PAUSED)
            self._buffering = True
            level = logging.DEBUG
        if percent == 100:
            self._buffering = False
            if self._target_state == GstState.PLAYING:
                self._pipeline.set_state(GstState.PLAYING)
            level = logging.DEBUG

        gst_logger.log(level, "Got BUFFERING bus message: percent=%d%%", percent)

    def _is_retired_playbin_event(self) -> bool:
        """Check whether an EOS/STOP predates the audible dynamic track end."""
        if self._pipeline is None:
            return False
        if not (
            self._pipeline.has_dynamic_track
            or self._pipeline.has_crossfade_source
        ):
            return False
        return should_ignore_spurious_eos(
            position_ms=self._pipeline.get_position(),
            duration_ms=self._pipeline.get_duration(),
            has_dynamic_track=self._pipeline.has_dynamic_track,
            has_pending_crossfade=self._pipeline.has_crossfade_source,
        )

    def _on_gst_end_of_stream(self) -> None:
        gst_logger.debug("Got EOS (end of stream) bus message.")
        if self._target_state == GstState.PLAYING and self._is_retired_playbin_event():
            assert self._pipeline is not None
            logger.warning(
                "Ignoring premature EOS from playbin; active position=%s "
                "duration=%s, pending_crossfade=%s",
                self._pipeline.get_position(),
                self._pipeline.get_duration(),
                self._pipeline.has_crossfade_source,
            )
            return
        logger.debug("Audio event: reached_end_of_stream()")
        self._tags = {}
        AudioListener.send("reached_end_of_stream")

    def _on_gst_error(self, error: GLib.Error, debug: str | None) -> None:
        gst_logger.error(f"GStreamer error: {error.message}")
        gst_logger.debug(f"Got ERROR bus message: error={error!r} debug={debug!r}")

        # TODO: is this needed?
        self.stop_playback()

    def _on_gst_warning(self, error: GLib.Error, debug: str | None) -> None:
        gst_logger.warning(f"GStreamer warning: {error.message}")
        gst_logger.debug(f"Got WARNING bus message: error={error!r} debug={debug!r}")

    def _on_gst_async_done(self) -> None:
        gst_logger.debug("Got ASYNC_DONE bus message.")

    def _on_gst_tag(self, tags: GstTags) -> None:
        if gst_logger.isEnabledFor(logging.DEBUG):
            gst_logger.debug("Got TAG bus message: tags=%s", repr_tags(tags))

        # Postpone emitting tags until stream start.
        if self._pending_tags is not None:
            self._pending_tags.update(tags)
            return

        # TODO: Add proper tests for only emitting changed tags.
        unique = object()
        changed = []
        for key, value in tags.items():
            # Update any tags that changed, and store changed keys.
            if self._tags.get(key, unique) != value:
                self._tags[key] = value
                changed.append(key)

        if changed:
            logger.debug("Audio event: tags_changed(tags=%r)", changed)
            AudioListener.send("tags_changed", tags=changed)

    def _on_gst_missing_plugin(
        self,
        description: str,
        installer_detail: str | None,
    ) -> None:
        gst_logger.debug("Got missing-plugin bus message: description=%r", description)
        logger.warning("Could not find a %s to handle media.", description)
        if GstPbutils.install_plugins_supported():
            logger.info(
                "You might be able to fix this by running: 'gst-installer \"%s\"'",
                installer_detail,
            )
        # TODO: store the missing plugins installer info in a file so we can
        # can provide a 'mopidy install-missing-plugins' if the system has the
        # required helper installed?

    def _on_gst_stream_start(self) -> None:
        gst_logger.debug("Got STREAM_START bus message")
        uri = self._pending_uri
        logger.debug("Audio event: stream_changed(uri=%r)", uri)
        AudioListener.send("stream_changed", uri=uri)

        # Emit any postponed tags that we got after about-to-finish.
        tags, self._pending_tags = self._pending_tags, None
        self._tags = tags or {}

        if tags:
            logger.debug("Audio event: tags_changed(tags=%r)", tags.keys())
            AudioListener.send("tags_changed", tags=tags.keys())

    def _on_gst_position(self, position: DurationMs) -> None:
        logger.debug("Audio event: position_changed(position=%r)", position)
        AudioListener.send("position_changed", position=position)

    @override
    def set_uri(
        self,
        uri: str,
        live_stream: bool = False,
        download: bool = False,
    ) -> None:
        assert self._pipeline

        # HACK: Hack to workaround issue on Mac OS X where volume level
        # does not persist between track changes. mopidy/mopidy#886
        current_volume = self.mixer.get_volume() if self.mixer is not None else None

        if live_stream and download:
            logger.warning(
                "Ambiguous buffering flags: "
                "'live_stream' and 'download' should not both be set.",
            )
        self._reset_crossfade_tracking()
        self._pending_uri = uri
        self._pending_tags = {}
        self._live_stream = live_stream
        self._pipeline.set_uri(uri, download=download)

        if self.mixer is not None and current_volume is not None:
            self.mixer.set_volume(current_volume)

    @override
    def set_source_setup_callback(
        self,
        callback: Callable[[Gst.Element], None],
    ) -> None:
        self._source_setup_callback = callback

    @override
    def set_about_to_finish_callback(
        self,
        callback: Callable[[], None],
    ) -> None:
        self._about_to_finish_callback = callback

    @override
    def get_position(self) -> DurationMs:
        assert self._pipeline

        return self._pipeline.get_position()

    @override
    def set_position(self, position: DurationMs) -> bool:
        assert self._pipeline
        self._reset_crossfade_tracking()
        dynamic = self._pipeline.has_dynamic_track
        success = self._pipeline.seek(position)
        if success and dynamic:
            # The output-bin SEGMENT probe only observes playbin's source.
            # A seek in the promoted branch otherwise leaves Core's
            # _pending_position frozen at the requested time forever.
            self._on_gst_position(position)
        return success

    @override
    def start_playback(self) -> bool:
        assert self._pipeline

        self._target_state = GstState.PLAYING
        started = self._pipeline.set_state(GstState.PLAYING)
        return started

    @override
    def pause_playback(self) -> bool:
        assert self._pipeline

        self._target_state = GstState.PAUSED
        return self._pipeline.set_state(GstState.PAUSED)

    @override
    def prepare_change(self) -> bool:
        # This function *MUST* be called before changing URIs or doing
        # changes like updating data that is being pushed. The reason for this
        # is that GStreamer will reset all its state when it changes to
        # `Gst.State.READY`.
        assert self._pipeline

        self._buffering = False
        self._reset_crossfade_tracking()
        self._target_state = GstState.READY
        return self._pipeline.set_state(GstState.READY)

    @override
    def stop_playback(self) -> bool:
        assert self._pipeline

        self._buffering = False
        self._reset_crossfade_tracking()
        self._target_state = GstState.NULL
        return self._pipeline.set_state(GstState.NULL)

    @override
    def get_current_tags(self) -> dict[str, list[Any]]:
        # TODO: should this be a (deep) copy? most likely yes
        # TODO: should we return None when stopped?
        # TODO: support only fetching keys we care about?
        return self._tags

    @override
    def testing_gst__wait_for_state_change(self) -> None:
        assert self._pipeline

        self._pipeline.wait_for_state_change()
