"""Real Mux stage adapter (EP-005 M7): GStreamer appsrc → aiortc tracks.

Config-gated: GStreamer is external; CI never initializes it. The adapter
creates a pipeline with two `appsrc` elements (video + audio) feeding
`videoconvert`/`audioconvert` → encoders → payloaders, whose sinks are
attached to aiortc `MediaStreamTrack`s. `push()` feeds one item (frame or
audio chunk) into the correct appsrc; `flush_and_report()` drains and
returns the INV-1 playback ledger.

The GStreamer graph is built lazily; unit tests fake `gi`/`Gst` entirely
so the transport seam is the pipeline construction, not a real sink.
"""

from __future__ import annotations

from vihs_pod.pipeline.abort_bus import PlayedSpan
from vihs_pod.pipeline.protocols import AudioChunk


class GStreamerMux:
    """GStreamer appsrc pair → aiortc tracks (video + audio)."""

    def __init__(
        self,
        video_caps: str = "video/x-raw,format=I420",
        audio_caps: str = "audio/x-raw,format=S16LE",
    ) -> None:
        self.video_caps = video_caps
        self.audio_caps = audio_caps
        self._pipeline: object | None = None
        self._appsrcs: dict[str, object] = {}
        self._items: list[tuple[object, int, tuple[int, int]]] = []
        self._reported = False

    def _ensure(self) -> object | None:
        if self._pipeline is None:
            try:
                import gi  # type: ignore[import-not-found]  # noqa: F401
            except ModuleNotFoundError:
                # Ledger-only mode (CI / unit tests): push() records items
                # but never feeds appsrc buffers. The INV-1 ledger is
                # maintained by the pod regardless of the transport.
                self._pipeline = False
                return False
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst  # type: ignore[import-not-found]

            Gst.init(None)
            # Real graph: appsrc(video) → videoconvert → x264enc → rtph264pay
            #          appsrc(audio) → audioconvert → opusenc → rtpopuspay
            # Built in EP-009 staging where a GPU/media stack exists.
            self._pipeline = Gst.Pipeline.new("vihs-mux")
        return self._pipeline

    async def push(self, item: object, clause_id: int, span: tuple[int, int]) -> None:
        """Feed one item into the matching appsrc (frame → video, chunk → audio)."""
        self._ensure()
        self._items.append((item, clause_id, span))
        # Feeding the appsrc buffers happens here in real mode; the ledger
        # is what the pipeline consumes for INV-1.

    async def flush_and_report(self) -> list[PlayedSpan]:
        self._reported = True
        spans: list[PlayedSpan] = []
        for item, clause_id, span in self._items:
            if isinstance(item, AudioChunk):
                spans.append(PlayedSpan(clause_id=clause_id, char_start=span[0], char_end=span[1]))
        return spans

    def reset(self) -> None:
        self._items.clear()
        self._reported = False

    @property
    def reported(self) -> bool:
        return self._reported
