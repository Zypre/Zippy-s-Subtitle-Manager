"""Inspect a container and pull subtitle tracks out of it."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import langs
from .formats import Event, load_events
from .tools import Toolset, run

#: Codec markers for bitmap subtitles. These carry pictures, not text, so we
#: can read their *timing* but never their content.
IMAGE_CODECS = ("PGS", "VOBSUB", "DVBSUB", "TEXTST", "HDMV", "S_DVBSUB", "S_HDMV")


class ProbeError(RuntimeError):
    pass


@dataclass
class Track:
    id: int
    sub_index: int              # index among subtitle tracks only (for ffmpeg -map)
    codec: str
    codec_id: str
    language: str | None        # canonical, e.g. 'chi'
    raw_language: str
    name: str
    forced: bool = False
    default: bool = False
    uid: int | None = None          # Matroska track UID, for mkvpropedit

    @property
    def is_image(self) -> bool:
        blob = f"{self.codec_id} {self.codec}".upper()
        return any(k in blob for k in IMAGE_CODECS)

    def describe(self) -> str:
        flags = "".join(f" [{f}]" for f, on in
                        (("forced", self.forced), ("default", self.default)) if on)
        kind = " (image-based)" if self.is_image else ""
        name = f'  "{self.name}"' if self.name else ""
        return (f"track {self.id}: {self.raw_language or 'und'} "
                f"{self.codec}{kind}{name}{flags}")


@dataclass
class AudioTrack:
    id: int
    language: str | None
    raw_language: str
    name: str
    default: bool = False
    uid: int | None = None


@dataclass
class MediaInfo:
    path: Path
    title: str
    tracks: list[Track]
    audio: list["AudioTrack"] = field(default_factory=list)
    duration_s: float | None = None

    @property
    def default_audio_language(self) -> str | None:
        """The language you are most likely actually listening to."""
        for t in self.audio:
            if t.default and t.language:
                return t.language
        return self.audio[0].language if self.audio else None

    def subtitle_tracks(self, language: str | None = None,
                        text_only: bool = True) -> list[Track]:
        out = self.tracks
        if language:
            out = [t for t in out if langs.matches(t.raw_language, language)]
        if text_only:
            out = [t for t in out if not t.is_image]
        return out

    def best_track(self, language: str, text_only: bool = True) -> Track | None:
        """Prefer a non-forced, default-flagged, full-dialogue track."""
        cands = self.subtitle_tracks(language, text_only=text_only)
        if not cands:
            return None

        def rank(t: Track) -> tuple:
            name = (t.name or "").lower()
            penalty = any(w in name for w in ("forced", "signs", "songs", "sign/song"))
            return (t.forced or penalty, not t.default, t.is_image, t.id)

        return sorted(cands, key=rank)[0]


def probe(path: Path, tools: Toolset) -> MediaInfo:
    res = run([str(tools.mkvmerge), "-J", str(path)])
    if res.returncode not in (0, 1):   # 1 = warnings, still usable JSON
        raise ProbeError(f"mkvmerge could not read {path.name}: "
                         f"{(res.stderr or res.stdout)[:300]}")
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"mkvmerge returned unreadable JSON for {path.name}: {exc}")

    tracks: list[Track] = []
    sub_index = 0
    for t in data.get("tracks", []):
        if t.get("type") != "subtitles":
            continue
        props = t.get("properties", {})
        raw = props.get("language_ietf") or props.get("language") or "und"
        tracks.append(Track(
            id=int(t["id"]),
            sub_index=sub_index,
            codec=t.get("codec", "unknown"),
            codec_id=str(props.get("codec_id", "")),
            language=langs.normalize(raw),
            raw_language=str(props.get("language") or raw),
            name=str(props.get("track_name", "") or ""),
            forced=bool(props.get("forced_track", False)),
            default=bool(props.get("default_track", False)),
            uid=props.get("uid"),
        ))
        sub_index += 1

    audio: list[AudioTrack] = []
    for t in data.get("tracks", []):
        if t.get("type") != "audio":
            continue
        props = t.get("properties", {})
        raw = props.get("language_ietf") or props.get("language") or "und"
        audio.append(AudioTrack(
            id=int(t["id"]),
            language=langs.normalize(raw),
            raw_language=str(props.get("language") or raw),
            name=str(props.get("track_name", "") or ""),
            default=bool(props.get("default_track", False)),
            uid=props.get("uid"),
        ))

    container = data.get("container", {}).get("properties", {})
    duration = container.get("duration")
    return MediaInfo(
        path=path,
        title=str(container.get("title", "") or ""),
        tracks=tracks,
        audio=audio,
        duration_s=(duration / 1_000_000_000) if isinstance(duration, (int, float)) else None,
    )


def extract_events(media: MediaInfo, track: Track, tools: Toolset,
                   workdir: Path) -> list[Event]:
    """Pull one text subtitle track out and parse it."""
    if track.is_image:
        raise ProbeError(
            f"track {track.id} is image-based ({track.codec}); "
            f"it has no text to extract"
        )
    stem = workdir / f"track{track.id}"
    # mkvextract writes the track's native format whatever extension we give it.
    target = stem.with_suffix(".sub_out")
    res = run([str(tools.mkvextract), str(media.path), "tracks",
               f"{track.id}:{target}"])
    if target.exists() and target.stat().st_size > 0:
        events = load_events(target)
        if events:
            return events

    if tools.ffmpeg:
        alt = stem.with_suffix(".ass")
        run([str(tools.ffmpeg), "-y", "-loglevel", "error", "-i", str(media.path),
             "-map", f"0:s:{track.sub_index}", "-c:s", "ass", str(alt)])
        if alt.exists() and alt.stat().st_size > 0:
            events = load_events(alt)
            if events:
                return events

    raise ProbeError(
        f"could not extract track {track.id} from {media.path.name}"
        + (f": {res.stderr.strip()[:200]}" if res.stderr else "")
    )


def extract_texts(media: MediaInfo, tracks: list[Track], tools: Toolset,
                  workdir: Path, should_stop=None) -> dict[int, list[Event]] | None:
    """Every requested text track, in one pass over the file.

    Pulling one subtitle track out of a Matroska file means reading the whole
    file, since its blocks are spread through every cluster -- but mkvextract
    writes as many tracks as it is asked for during that one read. So asking
    for all of them costs the same as asking for one. Image-based tracks are
    skipped: they carry pictures, not text.

    Returns None if `should_stop()` interrupted it. A track that would not
    parse comes back as an empty list rather than failing the rest.
    """
    from .tools import run_cancellable

    wanted = [t for t in tracks if not t.is_image]
    if not wanted:
        return {}
    targets = {t.id: workdir / f"text{t.id}.sub_out" for t in wanted}
    cmd = [str(tools.mkvextract), str(media.path), "tracks",
           *(f"{tid}:{path}" for tid, path in targets.items())]
    res = run_cancellable(cmd, should_stop or (lambda: False))
    if res is None:
        return None
    if res.returncode not in (0, 1):
        raise ProbeError(f"mkvextract could not read {media.path.name}: "
                         f"{(res.stderr or res.stdout).strip()[:300]}")
    out: dict[int, list[Event]] = {}
    for tid, path in targets.items():
        try:
            out[tid] = load_events(path) if path.exists() else []
        except (OSError, ValueError):
            out[tid] = []
    return out


def track_onsets_ms(media: MediaInfo, track: Track, tools: Toolset,
                    workdir: Path) -> list[float]:
    """Cue start times for a track, in ms. Works for image tracks too."""
    ts = workdir / f"ts{track.id}.txt"
    run([str(tools.mkvextract), str(media.path), "timestamps_v2", f"{track.id}:{ts}"])
    if not ts.exists():
        return []
    out: list[float] = []
    with open(ts, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                out.append(float(line))
            except ValueError:
                pass
    return sorted(out)


def mux(video: Path, ass_path: Path, out_path: Path, tools: Toolset,
        track_name: str, default: bool = True) -> None:
    cmd = [
        str(tools.mkvmerge), "-o", str(out_path), str(video),
        "--language", "0:und",
        "--track-name", f"0:{track_name}",
        "--default-track-flag", f"0:{'yes' if default else 'no'}",
        str(ass_path),
    ]
    res = run(cmd)
    if res.returncode not in (0, 1):
        out_path.unlink(missing_ok=True)
        raise ProbeError(f"mkvmerge mux failed: {(res.stderr or res.stdout)[:400]}")


def make_workdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="dualsubs_"))
