from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from mopidy import exceptions
from mopidy._lib.gi import GLib, Gst, GstPbutils
from mopidy._lib.gst import (
    Signals,
    from_gst_taglist,
    from_gst_time,
    to_gst_time,
)
from mopidy.audio._gst.crossfade import calculate_gains
from mopidy.audio._gst.types import (
    GstAsyncDone,
    GstBuffering,
    GstBusMessage,
    GstEndOfStream,
    GstError,
    GstMissingPlugin,
    GstPlayFlags,
    GstState,
    GstStateChanged,
    GstStreamStart,
    GstTag,
    GstWarning,
)
from mopidy.types import DurationMs

if TYPE_CHECKING:
    from collections.abc import Callable

    from mopidy.config import Config

logger = logging.getLogger(__name__)

# This logger is only meant for debug logging of low level GStreamer info such
# as callbacks, event, messages and direct interaction with GStreamer such as
# set_state() on a pipeline.
gst_logger = logging.getLogger("mopidy.audio.gst")


class GstOutputBin(Gst.Bin):
    """A bin that sends its input to one or more audio outputs."""

    def __init__(self) -> None:
        Gst.Bin.__init__(self, name="outputs")

        tee = Gst.ElementFactory.make("tee")
        if tee is None:
            msg = "Failed to create GStreamer tee."
            raise exceptions.AudioException(msg)
        self._tee = tee
        self.add(self._tee)

        tee_sink = self._tee.get_static_pad("sink")
        if tee_sink is None:
            msg = "Failed to get sink from GStreamer tee."
            raise exceptions.AudioException(msg)
        ghost_pad = Gst.GhostPad.new("sink", tee_sink)
        self.add_pad(ghost_pad)

    def add_output(self, description: str) -> None:
        # NOTE: This only works while the pipeline is not running yet.
        try:
            output = Gst.parse_bin_from_description(
                description,
                ghost_unlinked_pads=True,
            )
        except GLib.Error as exc:
            logger.error('Failed to create audio output "%s": %s', description, exc)
            msg = f"Failed to create audio output {description!r}"
            raise exceptions.AudioException(msg) from exc

        self._add(output)
        logger.info('Audio output set to "%s"', description)

    def _add(self, element: Gst.Element) -> None:
        self.add(element)

        queue = Gst.ElementFactory.make("queue")
        if queue is None:
            msg = "Failed to create GStreamer queue."
            raise exceptions.AudioException(msg)
        self.add(queue)

        queue.link(element)
        self._tee.link(queue)


def make_output_bin(output: str) -> Gst.Element:
    """Make the element that the audio sink sends its output to."""
    # We don't want to use outputs for regular testing, so just install
    # an unsynced fakesink when someone asks for a 'testoutput'.
    if output == "testoutput":
        fakesink = Gst.ElementFactory.make("fakesink")
        if fakesink is None:
            msg = "Failed to create GStreamer fakesink element."
            raise exceptions.AudioException(msg)
        return fakesink

    output_bin = GstOutputBin()
    output_bin.add_output(output)
    return output_bin


class GstPlaybackPipeline:
    """The GStreamer element graph used for playback.

    This is the only code that touches the GStreamer elements. It knows
    nothing about Mopidy's playback state, events or backends.
    """

    playbin: Gst.Element
    """The playbin element, which is the root of the graph."""

    output_bin: Gst.Element
    """The element that the audio sink sends its output to."""

    queue: Gst.Element
    """The queue in front of the audio sink. Seek events go here."""

    volume: Gst.Element
    """The volume element, which the software mixer controls."""

    def __init__(
        self,
        config: Config,
        *,
        on_bus_message: Callable[[GstBusMessage], None],
        on_position: Callable[[DurationMs], None],
        on_about_to_finish: Callable[[Gst.Element], None],
        on_source_setup: Callable[[Gst.Element, Gst.Element], None],
    ) -> None:
        self._config = config
        self._signals = Signals()
        self._event_handler_id: int | None = None
        self._pad: Gst.Pad | None = None

        self.crossfade_mixer: Gst.Element | None = None
        self.crossfade_volume_a: Gst.Element | None = None
        self.crossfade_volume_b: Gst.Element | None = None
        self._initial_fade_volume: Gst.Element | None = None
        self._active_dynamic: dict | None = None
        self._next_dynamic: dict | None = None
        self._dynamic_index = 0
        self._pause_snapshot: DurationMs | None = None

        self.playbin = self._make_playbin(on_about_to_finish, on_source_setup)
        self._setup_message_handling(on_bus_message)
        self.output_bin = make_output_bin(self._config["audio"]["output"])
        self._setup_event_handling(on_position)
        self.queue, self.volume = self._make_audio_sink()

    def _make_playbin(
        self,
        on_about_to_finish: Callable[[Gst.Element], None],
        on_source_setup: Callable[[Gst.Element, Gst.Element], None],
    ) -> Gst.Element:
        playbin = Gst.ElementFactory.make("playbin")
        if playbin is None:
            msg = "Failed to create GStreamer playbin."
            raise exceptions.AudioException(msg)
        playbin.set_property("flags", GstPlayFlags.AUDIO)

        # TODO: turn into config values...
        playbin.set_property("buffer-size", 5 << 20)  # 5MB
        playbin.set_property("buffer-duration", 5 * Gst.SECOND)

        self._signals.connect(playbin, "source-setup", on_source_setup)
        self._signals.connect(playbin, "about-to-finish", on_about_to_finish)

        return playbin

    def _make_audio_sink(self) -> tuple[Gst.Element, Gst.Element]:
        audio_sink = Gst.ElementFactory.make("bin", "audio-sink")
        if audio_sink is None:
            msg = "Failed to create GStreamer bin 'audio-sink'."
            raise exceptions.AudioException(msg)
        audio_sink = cast(Gst.Bin, audio_sink)

        queue = Gst.ElementFactory.make("queue")
        if queue is None:
            msg = "Failed to create GStreamer queue element."
            raise exceptions.AudioException(msg)

        volume = Gst.ElementFactory.make("volume")
        if volume is None:
            msg = "Failed to create GStreamer volume element."
            raise exceptions.AudioException(msg)

        # Queue element to buy us time between the about-to-finish event and
        # the actual switch, i.e. about to switch can block for longer thanks
        # to this queue.

        # TODO: See if settings should be set to minimize latency. Earlier
        # attempts at that broke on a few systems, so leave the default to
        # play it safe.
        buffer_time = self._config["audio"]["buffer_time"]
        if buffer_time is not None and buffer_time > 0:
            queue.set_property("max-size-time", buffer_time * Gst.MSECOND)

        audio_sink.add(queue)
        audio_sink.add(self.output_bin)
        audio_sink.add(volume)

        if self._config["audio"].get("crossfade_duration", 0) > 0:
            fade_volume = Gst.ElementFactory.make(
                "volume", "crossfade-volume-a"
            )
            audiomixer = Gst.ElementFactory.make(
                "audiomixer", "crossfade-mixer"
            )

            if fade_volume is None or audiomixer is None:
                raise exceptions.AudioException(
                    "Failed to create crossfade elements."
                )

            audio_sink.add(fade_volume)
            audio_sink.add(audiomixer)

            if not queue.link(fade_volume):
                raise exceptions.AudioException(
                    "Failed to link crossfade volume."
                )

            if not fade_volume.link(audiomixer):
                raise exceptions.AudioException(
                    "Failed to link audiomixer input."
                )

            if not audiomixer.link(volume):
                raise exceptions.AudioException(
                    "Failed to link audiomixer output."
                )
            self.crossfade_mixer = audiomixer
            self.crossfade_volume_a = fade_volume
            self._initial_fade_volume = fade_volume
            self.crossfade_volume_b = None
        else:
            queue.link(volume)

        volume.link(self.output_bin)

        queue_sink = queue.get_static_pad("sink")
        if queue_sink is None:
            msg = "Failed to get sink from GStreamer queue."
            raise exceptions.AudioException(msg)
        ghost_pad = Gst.GhostPad.new("sink", queue_sink)
        audio_sink.add_pad(ghost_pad)

        self.playbin.set_property("audio-sink", audio_sink)

        return queue, volume

    def _setup_message_handling(
        self,
        on_bus_message: Callable[[GstBusMessage], None],
    ) -> None:
        def sync_handler(_bus: Gst.Bus, message: Gst.Message) -> Gst.BusSyncReply:
            if (decoded := self._decode_bus_message(message)) is not None:
                on_bus_message(decoded)
            return Gst.BusSyncReply.DROP

        if (bus := self.playbin.get_bus()) is None:
            return

        bus.set_sync_handler(sync_handler)

    def _decode_bus_message(  # noqa: C901, PLR0911
        self,
        message: Gst.Message,
    ) -> GstBusMessage | None:
        """Turn a bus message into one of our records, or drop it.

        Runs on the GStreamer thread that posted the message. The parse
        methods only read the structure the message already carries, so they
        take no lock and cannot block that thread.
        """
        # Extended message types have the `Gst.MessageType.EXTENDED` bit (2³¹)
        # set. On 32-bit platforms with PyGObject < 3.51.0, comparing such a
        # type with a `Gst.MessageType` member leaves an `OverflowError` set,
        # and the next C call fails. We use none of these types, so we drop
        # them with integer arithmetic only. PyGObject 3.51.0 (a pre-release)
        # fixed this, and 3.52.0 is the first stable release with the fix.
        # Remove this when the minimum PyGObject version is 3.52.0 or newer.
        # See https://github.com/mopidy/mopidy/issues/2030.
        if int(message.type) & int(Gst.MessageType.EXTENDED):
            return None

        match message.type:
            case Gst.MessageType.ASYNC_DONE:
                return GstAsyncDone()
            case Gst.MessageType.BUFFERING:
                mode, _, _, _ = message.parse_buffering_stats()
                return GstBuffering(message.parse_buffering(), mode)
            case Gst.MessageType.EOS:
                return GstEndOfStream()
            case Gst.MessageType.ERROR:
                return GstError(*message.parse_error())
            case Gst.MessageType.ELEMENT if GstPbutils.is_missing_plugin_message(
                message
            ):
                return GstMissingPlugin(
                    description=GstPbutils.missing_plugin_message_get_description(
                        message
                    ),
                    installer_detail=(
                        GstPbutils.missing_plugin_message_get_installer_detail(message)
                    ),
                )
            case Gst.MessageType.STATE_CHANGED if message.src == self.playbin:
                # Only the playbin's own state matters.
                old_state, new_state, pending_state = message.parse_state_changed()
                return GstStateChanged(
                    GstState(old_state),
                    GstState(new_state),
                    GstState(pending_state),
                )
            case Gst.MessageType.STREAM_START:
                return GstStreamStart()
            case Gst.MessageType.TAG:
                return GstTag(from_gst_taglist(message.parse_tag()))
            case Gst.MessageType.WARNING:
                return GstWarning(*message.parse_warning())
            case _:
                return None

    def _setup_event_handling(
        self,
        on_position: Callable[[DurationMs], None],
    ) -> None:
        def on_pad_event(
            _pad: Gst.Pad,
            pad_probe_info: Gst.PadProbeInfo,
        ) -> Gst.PadProbeReturn:
            if (event := pad_probe_info.get_event()) is None:
                return Gst.PadProbeReturn.OK
            if event.type == Gst.EventType.SEGMENT:
                on_position(self._decode_segment(event.parse_segment()))
            return Gst.PadProbeReturn.OK

        if (pad := self.output_bin.get_static_pad("sink")) is None:
            return

        self._pad = pad
        self._event_handler_id = pad.add_probe(
            Gst.PadProbeType.EVENT_BOTH,
            on_pad_event,
        )

    @staticmethod
    def _decode_segment(segment: Gst.Segment) -> DurationMs:
        gst_logger.debug(
            "Got SEGMENT pad event: "
            "rate=%(rate)s format=%(format)s start=%(start)s stop=%(stop)s "
            "position=%(position)s",
            {
                "rate": segment.rate,
                "format": Gst.Format.get_name(segment.format),
                "start": segment.start,
                "stop": segment.stop,
                "position": segment.position,
            },
        )
        return from_gst_time(segment.position)


    def _running_time_ns(self) -> int:
        clock = self.playbin.get_clock()
        if clock is None:
            return 0
        return max(0, int(clock.get_time() - self.playbin.get_base_time()))

    @property
    def has_dynamic_track(self) -> bool:
        return self._active_dynamic is not None

    @property
    def has_crossfade_source(self) -> bool:
        return self._next_dynamic is not None

    @property
    def next_source_elapsed_ms(self) -> int:
        """Elapsed pipeline-running time since the overlap started."""
        if self._next_dynamic is None:
            return 0
        return max(
            0,
            (self._running_time_ns() - self._next_dynamic["start_rt_ns"])
            // Gst.MSECOND,
        )

    def _remove_dynamic(self, branch: dict) -> None:
        """Remove a stopped, muted dynamic branch and its mixer input."""
        elements = branch["elements"]
        source = branch["source"]
        try:
            source.disconnect(branch["handler_id"])
        except (TypeError, ValueError, RuntimeError):
            pass

        # Stop the source first so its streaming thread cannot push while
        # pads are being detached. Dynamic graph manipulation is delicate.
        for element in elements:
            element.set_state(Gst.State.NULL)

        source_pad = branch["volume"].get_static_pad("src")
        peer = source_pad.get_peer() if source_pad is not None else None
        if source_pad is not None and peer is not None:
            source_pad.unlink(peer)
            if self.crossfade_mixer is not None:
                self.crossfade_mixer.release_request_pad(peer)

        queue_sink = branch["queue"].get_static_pad("sink")
        if queue_sink is not None and (peer_pad := queue_sink.get_peer()):
            peer_pad.unlink(queue_sink)

        audio_sink = branch["bin"]
        for element in reversed(elements):
            if element.get_parent() == audio_sink:
                audio_sink.remove(element)

    def cancel_pending_crossfade(self) -> None:
        """Discard an uncommitted fade, e.g. after a manual seek."""
        if self._next_dynamic is not None:
            self._remove_dynamic(self._next_dynamic)
            self._next_dynamic = None
            self.crossfade_volume_b = None
        if self.crossfade_volume_a is not None:
            self.crossfade_volume_a.set_property("volume", 1.0)

    def _reset_crossfade_sources(self) -> None:
        self.cancel_pending_crossfade()
        if self._active_dynamic is not None:
            self._remove_dynamic(self._active_dynamic)
            self._active_dynamic = None
        self.crossfade_volume_a = self._initial_fade_volume
        if self.crossfade_volume_a is not None:
            self.crossfade_volume_a.set_property("volume", 1.0)
        self._pause_snapshot = None

    def start_crossfade_with_uri(self, uri: str, duration_ms: int | None) -> bool:
        """Begin playing a candidate URI in the existing GStreamer mixer.

        Only local, finite tracks are supported in this prototype.
        """
        if (
            self.crossfade_mixer is None
            or self.crossfade_volume_a is None
            or self._next_dynamic is not None
            or not uri.startswith("file://")
            or (duration_ms is not None and duration_ms <= 0)
        ):
            return False

        audio_sink = self.crossfade_mixer.get_parent()
        if audio_sink is None or self.playbin.get_clock() is None:
            return False

        self._dynamic_index += 1
        suffix = str(self._dynamic_index)
        source = Gst.ElementFactory.make("uridecodebin", f"xf-source-{suffix}")
        queue = Gst.ElementFactory.make("queue", f"xf-queue-{suffix}")
        convert = Gst.ElementFactory.make("audioconvert")
        resample = Gst.ElementFactory.make("audioresample")
        volume = Gst.ElementFactory.make("volume", f"xf-volume-{suffix}")
        elements = (source, queue, convert, resample, volume)
        if any(element is None for element in elements):
            raise exceptions.AudioException("Cannot create crossfade decoder")

        source.set_property("uri", uri)
        volume.set_property("volume", 0.0)
        start_rt_ns = self._running_time_ns()
        added = []
        try:
            for element in elements:
                audio_sink.add(element)
                added.append(element)
            if not (
                queue.link(convert)
                and convert.link(resample)
                and resample.link(volume)
                and volume.link(self.crossfade_mixer)
            ):
                raise exceptions.AudioException("Cannot link crossfade decoder")

            volume_src = volume.get_static_pad("src")
            if volume_src is None:
                raise exceptions.AudioException("Missing crossfade volume pad")
            volume_src.set_offset(start_rt_ns)
        except Exception:
            for element in added:
                element.set_state(Gst.State.NULL)
            pad = volume.get_static_pad("src")
            peer = pad.get_peer() if pad is not None else None
            if pad is not None and peer is not None:
                pad.unlink(peer)
                self.crossfade_mixer.release_request_pad(peer)
            for element in reversed(added):
                audio_sink.remove(element)
            raise

        def on_pad_added(_element: Gst.Element, pad: Gst.Pad) -> None:
            caps = pad.get_current_caps() or pad.query_caps(None)
            if caps is None or caps.get_size() == 0:
                return
            if not caps.get_structure(0).get_name().startswith("audio/x-raw"):
                return
            queue_sink = queue.get_static_pad("sink")
            if queue_sink is not None and not queue_sink.is_linked():
                result = pad.link(queue_sink)
                if result != Gst.PadLinkReturn.OK:
                    logger.error("Crossfade decoder pad link failed: %s", result)

        handler_id = source.connect("pad-added", on_pad_added)
        branch = {
            "elements": elements,
            "source": source,
            "queue": queue,
            "volume": volume,
            "bin": audio_sink,
            "handler_id": handler_id,
            "start_rt_ns": start_rt_ns,
            "duration_ms": duration_ms,
            "seek_base_ms": 0,
        }
        self._next_dynamic = branch
        self.crossfade_volume_b = volume

        for element in reversed(elements):
            if not element.sync_state_with_parent():
                self.cancel_pending_crossfade()
                raise exceptions.AudioException("Cannot activate crossfade decoder")

        logger.info("Dynamic crossfade source started: %s", uri)
        return True

    def update_crossfade(self, progress: float) -> None:
        """Apply the selected gain curve to both active audio branches."""
        if self.crossfade_volume_a is None or self.crossfade_volume_b is None:
            return
        a, b = calculate_gains(
            max(0.0, min(1.0, progress)),
            self._config["audio"].get("crossfade_curve", "linear"),
        )
        self.crossfade_volume_a.set_property("volume", a)
        self.crossfade_volume_b.set_property("volume", b)

    def promote_crossfade(self) -> bool:
        """The faded-in source now provides the current track's audio.

        The underlying playbin remains the clock owner. Track identity is
        updated separately by the Core actor after this returns.
        """
        if self._next_dynamic is None:
            return False
        old_dynamic = self._active_dynamic
        if self.crossfade_volume_a is not None:
            self.crossfade_volume_a.set_property("volume", 0.0)
        self.crossfade_volume_b.set_property("volume", 1.0)
        self.crossfade_volume_a = self.crossfade_volume_b
        self.crossfade_volume_b = None
        self._active_dynamic = self._next_dynamic
        self._next_dynamic = None
        self._pause_snapshot = None
        if old_dynamic is not None:
            self._remove_dynamic(old_dynamic)
        return True

    def set_uri(self, uri: str, *, download: bool = False) -> None:
        """Set the URI to play, and the buffering flags to use for it."""
        flags = GstPlayFlags.AUDIO
        if download:
            flags |= GstPlayFlags.DOWNLOAD

        logger.debug(f"Flags: {flags}")
        self.playbin.set_property("flags", flags)
        self.playbin.set_property("uri", uri)

    def set_state(self, state: GstState) -> bool:
        """Set the state of the playbin.

        Returns `True` if successful, else `False`.
        """
        if state == GstState.PAUSED and self._active_dynamic is not None:
            self._pause_snapshot = self.get_position()
        result = self.playbin.set_state(state.value)
        if state in (GstState.NULL, GstState.READY):
            self._reset_crossfade_sources()
        elif state == GstState.PLAYING:
            self._pause_snapshot = None
        gst_logger.debug(
            "Changing state to %s: result=%s",
            state.name,
            result.value_name,
        )

        if result == Gst.StateChangeReturn.FAILURE:
            logger.warning("Setting GStreamer state to %s failed", state.name)
            return False
        # TODO: at this point we could already emit stopped event instead
        # of faking it in the message handling when result=OK
        return True

    def get_position(self) -> DurationMs:
        """Return the time position of the currently audible source."""
        if self._active_dynamic is not None:
            if self._pause_snapshot is not None:
                return self._pause_snapshot
            active = self._active_dynamic
            ms = active["seek_base_ms"] + max(
                0,
                (self._running_time_ns() - active["start_rt_ns"])
                // Gst.MSECOND,
            )
            return DurationMs(int(ms))
        success, position = self.playbin.query_position(Gst.Format.TIME)

        if not success:
            # TODO: take state into account for this and possibly also return
            # None as the unknown value instead of zero?
            logger.debug("Position query failed")
            return DurationMs(0)

        return from_gst_time(position)

    def get_duration(self) -> DurationMs | None:
        """Return the active source duration, if available."""
        if self._active_dynamic is not None:
            active = self._active_dynamic
            if active["duration_ms"] is not None:
                return DurationMs(active["duration_ms"])
            # Tracklist entries added from file:// URIs do not always have
            # populated length metadata. Ask the active decoder if possible.
            ok, value = active["source"].query_duration(Gst.Format.TIME)
            if ok and value != Gst.CLOCK_TIME_NONE and value > 0:
                active["duration_ms"] = int(value // Gst.MSECOND)
                return DurationMs(active["duration_ms"])
            return None
        success, duration = self.playbin.query_duration(Gst.Format.TIME)

        if not success or duration == Gst.CLOCK_TIME_NONE:
            return None

        return from_gst_time(duration)

    def seek(self, position: DurationMs) -> bool:
        """Seek the currently audible track rather than a retired source."""
        self.cancel_pending_crossfade()
        gst_position = to_gst_time(position)
        gst_logger.debug("Sending flushing seek: position=%r", gst_position)

        if self._active_dynamic is not None:
            # Direct the seek into the dynamic track, not the retired playbin.
            active = self._active_dynamic
            now = self._running_time_ns()
            source_pad = active["volume"].get_static_pad("src")
            if source_pad is None:
                return False

            # A pad offset adjusts running time, not file position.
            # Prepare the new segment before issuing a flushing seek.
            previous_offset = source_pad.get_offset()
            source_pad.set_offset(now)

            if not active["volume"].seek_simple(
                Gst.Format.TIME, Gst.SeekFlags.FLUSH, gst_position
            ):
                source_pad.set_offset(previous_offset)
                return False

            active["seek_base_ms"] = int(position)
            active["start_rt_ns"] = now
            self._pause_snapshot = (
                DurationMs(position) if self._pause_snapshot is not None else None
            )
            return True

        # The main playbin handles a mixer-based graph correctly, while
        # the original queue target remains right for the normal mode.
        target = (
            self.playbin
            if self._config["audio"].get("crossfade_duration", 0) > 0
            else self.queue
        )
        return target.seek_simple(
            Gst.Format.TIME, Gst.SeekFlags.FLUSH, gst_position
        )

    def debug_to_dot_file(self, file_name: str) -> None:
        """Write the graph to a dot file, for debugging."""
        Gst.debug_bin_to_dot_file(
            bin=cast(Gst.Bin, self.playbin),
            details=Gst.DebugGraphDetails.ALL,
            file_name=file_name,
        )

    def wait_for_state_change(self) -> None:
        """Block until any pending state changes are complete."""
        self.playbin.get_state(timeout=Gst.CLOCK_TIME_NONE)

    def teardown(self) -> None:
        if (bus := self.playbin.get_bus()) is not None:
            bus.set_sync_handler(None)

        if self._pad is not None and self._event_handler_id is not None:
            self._pad.remove_probe(self._event_handler_id)
        self._event_handler_id = None

        self._signals.disconnect_all()
        self.playbin.set_state(Gst.State.NULL)
        self._reset_crossfade_sources()
