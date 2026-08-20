#!/usr/bin/env python3
"""
video_auto_editor.py
=====================

Bulk pipeline that, for every input video:

  1. DETECTS the intro and outro (branded opener / end card, credits, outro CTA)
     and cuts them off both ends.
  2. SPLITS everything in between into consecutive N-second PARTS (default 60s).
     A 3-minute video with no intro/outro becomes 3 x 1-minute part files,
     automatically -- no manual math needed. This is the key difference from
     a single-clip tool: every video can produce MULTIPLE output files.
  3. REMOVES a static logo/watermark/credit-text region (if present), baked
     into the same encode.
  4. OPTIMIZES every output part (resolution/bitrate cap + faststart) so the
     final files are small and fast-loading, in the same encode pass as the
     intro/outro trim and the watermark removal -- no separate re-encode step.

Works on a single video OR a whole folder of videos in one command (batch mode
is also what you want if many videos share the same intro/outro/logo, since it
can detect those once instead of guessing per-file).

--------------------------------------------------------------------------
QUICK ANSWERS TO WHAT YOU ASKED
--------------------------------------------------------------------------

Bulk / batch, split each long video into multiple N-second parts, strip
intro+outro+logo, all in one command:

    python video_auto_editor.py --mode batch --batch ./videos_in --outdir ./videos_out \
        --clip-seconds 60 --shared-watermark-from-first

Single video into multiple parts (e.g. a 3-min video -> three 1-min parts):

    python video_auto_editor.py --input movie.mp4 --output out.mp4 --clip-seconds 60

    -> writes out_part01.mp4, out_part02.mp4, out_part03.mp4 automatically
       (it only writes a single out.mp4 with no suffix if just one part fits)

If you don't want splitting -- just ONE clip like before -- add --single-clip.

SHORT VIDEOS (usable window smaller than --clip-seconds): by default this
now produces TWO EQUAL-LENGTH parts covering the whole usable window instead
of 0 parts. E.g. a 1:50 video with a 60s --clip-seconds request, whose usable
window (after intro/outro trim) is 48s, becomes two ~24s parts rather than
nothing. This only kicks in when a single full clip-length part wouldn't
fit at all. Control it with:

    --two-part-fallback / --no-two-part-fallback   (default: ON)
    --two-part-min-sec 10                          (minimum usable seconds
                                                       required before we'll
                                                       bother splitting into
                                                       two -- below this, the
                                                       leftover is considered
                                                       unusable and skipped)

--------------------------------------------------------------------------
WATERMARK / CREDIT-TEXT DETECTION (rewritten to be size-adaptive)
--------------------------------------------------------------------------

Real credit lines and logo bumpers vary a LOT in how much of the frame they
actually cover -- a short "HD video on X" line one time, a wide multi-word
credit block another time, at different opacities. A single fixed pixel/
threshold guess was either too tight (clipping letters) or too loose
(eating real picture) depending on the video. Detection now works in three
stages instead of one fixed pass:

  1. SIGNAL: for every sampled pixel, compute two per-pixel scores across
     all sampled frames --
       - "opaque" score: how often that pixel has an edge (Canny) AND how
         LOW its brightness variance is across samples (persistent, static
         logos/text sit still frame to frame).
       - "transparent overlay" score: how often that pixel has an edge,
         restricted to a border band around the frame -- catches
         semi-transparent overlays whose underlying pixel color still
         shifts with the video, so the opaque/low-variance test alone
         would miss them.
     The two are combined by taking the max per pixel, so one pass now
     catches both opaque logos AND semi-transparent text lines.

  2. ADAPTIVE CUTOFF: instead of a fixed magic threshold, Otsu's method
     picks the cutoff automatically FOR THIS VIDEO's actual signal
     strength, so faint/small overlays and bold/large ones both get a
     sensible cutoff instead of one hardcoded number that only worked for
     one contrast level.

  3. REGION GROWING: after finding a rough blob, the bounding box is grown
     outward on all 4 sides while pixel density just outside the box stays
     above a floor, and stops the moment it doesn't. This is the actual fix
     for "sometimes bigger, sometimes smaller" -- the box organically
     settles on the true extent of the credit/logo for THIS video instead
     of relying on one fixed cluster-gap guess. A runaway safety cap
     prevents it from ballooning into unrelated picture content.

If AUTO detection still isn't landing on your credit text, you have two more
reliable options:

  1. Check what it WOULD remove before committing to a whole batch:

        python video_auto_editor.py --input movie.mp4 --debug-preview

     This writes movie_wm_preview.png with a red box around whatever region
     will actually be delogo'd (detected OR manually specified), so you can
     verify alignment on one frame before rendering anything. Add
     --debug-detect to also print the scoring math to the terminal.

  2. Skip detection entirely and specify the exact region yourself, either
     in fixed pixels (same box on every video -- use this if all your
     videos share one resolution):

        --watermark-box 1200,880,700,180        # x,y,w,h in pixels

     ...or as PERCENTAGES of the frame (use this if videos have different
     resolutions but the credit sits in the same relative spot on all of
     them -- this is the more "dynamic" option for a mixed-resolution batch):

        --watermark-box-pct 0.62,0.80,0.35,0.12   # x,y,w,h as fractions 0-1

If your intros or outros are getting confused with content in the MIDDLE of
the video (e.g. on-screen text or a scene change partway through getting
mistaken for an intro/outro boundary), note that detection is hard-capped:
it only ever looks at the first 15s (intro) and last 15s (outro) of the
video by default. Raise --intro-max-search / --outro-max-search if your
real intro/outro genuinely runs longer than that.

--------------------------------------------------------------------------
OUTRO DETECTION (rewritten to catch fades, dissolves, and CTA overlays,
not just hard black-frame gaps)
--------------------------------------------------------------------------

Outros vary just as much as intros -- some cut hard to a black frame, some
slowly dim/dissolve into black over a few seconds, some cross-fade into a
mostly-static "subscribe" card or solid-color end card, and some are a
semi-transparent overlay that fades in on top of the still-playing video.
A single black-frame check missed all but the first case. Outro detection
now runs several checks over the trailing window (last 15s by default) and
takes the EARLIEST boundary any of them find, since that earliest point is
where the real content actually stops and the outro material begins:

  - black_frame_gap    : a run of near-black frames near the end (hard cut
                          to black, or credits over black).
  - color_bumper_outro  : the video ends on a frame dominated by one solid
                          hue (a flat-color end card / bumper), walked
                          backward to find where that color took over.
  - static_card_outro   : the last few frames are nearly motionless (a
                          still end-card or CTA overlay), walked backward
                          to find where the video stopped changing.
  - fade_to_black_outro : brightness ramps down toward black by the very
                          end even without ever fully triggering the
                          black-frame threshold -- a slow dissolve/offset
                          into darkness -- walked backward to where the dim
                          started.
  - scene_cut           : fallback -- the strongest hard scene change in
                          the window, if none of the above match.

On top of whichever boundary is found, a SAFETY MARGIN (default 2s) is
subtracted so the cut lands a couple of seconds BEFORE the detected outro
material, not right at its first frame -- this is what removes the last
bit of "real" content that's already mid-transition (fading, offsetting,
dimming) into the outro, instead of leaving a few frames of that dissolve
in the usable part. Tune it with:

    --outro-safety-margin 3     # strip 3s before the detected outro start
                                   (default 2, sensible range 1-5)

--------------------------------------------------------------------------
OUTPUT OPTIMIZATION (Bluesky-style, on by default)
--------------------------------------------------------------------------

Every rendered part is, by default, ALSO capped to a max dimension and a
target bitrate and flagged for fast-start playback -- the same three things
a separate "optimize for upload" pass would do -- so there is no second
re-encode step. Tune or disable with:

    --max-dim 640            # longest side in px (default 640)
    --video-bitrate 1.5M     # capped/target video bitrate (default 1.5M)
    --audio-bitrate 128k     # audio bitrate (default 128k)
    --encode-preset fast     # ffmpeg -preset (default fast; ultrafast is
                                quicker but produces larger files)
    --no-optimize            # disable the cap entirely; falls back to a
                                plain --crf quality encode at source size

--------------------------------------------------------------------------
SPEED (parallel rendering)
--------------------------------------------------------------------------

ffmpeg encodes are independent processes, so both axes of this pipeline can
run concurrently instead of one-clip-at-a-time:

    --part-workers 3   # render this many PARTS of the SAME video at once
    --workers 2         # batch mode: process this many VIDEOS at once

Each ffmpeg process itself uses multiple CPU threads for the encode, so
keep (workers * part-workers) modest relative to your core count -- e.g. on
an 8-core machine, --workers 2 --part-workers 2 is a reasonable start.

--------------------------------------------------------------------------

Requirements
------------
    pip install opencv-python numpy imagehash pillow --break-system-packages
    ffmpeg must be installed and on PATH.
"""

import argparse
import concurrent.futures
import glob
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

try:
    import cv2
except ImportError:
    sys.exit("Missing dependency: pip install opencv-python --break-system-packages")


# --------------------------------------------------------------------------- #
# Output optimization defaults (Bluesky-style: small, fast-loading files)
# --------------------------------------------------------------------------- #
# Longest side in pixels, target/max video bitrate, and audio bitrate used
# when --optimize is on (the default). These match the same numbers used by
# a standalone "optimize for Bluesky" pass, folded directly into the same
# ffmpeg invocation that does the intro/outro trim and delogo -- so there is
# no separate re-encode step and no quality loss from encoding twice.
MAX_DIM = 640
VIDEO_BITRATE = "1.5M"
AUDIO_BITRATE = "128k"


# --------------------------------------------------------------------------- #
# Utility
# --------------------------------------------------------------------------- #

def run(cmd: List[str]) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n\nSTDERR:\n{proc.stderr}")
    return proc


def ffprobe_duration(path: str) -> float:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path]
    out = run(cmd).stdout
    return float(json.loads(out)["format"]["duration"])


def ffprobe_dimensions(path: str) -> Tuple[int, int]:
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=width,height", "-of", "json", path]
    out = json.loads(run(cmd).stdout)
    s = out["streams"][0]
    return int(s["width"]), int(s["height"])


# --------------------------------------------------------------------------- #
# Frame sampling
# --------------------------------------------------------------------------- #

def _sample_frame_stats(path: str, scan_seconds: float, step_sec: float = 0.25,
                         start_offset: float = 0.0):
    """Sample (absolute_t, mean_brightness, hist) every step_sec within
    [start_offset, start_offset + scan_seconds]."""
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    step_frames = max(1, int(round(step_sec * fps)))
    start_frame = int(start_offset * fps)
    stats = []
    frame_idx = start_frame
    while True:
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ok, frame = cap.read()
        if not ok:
            break
        t = frame_idx / fps
        if t > start_offset + scan_seconds:
            break
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        mean_b = float(gray.mean())
        hist = cv2.calcHist([gray], [0], None, [32], [0, 256]).flatten()
        hist = hist / (hist.sum() + 1e-6)
        stats.append((t, mean_b, hist))
        frame_idx += step_frames
    cap.release()
    return stats


# --------------------------------------------------------------------------- #
# Intro / outro detection -- SINGLE VIDEO heuristic mode
# --------------------------------------------------------------------------- #

@dataclass
class BoundaryResult:
    time_sec: float          # intro: end-of-intro timestamp. outro: start-of-outro timestamp.
    method: str
    confidence: str          # "high" | "medium" | "low" | "none"


def _find_black_runs(stats, black_thresh=18.0, min_gap_frames=2):
    runs = []
    run_start = None
    for i, (t, mean_b, _h) in enumerate(stats):
        if mean_b < black_thresh:
            if run_start is None:
                run_start = i
        else:
            if run_start is not None and (i - run_start) >= min_gap_frames:
                runs.append((stats[run_start][0], stats[i - 1][0]))
            run_start = None
    if run_start is not None and (len(stats) - run_start) >= min_gap_frames:
        runs.append((stats[run_start][0], stats[-1][0]))
    return runs


def _strongest_cuts(stats):
    """Returns list of (t, score) sorted by time, for scene cuts."""
    cuts = []
    for i in range(1, len(stats)):
        h1, h2 = stats[i - 1][2], stats[i][2]
        score = float(cv2.compareHist(h1.astype("float32"), h2.astype("float32"), cv2.HISTCMP_BHATTACHARYYA))
        cuts.append((stats[i][0], score))
    return cuts


# Hard limit (seconds) for how far into the video the intro scan is allowed
# to look, and how far from the end the outro scan is allowed to look. This
# is deliberately strict: only the literal opening/closing 15s of the video
# are ever inspected, so a scene change, on-screen text, or dark moment in
# the MIDDLE of the video can never be mistaken for an intro/outro boundary.
DEFAULT_INTRO_MIN_SCAN_SEC = 15.0
DEFAULT_OUTRO_MIN_SCAN_SEC = 15.0

# How many seconds to strip BEFORE whatever boundary outro detection lands
# on. Outros frequently start with a fade/dissolve/offset transition that's
# already mid-flight by the time any single check (black frame, static
# card, color dominance...) actually trips -- the safety margin backs the
# cut up so that transition itself is trimmed too, not just the fully-
# formed outro material after it.
DEFAULT_OUTRO_SAFETY_MARGIN_SEC = 2.0


# --------------------------------------------------------------------------- #
# Solid-color / graphic "logo bumper" intro detection
# --------------------------------------------------------------------------- #
#
# Some intros aren't black screens and don't produce a strong histogram
# scene-cut either -- e.g. a production-logo splash screen (a big solid-color
# shape/oval covering most of the frame) that fades in and out gradually.
# Neither the black-screen check (frame isn't dark) nor the scene-cut check
# (transition is a soft fade, not a hard cut) catches this, so it was
# previously left in the output. This detects it directly: does the opening
# frame have one dominant hue covering a large fraction of the picture, and
# if so, when does that dominance drop off (i.e. the bumper ends)?

def _dominant_color_fraction(frame_bgr, sat_thresh: int = 60, hue_bin: int = 10) -> float:
    """Fraction of saturated pixels in `frame_bgr` that share the single most
    common hue bucket. High (e.g. > 0.25-0.3) means the frame is dominated by
    one solid color -- typical of a logo/graphic bumper, not real footage."""
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    h, s, _v = cv2.split(hsv)
    mask = s > sat_thresh
    total = mask.size
    if mask.sum() < 0.05 * total:
        return 0.0
    hues = h[mask]
    hist = np.bincount((hues // hue_bin).astype(np.int32), minlength=(180 // hue_bin) + 1)
    return float(hist.max()) / total


def detect_color_bumper_intro(path: str, window_end: float, start_offset: float = 0.0,
                               dominance_thresh: float = 0.25, drop_frac: float = 0.5,
                               step_sec: float = 0.25, debug: bool = False) -> Optional[BoundaryResult]:
    """Returns a BoundaryResult if the video is on a solid-color graphic
    bumper starting at `start_offset`, else None. `window_end` is the
    absolute hard cap (seconds) detection is not allowed to look past.
    `dominance_thresh` is how much of the frame at `start_offset` must be
    one solid hue to count as a bumper. `drop_frac` is how far that fraction
    must fall (relative to its opening value) before we call it "ended".
    `start_offset` lets this be called again after an earlier segment (e.g.
    a black flash) has already been trimmed, so a bumper that only starts
    AFTER that segment is still detected instead of being missed."""
    if window_end <= start_offset:
        return None
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    step_frames = max(1, int(round(step_sec * fps)))
    frame_idx = int(start_offset * fps)
    times, fracs = [], []
    while True:
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ok, frame = cap.read()
        if not ok:
            break
        t = frame_idx / fps
        if t > window_end:
            break
        fracs.append(_dominant_color_fraction(frame))
        times.append(t)
        frame_idx += step_frames
    cap.release()

    if debug:
        print(f"  [intro-detect] color-bumper scan @ cursor={start_offset:.2f}s: dominant-hue fraction "
              f"per sample = {[round(f, 3) for f in fracs]} (need first sample > {dominance_thresh})")

    if not fracs or fracs[0] < dominance_thresh:
        if debug:
            print("  [intro-detect] frame at this cursor is not dominated by one solid color -- not a bumper")
        return None

    opening = fracs[0]
    end_t = times[-1]
    for t, f in zip(times, fracs):
        if f < opening * drop_frac:
            end_t = t
            break
    if debug and end_t >= window_end - 0.5:
        print(f"  [intro-detect] color bumper still dominant at the {window_end:.2f}s scan limit -- "
              f"NOT extending further (hard cap); cutting here")
    if debug:
        print(f"  [intro-detect] -> color/logo bumper (from cursor {start_offset:.2f}s) ends at {end_t:.2f}s")
    return BoundaryResult(round(end_t, 2), "color_bumper_intro", "high")


# --------------------------------------------------------------------------- #
# Generic static title-card / logo bumper detection (any look, not just
# black or one solid color)
# --------------------------------------------------------------------------- #
#
# Some intros are detailed graphics -- a laurel wreath, a torch, textured
# lighting, particle sparkle -- that are neither a flat black screen nor
# dominated by one solid saturated color, so Cases 0 and 1 miss them. What
# they DO have in common with every logo-bumper style is that the opening is
# essentially STATIC: frame-to-frame it barely changes (maybe a little
# shimmer or a slow rotation) until it cuts or fades into real, moving
# footage. This checks for that directly: confirm the first few sampled
# frames are nearly identical to each other (so we know it's really a
# static card, not real motion from frame 0), then find the first point
# where the frame has drifted away from that opening reference. Comparing
# against the ORIGINAL reference frame (not just the previous sample) means
# this also catches slow fades/dissolves that a consecutive-frame scene-cut
# check would miss, since the cumulative drift still crosses the threshold
# even when no single step between adjacent samples looks like a hard cut.

def _hist_distance(h1, h2) -> float:
    return float(cv2.compareHist(h1.astype("float32"), h2.astype("float32"), cv2.HISTCMP_BHATTACHARYYA))


def _static_opening_boundary(stats, stable_frames: int = 3, stable_thresh: float = 0.15,
                              drift_thresh: float = 0.40, debug: bool = False) -> Optional[float]:
    if len(stats) < stable_frames + 2:
        return None
    pairwise = [_hist_distance(stats[i][2], stats[i + 1][2]) for i in range(stable_frames)]
    if debug:
        print(f"  [intro-detect] opening stability check, consecutive distances "
              f"(need ALL < {stable_thresh} to count as a static card): {[round(d, 3) for d in pairwise]}")
    if any(d > stable_thresh for d in pairwise):
        if debug:
            print("  [intro-detect] opening isn't static -- likely real footage already playing, skipping")
        return None

    ref_hist = stats[0][2]
    for t, _b, hist in stats[stable_frames:]:
        dist = _hist_distance(ref_hist, hist)
        if dist > drift_thresh:
            if debug:
                print(f"  [intro-detect] -> static opening drifts from its reference frame at "
                      f"{t:.2f}s (distance={dist:.3f} > {drift_thresh})")
            return t
    return None


def detect_intro_single(path: str, max_search_sec: float = DEFAULT_INTRO_MIN_SCAN_SEC,
                         debug: bool = False) -> BoundaryResult:
    duration = ffprobe_duration(path)
    # Always scan out to at least `max_search_sec` (default 15s) as long as
    # the video is actually that long; only fall back to a smaller window
    # for videos shorter than that, and leave a little buffer so we don't
    # run past the very end of a short clip.
    window = min(max_search_sec, max(0.0, duration - 1.0))
    stats = _sample_frame_stats(path, window, start_offset=0.0)
    if debug:
        print(f"  [intro-detect] scanning [0.00s, {window:.2f}s] ({len(stats)} samples)")
    if len(stats) < 4:
        if debug:
            print(f"  [intro-detect] too few samples ({len(stats)}) -- insufficient data")
        return BoundaryResult(0.0, "insufficient_data", "none")

    if debug:
        print(f"  [intro-detect] first sampled frame brightness={stats[0][1]:.1f} "
              f"(threshold for 'opens on black' is < 18.0)")

    # Detection CHAINS: a real intro is often several segments back-to-back
    # (e.g. a brief black flash, THEN a logo bumper, THEN real content
    # starts). Earlier versions returned as soon as the FIRST segment
    # (e.g. the black flash) was resolved, which cut only that flash and
    # left the logo bumper that followed it untouched. This instead keeps
    # advancing a cursor: at each position, try black-screen, then
    # color-bumper, then static-title-card in turn; whichever matches
    # extends the cursor to where THAT segment ends, and detection repeats
    # from there. It stops as soon as no case matches at the current cursor
    # (i.e. real, changing footage has started) or the hard cap is reached.
    cursor = 0.0
    chained_methods = []

    while cursor < window - 0.5:
        sub_stats = [s for s in stats if s[0] >= cursor - 1e-6]
        if len(sub_stats) < 4:
            break
        if debug:
            print(f"  [intro-detect] --- cursor at {cursor:.2f}s, brightness={sub_stats[0][1]:.1f} ---")

        advanced_to = None
        method_used = None

        # Black screen starting at this cursor.
        if sub_stats[0][1] < 18.0:
            black_runs = _find_black_runs(sub_stats)
            opening_run = black_runs[0] if black_runs and black_runs[0][0] < sub_stats[0][0] + 1.0 else None
            if opening_run is not None:
                run_end = opening_run[1]
                # Advance to the NEXT sampled frame after the black run, not
                # to the run's own end timestamp -- that timestamp is still
                # the LAST black/transitional frame, and evaluating a bumper
                # or static-card check starting on that frame (still partly
                # dark, still mid-fade) makes both checks fail even though a
                # real bumper follows immediately after it.
                next_samples = [s for s in sub_stats if s[0] > run_end + 1e-6]
                advanced_to = next_samples[0][0] if next_samples else run_end
                method_used = "black_screen"
                if debug:
                    print(f"  [intro-detect] black screen at cursor -> run ends {run_end:.2f}s, "
                          f"advancing cursor to next frame at {advanced_to:.2f}s")

        # Solid-color / logo bumper starting at this cursor.
        if advanced_to is None:
            bumper = detect_color_bumper_intro(path, window_end=window, start_offset=cursor, debug=debug)
            if bumper is not None:
                advanced_to = bumper.time_sec
                method_used = "color_bumper"

        # Static (near-motionless) title card starting at this cursor --
        # catches detailed graphics that are neither black nor one solid
        # color (e.g. a laurel wreath / torch logo), including slow fades.
        if advanced_to is None:
            static_end = _static_opening_boundary(sub_stats, debug=debug)
            if static_end is not None:
                advanced_to = static_end
                method_used = "static_card"

        if advanced_to is None or advanced_to <= cursor + 1e-6:
            break

        chained_methods.append(method_used)
        cursor = advanced_to

    if chained_methods:
        method_str = "+".join(dict.fromkeys(chained_methods))  # de-dup while keeping order
        if debug:
            print(f"  [intro-detect] -> chained detection [{method_str}], final intro end = {cursor:.2f}s")
        return BoundaryResult(round(cursor, 2), method_str, "high")

    # Nothing at cursor 0 matched black/bumper/static at all -- fall back to
    # the strongest plain scene cut in the window (e.g. a hard cut straight
    # into content with no distinct opening card).
    cuts = _strongest_cuts(stats)
    if debug and cuts:
        top5 = sorted(cuts, key=lambda x: -x[1])[:5]
        print(f"  [intro-detect] top scene-cut candidates (time, score, need >0.35): "
              f"{[(round(t, 2), round(s, 3)) for t, s in top5]}")
    if cuts:
        best_t, best_score = max(cuts, key=lambda x: x[1])
        if best_score > 0.35:
            if debug:
                print(f"  [intro-detect] -> using scene cut at {best_t:.2f}s (score={best_score:.3f})")
            return BoundaryResult(round(best_t, 2), "scene_cut", "medium")
        elif debug:
            print(f"  [intro-detect] strongest scene-cut score {best_score:.3f} is below the 0.35 "
                  f"threshold -- treating as no boundary found (intro end = 0.00s)")

    return BoundaryResult(0.0, "no_clear_boundary", "none")


# --------------------------------------------------------------------------- #
# Outro detection helpers -- mirror of the intro detectors above, but
# working from the END of the video backward, since outros end the same
# ways intros begin (black, solid-color bumper, static card) plus two
# outro-specific patterns: a gradual fade-to-black ramp, and the general
# "find the earliest onset" combining rule.
# --------------------------------------------------------------------------- #

def detect_color_bumper_outro(path: str, scan_start: float, scan_end: float,
                               dominance_thresh: float = 0.25, drop_frac: float = 0.5,
                               step_sec: float = 0.25, debug: bool = False) -> Optional[BoundaryResult]:
    """Mirror of detect_color_bumper_intro, but checks whether the video
    ENDS (at scan_end) on a solid-color card, and walks BACKWARD from there
    to find the earliest point that color card was already dominant --
    i.e. where the outro bumper actually started."""
    if scan_end <= scan_start:
        return None
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    step_frames = max(1, int(round(step_sec * fps)))
    frame_idx = int(scan_start * fps)
    times, fracs = [], []
    while True:
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ok, frame = cap.read()
        if not ok:
            break
        t = frame_idx / fps
        if t > scan_end:
            break
        fracs.append(_dominant_color_fraction(frame))
        times.append(t)
        frame_idx += step_frames
    cap.release()

    if debug:
        print(f"  [outro-detect] color-bumper scan [{scan_start:.2f}s, {scan_end:.2f}s]: dominant-hue "
              f"fraction per sample = {[round(f, 3) for f in fracs]} (need last sample > {dominance_thresh})")

    if not fracs or fracs[-1] < dominance_thresh:
        if debug:
            print("  [outro-detect] final frame isn't dominated by one solid color -- not a color-bumper outro")
        return None

    ending_frac = fracs[-1]
    start_t = times[-1]
    for t, f in zip(times, fracs):
        if f >= ending_frac * drop_frac:
            start_t = t
            break
    if debug:
        print(f"  [outro-detect] -> color/logo bumper outro starts at {start_t:.2f}s")
    return BoundaryResult(round(start_t, 2), "color_bumper_outro", "high")


def _static_ending_boundary(stats, stable_frames: int = 3, stable_thresh: float = 0.15,
                             drift_thresh: float = 0.40, debug: bool = False) -> Optional[float]:
    """Mirror of _static_opening_boundary, but anchored on the END of the
    window instead of the start: confirms the LAST few sampled frames are
    nearly identical (a still end-card / CTA overlay, not motion right up
    to EOF), then walks backward from there to find the earliest point that
    was already close to that final, static frame."""
    if len(stats) < stable_frames + 2:
        return None
    tail = stats[-stable_frames:]
    pairwise = [_hist_distance(tail[i][2], tail[i + 1][2]) for i in range(len(tail) - 1)]
    if debug:
        print(f"  [outro-detect] ending stability check, consecutive distances "
              f"(need ALL < {stable_thresh} to count as a static end-card): {[round(d, 3) for d in pairwise]}")
    if any(d > stable_thresh for d in pairwise):
        if debug:
            print("  [outro-detect] ending isn't static -- likely real footage right up to EOF, skipping")
        return None

    ref_hist = stats[-1][2]
    boundary_t = stats[max(0, len(stats) - stable_frames)][0]
    for i in range(len(stats) - stable_frames - 1, -1, -1):
        t, _b, hist = stats[i]
        dist = _hist_distance(ref_hist, hist)
        if dist > drift_thresh:
            if debug:
                print(f"  [outro-detect] -> static ending drifts from its reference frame going back to "
                      f"{stats[i + 1][0]:.2f}s (distance={dist:.3f} > {drift_thresh})")
            return stats[i + 1][0]
        boundary_t = t
    return boundary_t


def _fade_to_black_boundary(stats, end_thresh: float = 30.0, drop_ratio: float = 0.6,
                             debug: bool = False) -> Optional[float]:
    """Catches a gradual dim/dissolve/offset into black that never produces
    a long enough run of frames below the hard black-frame threshold to be
    caught by _find_black_runs (e.g. it only just reaches black in the very
    last sample, or briefly flickers back up mid-fade). Requires the LAST
    sampled frame to be reasonably dark, then walks forward from the peak
    brightness in the window to find the earliest sample that has already
    dropped to `drop_ratio` of that peak -- i.e. where the dimming began."""
    if len(stats) < 4:
        return None
    final_b = stats[-1][1]
    if final_b > end_thresh:
        if debug:
            print(f"  [outro-detect] fade-to-black check: final brightness {final_b:.1f} is above "
                  f"the near-black threshold ({end_thresh}) -- video doesn't end dark, skipping")
        return None

    peak_b = max(b for _t, b, _h in stats)
    if peak_b <= 0:
        return None
    target = peak_b * (1.0 - drop_ratio)

    for t, b, _h in stats:
        if b <= target:
            if debug:
                print(f"  [outro-detect] -> brightness fade starts at {t:.2f}s "
                      f"(peak={peak_b:.1f}, dropped to {b:.1f} <= target {target:.1f})")
            return t
    return None


DEFAULT_OUTRO_MAX_SEARCH_SEC = DEFAULT_OUTRO_MIN_SCAN_SEC


def detect_outro_single(path: str, max_search_sec: float = DEFAULT_OUTRO_MAX_SEARCH_SEC,
                         safety_margin: float = DEFAULT_OUTRO_SAFETY_MARGIN_SEC,
                         debug: bool = False) -> BoundaryResult:
    """Scans the LAST portion of the video for the EARLIEST boundary any of
    several outro patterns find -- a hard black-frame gap, a solid-color end
    card, a static/near-motionless end card or CTA overlay, or a gradual
    brightness fade toward black -- since outros commonly chain several of
    these (e.g. content dims, THEN a static end card appears, THEN it cuts
    to black at true EOF), and the earliest one marks where real content
    actually stopped.

    A `safety_margin` (seconds) is then subtracted from whichever boundary
    wins, so the cut lands a little BEFORE the first detected sign of the
    outro -- this is what removes the tail end of real content that's
    already mid-transition (fading, offsetting, dimming) into the outro
    instead of leaving a few of those transition frames in the usable part.
    """
    duration = ffprobe_duration(path)
    window = min(max_search_sec, duration * 0.4)
    start_offset = max(0.0, duration - window)
    stats = _sample_frame_stats(path, window, start_offset=start_offset)
    if debug:
        print(f"  [outro-detect] scanning [{start_offset:.2f}s, {duration:.2f}s] ({len(stats)} samples)")
    if len(stats) < 4:
        if debug:
            print(f"  [outro-detect] too few samples ({len(stats)}) -- insufficient data")
        return BoundaryResult(duration, "insufficient_data", "none")

    candidates: List[BoundaryResult] = []

    # 1. Hard black-frame gap near the end.
    black_runs = _find_black_runs(stats)
    if debug:
        print(f"  [outro-detect] black runs found in window: "
              f"{[(round(s, 2), round(e, 2)) for s, e in black_runs]}")
    candidate_gaps = [g for g in black_runs if g[0] < duration - 0.5]
    if candidate_gaps:
        candidates.append(BoundaryResult(round(candidate_gaps[0][0], 2), "black_frame_gap", "high"))

    # 2. Solid-color end card / bumper the video finishes on.
    bumper = detect_color_bumper_outro(path, start_offset, duration, debug=debug)
    if bumper is not None:
        candidates.append(bumper)

    # 3. Static / near-motionless end card or CTA overlay the video
    #    finishes on (catches detailed graphics and semi-transparent
    #    overlays that neither of the above two checks would flag).
    static_t = _static_ending_boundary(stats, debug=debug)
    if static_t is not None:
        candidates.append(BoundaryResult(round(static_t, 2), "static_card_outro", "high"))

    # 4. Gradual brightness fade/dissolve/offset toward black that ends the
    #    video, even when it never forms a long enough run of hard-black
    #    frames to trip check #1.
    fade_t = _fade_to_black_boundary(stats, debug=debug)
    if fade_t is not None:
        candidates.append(BoundaryResult(round(fade_t, 2), "fade_to_black_outro", "high"))

    if candidates:
        best = min(candidates, key=lambda c: c.time_sec)
        if debug:
            all_str = ", ".join(f"{c.method}={c.time_sec:.2f}s" for c in candidates)
            print(f"  [outro-detect] candidates found: [{all_str}] -> earliest is {best.method} "
                  f"@ {best.time_sec:.2f}s")
        margin_applied = min(safety_margin, max(0.0, best.time_sec - start_offset))
        final_t = max(start_offset, best.time_sec - margin_applied)
        if debug and margin_applied > 0:
            print(f"  [outro-detect] applying {margin_applied:.2f}s safety margin -> cutting at "
                  f"{final_t:.2f}s instead of {best.time_sec:.2f}s")
        method = best.method if margin_applied <= 0 else f"{best.method}_margin{margin_applied:.1f}s"
        return BoundaryResult(round(final_t, 2), method, best.confidence)

    # 5. Fallback -- the strongest hard scene change anywhere in the window.
    cuts = _strongest_cuts(stats)
    strong_cuts = [c for c in cuts if c[1] > 0.35]
    if debug and cuts:
        top5 = sorted(cuts, key=lambda x: -x[1])[:5]
        print(f"  [outro-detect] top scene-cut candidates (time, score, need >0.35): "
              f"{[(round(t, 2), round(s, 3)) for t, s in top5]}")
    if strong_cuts:
        earliest_t, _score = min(strong_cuts, key=lambda x: x[0])
        margin_applied = min(safety_margin, max(0.0, earliest_t - start_offset))
        final_t = max(start_offset, earliest_t - margin_applied)
        if debug:
            print(f"  [outro-detect] -> using earliest strong scene cut at {earliest_t:.2f}s "
                  f"(margin {margin_applied:.2f}s -> cutting at {final_t:.2f}s)")
        method = "scene_cut" if margin_applied <= 0 else f"scene_cut_margin{margin_applied:.1f}s"
        return BoundaryResult(round(final_t, 2), method, "medium")

    if debug:
        print("  [outro-detect] no black run, color/static end-card, brightness fade, or strong scene "
              "cut found -- no boundary (outro start = end of video, i.e. nothing trimmed)")
    return BoundaryResult(duration, "no_clear_boundary", "none")


# --------------------------------------------------------------------------- #
# Intro/outro detection -- BATCH mode (shared across many videos)
# --------------------------------------------------------------------------- #

def _phash_sequence(path: str, scan_seconds: float, start_offset: float = 0.0,
                     fps_sample: float = 1.0) -> List[int]:
    import imagehash
    from PIL import Image

    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    step_frames = max(1, int(round(fps / fps_sample)))
    start_frame = int(start_offset * fps)
    hashes = []
    frame_idx = start_frame
    while True:
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ok, frame = cap.read()
        if not ok:
            break
        t = frame_idx / fps
        if t > start_offset + scan_seconds:
            break
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)
        h = imagehash.phash(img)
        hashes.append(int(str(h), 16))
        frame_idx += step_frames
    cap.release()
    return hashes


def detect_intro_batch(paths: List[str], scan_seconds: float = DEFAULT_INTRO_MIN_SCAN_SEC,
                        hamming_thresh: int = 8) -> BoundaryResult:
    sequences = [_phash_sequence(p, scan_seconds, start_offset=0.0) for p in paths]
    min_len = min(len(s) for s in sequences)
    if min_len == 0:
        return BoundaryResult(0.0, "batch_no_frames", "none")

    matched = 0
    for i in range(min_len):
        ref = sequences[0][i]
        ok = all(bin(ref ^ seq[i]).count("1") <= hamming_thresh for seq in sequences[1:])
        if ok:
            matched = i + 1
        else:
            break
    confidence = "high" if matched >= 2 else "none"
    return BoundaryResult(float(matched), "batch_common_prefix", confidence)


def detect_outro_batch(paths: List[str], scan_seconds: float = 30.0,
                        hamming_thresh: int = 8,
                        safety_margin: float = DEFAULT_OUTRO_SAFETY_MARGIN_SEC) -> Optional[float]:
    """Returns the shared outro LENGTH (seconds, counted from each video's own
    end backward) if all videos share a common trailing sequence, else None.
    Because each video may have a different total duration, this compares
    each video's own final `scan_seconds` against every other video's own
    final `scan_seconds`, aligned from the end. The safety margin is added
    on top of the detected common length, so the shared cut also backs up
    a couple of seconds before the outro material actually starts."""
    durations = [ffprobe_duration(p) for p in paths]
    seqs = []
    for p, d in zip(paths, durations):
        window = min(scan_seconds, d * 0.4)
        offset = max(0.0, d - window)
        seqs.append(_phash_sequence(p, window, start_offset=offset))
    min_len = min(len(s) for s in seqs)
    if min_len == 0:
        return None

    # align from the END of each sequence backward
    matched = 0
    for i in range(1, min_len + 1):
        ref = seqs[0][-i]
        ok = all(bin(ref ^ seq[-i]).count("1") <= hamming_thresh for seq in seqs[1:])
        if ok:
            matched = i
        else:
            break
    if matched < 2:
        return None
    return float(matched) + max(0.0, safety_margin)


# --------------------------------------------------------------------------- #
# Watermark / logo / credit-text detection
# --------------------------------------------------------------------------- #

@dataclass
class WatermarkBox:
    x: int
    y: int
    w: int
    h: int

    def clamped(self, frame_w: int, frame_h: int, pad: int = 4, margin: int = 4) -> "WatermarkBox":
        """Returns a padded copy of this box guaranteed to fit inside the frame
        WITH a safety margin on every side. ffmpeg's delogo filter samples
        pixels just outside the box to interpolate the fill, so a box that
        merely touches the frame edge (x=0, or y+h==frame_h, etc.) still gets
        rejected with 'Logo area is outside of the frame' -- it needs genuine
        breathing room, not just to stay in-bounds.

        If the box (e.g. one detected/specified for a DIFFERENT resolution)
        doesn't overlap the frame at all, or the overlap is too thin to be a
        sane delogo target, this returns WatermarkBox(0, 0, 0, 0) -- an
        explicitly INVALID box -- rather than silently forcing a minimum
        2x2px box at an out-of-bounds position. A forced-minimum box was the
        earlier bug: it always reported w>0, h>0 to callers even when x/y
        were still off-frame, so it slipped past the render_clip() 'is this
        box usable' check and reached ffmpeg, which then hard-crashed with
        'Logo area is outside of the frame'. Callers must check w>0 and h>0
        on the result before using it."""
        x_min, y_min = margin, margin
        x_max, y_max = frame_w - margin, frame_h - margin
        if x_max <= x_min or y_max <= y_min:
            return WatermarkBox(0, 0, 0, 0)  # frame itself too small for any margin

        x1 = max(self.x - pad, x_min)
        y1 = max(self.y - pad, y_min)
        x2 = min(self.x + self.w + pad, x_max)
        y2 = min(self.y + self.h + pad, y_max)

        w = x2 - x1
        h = y2 - y1
        # Box doesn't actually overlap the valid frame region at all (e.g.
        # it was detected/specified for a wider/taller source resolution) --
        # report invalid instead of clamping into a meaningless sliver.
        if w < 4 or h < 4:
            return WatermarkBox(0, 0, 0, 0)

        w = max(2, w - (w % 2))
        h = max(2, h - (h % 2))
        return WatermarkBox(x1, y1, w, h)

    def to_fractions(self, frame_w: int, frame_h: int) -> Tuple[float, float, float, float]:
        """Expresses this box as (x, y, w, h) fractions (0-1) of frame_w x
        frame_h. Used so a watermark box detected on ONE video (e.g. the
        first video in a --shared-watermark-from-first batch) can be
        rescaled correctly onto other videos that don't share its exact
        resolution, instead of reusing raw pixel coordinates that may fall
        entirely outside a smaller frame."""
        if frame_w <= 0 or frame_h <= 0:
            return (0.0, 0.0, 0.0, 0.0)
        return (self.x / frame_w, self.y / frame_h, self.w / frame_w, self.h / frame_h)

    def as_ffmpeg_delogo(self) -> str:
        return f"delogo=x={self.x}:y={self.y}:w={self.w}:h={self.h}:show=0"


def watermark_box_from_fractions(xp: float, yp: float, wp: float, hp: float,
                                  frame_w: int, frame_h: int) -> WatermarkBox:
    """Scales (x, y, w, h) fractions (0-1) to pixel coordinates for a
    specific frame_w x frame_h. This is the shared core used both by
    --watermark-box-pct (user-specified fractions) and by
    --shared-watermark-from-first (fractions derived from whatever the
    detector found on the first video), so a box defined relative to one
    resolution scales correctly onto a differently-sized video instead of
    being reused as fixed, now-wrong pixel coordinates."""
    x = round(xp * frame_w)
    y = round(yp * frame_h)
    w = round(wp * frame_w)
    h = round(hp * frame_h)
    return WatermarkBox(x, y, w, h)


def _boxes_overlap_or_close(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int],
                             gap: int) -> bool:
    """True if rects a, b (x,y,w,h) overlap, or are within `gap` px of each
    other -- used to merge separate letter/word blobs of the same text line."""
    ax1, ay1, aw, ah = a
    ax2, ay2 = ax1 + aw, ay1 + ah
    bx1, by1, bw, bh = b
    bx2, by2 = bx1 + bw, by1 + bh
    return not (ax1 - gap > bx2 or bx1 - gap > ax2 or ay1 - gap > by2 or by1 - gap > ay2)


def _cluster_boxes(boxes: List[Tuple[int, int, int, int]], gap: int) -> List[Tuple[int, int, int, int]]:
    """Union-merge boxes that overlap or sit within `gap` px of each other.
    This is what lets multi-letter / multi-word credit text (which shows up
    as several separate contours) collapse into ONE bounding box covering
    the whole line, instead of only the single largest letter."""
    clusters = [list(b) for b in boxes]
    merged = True
    while merged:
        merged = False
        out = []
        used = [False] * len(clusters)
        for i in range(len(clusters)):
            if used[i]:
                continue
            cur = clusters[i]
            for j in range(i + 1, len(clusters)):
                if used[j]:
                    continue
                if _boxes_overlap_or_close(tuple(cur), tuple(clusters[j]), gap):
                    x1 = min(cur[0], clusters[j][0])
                    y1 = min(cur[1], clusters[j][1])
                    x2 = max(cur[0] + cur[2], clusters[j][0] + clusters[j][2])
                    y2 = max(cur[1] + cur[3], clusters[j][1] + clusters[j][3])
                    cur = [x1, y1, x2 - x1, y2 - y1]
                    used[j] = True
                    merged = True
            out.append(cur)
            used[i] = True
        clusters = out
    return [tuple(c) for c in clusters]


def _otsu_threshold(arr: np.ndarray, bins: int = 256) -> float:
    """Otsu's method on a float array assumed to be roughly in [0, 1].
    Returns the cutoff that best separates the array into two classes by
    maximizing between-class variance. This is what lets the watermark
    detector adapt its cutoff per-video instead of using one fixed number
    that only suits one contrast level."""
    scaled = np.clip(arr, 0.0, 1.0)
    hist, edges = np.histogram(scaled, bins=bins, range=(0.0, 1.0))
    hist = hist.astype(np.float64)
    total = hist.sum()
    if total == 0:
        return 0.5
    centers = (edges[:-1] + edges[1:]) / 2.0
    sum_all = float(np.dot(hist, centers))
    sum_b = 0.0
    w_b = 0.0
    best_var = -1.0
    best_thresh = float(centers[0])
    for i in range(bins):
        w_b += hist[i]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += centers[i] * hist[i]
        m_b = sum_b / w_b
        m_f = (sum_all - sum_b) / w_f
        var_between = w_b * w_f * (m_b - m_f) ** 2
        if var_between > best_var:
            best_var = var_between
            best_thresh = float(centers[i])
    return best_thresh


def _expand_box_by_density(mask01: np.ndarray, x: int, y: int, w: int, h: int,
                            min_density: float = 0.18, step: int = 2,
                            max_frame_frac: float = 0.22, debug: bool = False
                            ) -> Tuple[int, int, int, int]:
    """Grows (x, y, w, h) outward on all four sides while the strip of pixels
    just outside the current edge still has mask density >= min_density,
    stopping the moment it doesn't. This is what makes the final box track
    the ACTUAL size of the credit/logo in this particular video -- small
    text stops growing almost immediately, a wider/taller credit block keeps
    growing until it genuinely runs out of signal -- instead of relying on
    one fixed guessed size. A frame-fraction cap prevents runaway growth
    into unrelated picture content if the mask is unexpectedly noisy."""
    H, W = mask01.shape
    frame_area = H * W
    orig = (x, y, w, h)

    def row_density(yy: int) -> float:
        yy0, yy1 = max(0, yy), min(H, yy + step)
        xx0, xx1 = max(0, x), min(W, x + w)
        if yy1 <= yy0 or xx1 <= xx0:
            return 0.0
        return float(mask01[yy0:yy1, xx0:xx1].mean())

    def col_density(xx: int) -> float:
        xx0, xx1 = max(0, xx), min(W, xx + step)
        yy0, yy1 = max(0, y), min(H, y + h)
        if xx1 <= xx0 or yy1 <= yy0:
            return 0.0
        return float(mask01[yy0:yy1, xx0:xx1].mean())

    for _ in range(400):
        if y <= 0 or (w * h) / frame_area > max_frame_frac:
            break
        if row_density(y - step) < min_density:
            break
        step_up = min(step, y)
        y -= step_up
        h += step_up

    for _ in range(400):
        if y + h >= H or (w * h) / frame_area > max_frame_frac:
            break
        if row_density(y + h) < min_density:
            break
        step_down = min(step, H - (y + h))
        h += step_down

    for _ in range(400):
        if x <= 0 or (w * h) / frame_area > max_frame_frac:
            break
        if col_density(x - step) < min_density:
            break
        step_left = min(step, x)
        x -= step_left
        w += step_left

    for _ in range(400):
        if x + w >= W or (w * h) / frame_area > max_frame_frac:
            break
        if col_density(x + w) < min_density:
            break
        step_right = min(step, W - (x + w))
        w += step_right

    if (w * h) / frame_area > max_frame_frac:
        if debug:
            print(f"  [watermark-detect] expansion ran away past {max_frame_frac:.0%} of frame -- "
                  f"reverting to pre-expansion box {orig}")
        return orig

    if debug and (x, y, w, h) != orig:
        print(f"  [watermark-detect] region-grow expanded box {orig} -> {(x, y, w, h)}")

    return x, y, w, h


def _is_bar_shape(x: int, y: int, w: int, h: int, frame_w: int, frame_h: int,
                   bar_span_frac: float = 0.75, bar_thin_frac: float = 0.08) -> bool:
    """True if (x,y,w,h) looks like a letterbox edge, progress bar, or other
    full-frame-spanning UI bar rather than a logo/credit line -- i.e. it
    stretches across almost the entire width (or height) of the frame while
    staying very thin in the other dimension. Real watermarks/credit text
    are compact in BOTH dimensions, so this is a cheap, safe way to drop the
    false positives that used to win purely on edge-density/persistence."""
    spans_width = w >= bar_span_frac * frame_w
    spans_height = h >= bar_span_frac * frame_h
    thin_height = h <= bar_thin_frac * frame_h
    thin_width = w <= bar_thin_frac * frame_w
    return (spans_width and thin_height) or (spans_height and thin_width)


def _split_oversized_box(mask01: np.ndarray, box: Tuple[int, int, int, int],
                          frame_w: int, frame_h: int,
                          max_w_frac: float = 0.55, max_h_frac: float = 0.55
                          ) -> List[Tuple[int, int, int, int]]:
    """If a candidate box got bridged (via dilation) into spanning most of the
    frame's width or height, re-segment ONLY that region on the raw
    (un-dilated) mask with plain connected-components -- no bridging -- so a
    small corner logo that got glued to unrelated far-away edge noise is
    split back apart into its real, separate pieces instead of being kept
    as one giant band."""
    x, y, w, h = box
    if w < max_w_frac * frame_w and h < max_h_frac * frame_h:
        return [box]
    sub = mask01[y:y + h, x:x + w]
    n, _labels, stats, _cent = cv2.connectedComponentsWithStats(sub.astype(np.uint8), connectivity=8)
    pieces = []
    for i in range(1, n):
        sx, sy, sw, sh, area = stats[i]
        if area < 6:
            continue
        pieces.append((x + int(sx), y + int(sy), int(sw), int(sh)))
    return pieces if pieces else [box]


def _corner_proximity_score(x: int, y: int, w: int, h: int, frame_w: int, frame_h: int) -> float:
    """1.0 at a frame corner, falling off to 0 well before mid-frame. Credit
    lines and logos are placed in a CORNER, not just 'near some edge' -- a
    box centered on an edge midpoint (e.g. a top-center title bar) should
    score much lower than one actually tucked into a corner, which the old
    nearest-edge-distance metric didn't distinguish."""
    cx, cy = x + w / 2.0, y + h / 2.0
    corners = [(0, 0), (frame_w, 0), (0, frame_h), (frame_w, frame_h)]
    dist = min(((cx - ccx) ** 2 + (cy - ccy) ** 2) ** 0.5 for ccx, ccy in corners)
    diag = (frame_w ** 2 + frame_h ** 2) ** 0.5
    return max(0.0, 1.0 - dist / (diag * 0.35))


def _regions_overlap_or_close_dynamic(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int],
                                       min_gap: int = 18, gap_frac: float = 0.6) -> bool:
    """Like _boxes_overlap_or_close, but the allowed gap scales with the
    SMALLER of the two boxes' own size (capped by min_gap as a floor). This
    is what lets a stacked icon+text pair merge into one removal region
    without letting a large text block reach far across the frame and pull
    in something small and unrelated -- the gap a box is allowed to bridge
    is bounded by whichever of the pair is more local in scale."""
    a_scale = max(a[2], a[3])
    b_scale = max(b[2], b[3])
    gap = max(min_gap, int(round(gap_frac * min(a_scale, b_scale))))
    return _boxes_overlap_or_close(a, b, gap)


def _merge_nearby_regions(boxes: List[Tuple[int, int, int, int]]) -> List[Tuple[int, int, int, int]]:
    """Chain-merges boxes using the dynamic, scale-bounded gap above. Distinct
    from _cluster_boxes (fixed gap, used earlier for raw letter/word
    bridging): this pass runs on already-filtered, already-legitimate
    watermark-piece candidates, so it's safe to let it reach a bit further
    to reunite something like a small logo icon with the credit-text line
    it belongs to."""
    clusters = [list(b) for b in boxes]
    merged = True
    while merged:
        merged = False
        out = []
        used = [False] * len(clusters)
        for i in range(len(clusters)):
            if used[i]:
                continue
            cur = clusters[i]
            for j in range(i + 1, len(clusters)):
                if used[j]:
                    continue
                if _regions_overlap_or_close_dynamic(tuple(cur), tuple(clusters[j])):
                    x1 = min(cur[0], clusters[j][0])
                    y1 = min(cur[1], clusters[j][1])
                    x2 = max(cur[0] + cur[2], clusters[j][0] + clusters[j][2])
                    y2 = max(cur[1] + cur[3], clusters[j][1] + clusters[j][3])
                    cur = [x1, y1, x2 - x1, y2 - y1]
                    used[j] = True
                    merged = True
            out.append(cur)
            used[i] = True
        clusters = out
    return [tuple(c) for c in clusters]


def detect_watermark_auto(path: str, start_sec: float, end_sec: float, sample_count: int = 48,
                           min_area_frac: float = 0.0004, max_area_frac: float = 0.45,
                           density_floor: float = 0.06, max_regions: int = 3,
                           border_frac: float = 0.32, debug: bool = False
                           ) -> List[WatermarkBox]:
    """Finds every static logo, credit-text line, or larger burned-in caption
    block in the frame and returns a bounding box for EACH -- sized to
    whatever it actually is (a small corner mark or a large multi-line
    caption) rather than a fixed guess, and without assuming there's only
    one watermark element on screen.

    Pipeline:
      1. Sample frames across [start_sec, end_sec] and compute, per pixel:
         - edge_freq: how often Canny finds an edge there.
         - var_norm : normalized brightness variance across samples.
      2. Combine two scores per pixel and take the max:
         - "opaque"      = edge_freq * (1 - var_norm)   (persistent + static)
         - "transparent" = edge_freq, restricted to a border band around the
           frame (catches alpha-blended overlays whose underlying color
           still shifts with the video, so variance alone can't flag them).
      3. Threshold the combined map with Otsu's method (adaptive per video).
      4. Cluster nearby blobs with a SMALL merge gap (bridges gaps between
         letters/words of the same credit line without also bridging
         unrelated far-away edge noise into the same box). Any blob that
         still ends up spanning most of the frame is re-split on the raw
         mask with plain connected components, and anything letterbox- or
         progress-bar-shaped (spans almost the full width or height while
         staying very thin) is dropped outright.
      5. Each survivor must also clear a DENSITY FLOOR (the fraction of
         "on" pixels inside its own bounding box) -- this is what lets the
         area cap be generous (real captions/watermark blocks can be a
         large, legitimate chunk of the frame) without that generosity
         being exploited by a big, sparse, accidentally-bridged region:
         genuine text/graphics are locally dense wherever they sit, sparse
         bridging noise is not.
      6. Merge nearby survivors (e.g. a logo icon stacked beside its own
         credit-text line) using a gap bounded by the smaller piece's own
         scale, so this can reunite one watermark's parts without reaching
         across the frame to grab something unrelated.
      7. Region-grow each surviving region outward until density drops off,
         with a cap scaled to how big it already is (a tiny mark stays
         tightly capped; a block that's already large is allowed to keep
         growing further) and a hard final ceiling either way; if growth
         somehow still runs away, that region reverts to its pre-growth
         box rather than shipping something oversized.
      8. Keep at most `max_regions` regions, ranked by density + corner/edge
         proximity + size, so a video with one tiny corner logo still gets
         exactly one region while one with both a caption block AND a
         separate logo gets both.

    Callers should pass `start_sec`/`end_sec` as the USABLE window (i.e.
    already past the intro and before the outro) so a black-screen intro
    never gets sampled here.
    """
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    start_frame = int(start_sec * fps)
    end_frame = min(int(end_sec * fps), total_frames - 1)
    end_frame = max(end_frame, start_frame + 1)

    idxs = np.linspace(start_frame, end_frame, num=sample_count, dtype=int)

    edge_accum = None
    frames_gray = []
    for idx in idxs:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
        ok, frame = cap.read()
        if not ok:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        frames_gray.append(gray)
        edges = cv2.Canny(gray, 60, 160)
        edge_accum = edges.astype(np.float32) if edge_accum is None else edge_accum + edges.astype(np.float32)
    cap.release()

    if not frames_gray or edge_accum is None:
        return []

    n = len(frames_gray)
    h_frame, w_frame = frames_gray[0].shape
    frame_area = h_frame * w_frame

    edge_freq = edge_accum / (255.0 * n)

    stack = np.stack(frames_gray, axis=0).astype(np.float32)
    variance = stack.var(axis=0)
    var_norm = variance / (variance.max() + 1e-6)

    # --- combined per-pixel signal: opaque-static OR transparent-overlay ---
    opaque_score = edge_freq * (1.0 - var_norm)

    border_px = border_frac * min(w_frame, h_frame)
    yy, xx = np.mgrid[0:h_frame, 0:w_frame]
    border_mask = ((xx < border_px) | (yy < border_px) |
                    (w_frame - xx <= border_px) | (h_frame - yy <= border_px))
    transparent_score = edge_freq * border_mask

    combined = np.maximum(opaque_score, transparent_score * 0.85)
    peak = float(combined.max())

    if debug:
        print(f"  [watermark-detect] sampled {n} frames over [{start_sec:.1f}s, {end_sec:.1f}s], "
              f"resolution={w_frame}x{h_frame}")
        print(f"  [watermark-detect] combined signal: peak={peak:.3f} mean={combined.mean():.4f}")

    if peak < 0.12:
        if debug:
            print("  [watermark-detect] peak combined signal below floor (0.12) -- no watermark detected")
        return []

    thresh = _otsu_threshold(combined)
    thresh = float(np.clip(thresh, 0.10, peak * 0.65))
    if debug:
        print(f"  [watermark-detect] adaptive (Otsu) threshold = {thresh:.3f}")

    raw_mask01 = (combined >= thresh).astype(np.uint8)

    # Morphology kernel scaled to resolution so it bridges inter-letter /
    # inter-word gaps consistently regardless of the video's actual size.
    # Kept intentionally SMALL and applied only once: a wide/aggressive
    # kernel (and a second CLOSE pass on top of it) is what used to bridge
    # a small corner logo together with unrelated edge noise clear across
    # the frame, producing one giant false "candidate".
    kx = max(9, int(round(w_frame / 130.0)))
    ky = max(3, int(round(h_frame / 220.0)))
    kx += (kx % 2 == 0)
    ky += (ky % 2 == 0)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kx, ky))
    dilated = cv2.dilate(raw_mask01 * 255, kernel, iterations=1)

    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        if debug:
            print("  [watermark-detect] no contours survived thresholding/morphology")
        return []

    stable_boxes = []   # already-atomic pieces recovered from a split -- never re-merged
    mergeable_boxes = []  # small, un-split contours -- may still need letter/word bridging
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        was_oversized = (w >= 0.55 * w_frame) or (h >= 0.55 * h_frame)
        pieces = _split_oversized_box(raw_mask01, (x, y, w, h), w_frame, h_frame)
        for (sx, sy, sw, sh) in pieces:
            area_frac = (sw * sh) / frame_area
            if not (min_area_frac <= area_frac <= max_area_frac):
                continue
            if was_oversized:
                stable_boxes.append((sx, sy, sw, sh))
            else:
                mergeable_boxes.append((sx, sy, sw, sh))

    if debug:
        print(f"  [watermark-detect] {len(contours)} raw contours -> {len(stable_boxes)} stable (split, "
              f"not re-merged) + {len(mergeable_boxes)} mergeable, within area range "
              f"[{min_area_frac:.4f}, {max_area_frac:.2f}]")

    if not stable_boxes and not mergeable_boxes:
        return []

    merge_gap = max(12, int(round(min(w_frame, h_frame) / 45.0)))
    # Only the small, never-split contours go through gap-based merging --
    # that's what legitimately bridges separate letters/words of one credit
    # line. Pieces already recovered from splitting a bridged blob are kept
    # exactly as they came out: re-running them through the same transitive
    # gap-merge is what glued a real corner logo back into a bogus
    # full-width band in the first place.
    clustered = stable_boxes + _cluster_boxes(mergeable_boxes, gap=merge_gap)

    # Drop anything that's a letterbox edge / progress-bar shape (spans
    # almost the whole width or height while staying very thin) -- those
    # aren't logos or credit text, whatever their edge density looks like.
    bar_rejected = [c for c in clustered if _is_bar_shape(*c, w_frame, h_frame)]
    survivors = [c for c in clustered if not _is_bar_shape(*c, w_frame, h_frame)]
    if debug:
        print(f"  [watermark-detect] {len(clustered)} clusters after merging (gap={merge_gap}px); "
              f"{len(bar_rejected)} rejected as bar/letterbox-shaped: {bar_rejected}")

    # Density floor: this is what makes the generous area cap above safe.
    # A big cap alone would let sparse, accidentally-bridged regions through
    # just because they're not bar-shaped; requiring real internal density
    # keeps only regions that are actually coherent text/graphics throughout
    # their own bounding box, however large that box is.
    dense_enough = []
    density_rejected = []
    for (x, y, w, h) in survivors:
        density = float(raw_mask01[y:y + h, x:x + w].mean())
        if density >= density_floor:
            dense_enough.append((x, y, w, h))
        else:
            density_rejected.append(((x, y, w, h), round(density, 3)))
    if debug:
        print(f"  [watermark-detect] {len(dense_enough)} pass density floor ({density_floor}); "
              f"{len(density_rejected)} rejected as too sparse: {density_rejected}")

    if not dense_enough:
        if debug:
            print("  [watermark-detect] nothing survived bar-shape + density filtering -- "
                  "no logo/credit-text/caption found")
        return []

    # Reunite parts of the same watermark that clustering's small fixed gap
    # didn't bridge -- e.g. a logo icon stacked beside its own credit line --
    # using a gap bounded by the smaller piece's own scale so this can't
    # reach across the frame and glue unrelated regions together.
    merged_regions = _merge_nearby_regions(dense_enough)
    merged_regions = [r for r in merged_regions if not _is_bar_shape(*r, w_frame, h_frame)]
    if debug and len(merged_regions) != len(dense_enough):
        print(f"  [watermark-detect] merged nearby pieces into {len(merged_regions)} region(s): {merged_regions}")

    if not merged_regions:
        return []

    scored = []
    for (x, y, w, h) in merged_regions:
        area_frac = (w * h) / frame_area
        corner_score = _corner_proximity_score(x, y, w, h, w_frame, h_frame)
        density = float(raw_mask01[y:y + h, x:x + w].mean())
        # Compactness is now relative to a fixed, generous reference rather
        # than max_area_frac itself -- a genuinely large caption block
        # shouldn't be penalized just for being large the same way a small
        # corner mark would be for sprawling unexpectedly.
        compactness = max(0.0, 1.0 - area_frac / 0.5)
        score = density * 0.45 + corner_score * 0.30 + compactness * 0.25
        if debug:
            print(f"    region x={x} y={y} w={w} h={h} area_frac={area_frac:.4f} "
                  f"density={density:.3f} corner_score={corner_score:.3f} "
                  f"compactness={compactness:.3f} score={score:.3f}")
        scored.append(((x, y, w, h), score, area_frac))

    scored.sort(key=lambda t: t[1], reverse=True)
    kept = scored[:max_regions]

    final_boxes = []
    for (x, y, w, h), _score, pre_area_frac in kept:
        # Growth cap scales with how large the region already is: a tiny
        # mark stays tightly capped so it can't run away, but a region
        # that's already legitimately large (a caption block) is allowed
        # to keep growing to its real extent.
        grow_cap = float(np.clip(max(0.22, pre_area_frac * 2.0), 0.22, 0.50))
        gx, gy, gw, gh = _expand_box_by_density(raw_mask01, x, y, w, h,
                                                 max_frame_frac=grow_cap, debug=debug)
        final_area_frac = (gw * gh) / frame_area
        if final_area_frac > 0.55:
            if debug:
                print(f"  [watermark-detect] region still covers {final_area_frac:.0%} of the frame after "
                      f"growth -- reverting to pre-growth box {(x, y, w, h)}")
            gx, gy, gw, gh = x, y, w, h
        box = WatermarkBox(gx, gy, gw, gh).clamped(w_frame, h_frame, pad=0)
        if box.w > 0 and box.h > 0:
            final_boxes.append(box)

    if debug:
        for b in final_boxes:
            print(f"  [watermark-detect] -> final region x={b.x} y={b.y} w={b.w} h={b.h} "
                  f"(area_frac={b.w*b.h/frame_area:.4f})")
        if not final_boxes:
            print("  [watermark-detect] no regions survived to the end")

    return final_boxes


def detect_watermark_manual(path: str) -> Optional[WatermarkBox]:
    cap = cv2.VideoCapture(path)
    ok, frame = cap.read()
    cap.release()
    if not ok:
        print("Could not read a frame for manual selection.")
        return None
    print("Drag a box around the logo/watermark, then press ENTER or SPACE. 'c' to cancel.")
    box = cv2.selectROI("Select watermark - ENTER to confirm, C to cancel", frame,
                         showCrosshair=True, fromCenter=False)
    cv2.destroyAllWindows()
    x, y, w, h = box
    if w == 0 or h == 0:
        return None
    return WatermarkBox(int(x), int(y), int(w), int(h))


def watermark_from_pixels(spec: str) -> WatermarkBox:
    """Parse '--watermark-box x,y,w,h' -> WatermarkBox (fixed pixels, same on
    every video -- only safe if all videos share one resolution)."""
    try:
        x, y, w, h = [int(v.strip()) for v in spec.split(",")]
    except Exception:
        sys.exit(f"--watermark-box must be 'x,y,w,h' in pixels, got: {spec!r}")
    return WatermarkBox(x, y, w, h)


def watermark_from_pct(spec: str, frame_w: int, frame_h: int) -> WatermarkBox:
    """Parse '--watermark-box-pct x,y,w,h' (fractions 0-1) and scale to this
    video's actual resolution -- the right choice when videos in a batch
    have different resolutions but the credit sits in the same relative
    spot on all of them."""
    try:
        xp, yp, wp, hp = [float(v.strip()) for v in spec.split(",")]
    except Exception:
        sys.exit(f"--watermark-box-pct must be 'x,y,w,h' as fractions 0-1, got: {spec!r}")
    return watermark_box_from_fractions(xp, yp, wp, hp, frame_w, frame_h)


def save_watermark_preview(path: str, wms: List[WatermarkBox], out_path: str, at_sec: float = 2.0):
    """Grabs one frame and draws a red rectangle around every region that
    WOULD be delogo'd, so you can check alignment before rendering a whole
    batch."""
    frame_w, frame_h = ffprobe_dimensions(path)
    cmd = ["ffmpeg", "-y", "-ss", f"{at_sec:.2f}", "-i", path, "-frames:v", "1"]
    safe_boxes = []
    if wms:
        for wm in wms:
            safe = wm.clamped(frame_w, frame_h)
            if safe.w > 0 and safe.h > 0:
                safe_boxes.append(safe)
    if safe_boxes:
        drawboxes = ",".join(
            f"drawbox=x={b.x}:y={b.y}:w={b.w}:h={b.h}:color=red@0.9:thickness=4" for b in safe_boxes
        )
        cmd += ["-vf", drawboxes]
    cmd += [out_path]
    run(cmd)
    if not safe_boxes:
        print(f"  -> preview saved (NO watermark box detected/specified): {out_path}")
    else:
        boxes_desc = "; ".join(f"x={b.x} y={b.y} w={b.w} h={b.h}" for b in safe_boxes)
        print(f"  -> preview saved: {out_path}  [{len(safe_boxes)} box(es): {boxes_desc}]")


# --------------------------------------------------------------------------- #
# Splitting usable content into N-second parts
# --------------------------------------------------------------------------- #

def compute_parts(usable_start: float, usable_end: float, clip_sec: float,
                   keep_remainder: bool, two_part_fallback: bool = True,
                   two_part_min_sec: float = 10.0, equal_split: bool = True
                   ) -> List[Tuple[float, float]]:
    """Returns list of (part_start, part_duration).

    EQUAL-SPLIT MODE (equal_split=True, the default): the whole usable
    window is divided into the nearest whole number of roughly-equal parts
    sized close to clip_sec, so ALL usable content is used -- nothing is
    silently dropped as an uneven "remainder". E.g. a 119s usable window
    with clip_sec=60 becomes two ~59.5s parts (not one 60s part + a
    discarded 59s leftover). A usable window shorter than clip_sec still
    becomes two equal halves (as long as it's at least two_part_min_sec
    seconds total) rather than a single short clip or nothing, matching
    "make shorts either way" behavior for short source videos. Exact or
    near-exact multiples of clip_sec still divide the same way they always
    did (e.g. 600s usable / 60s clip = 10 even 60s parts).

    LEGACY MODE (equal_split=False): fixed-length clip_sec parts walked
    forward across the usable window; any final leftover shorter than
    clip_sec is kept only if keep_remainder is set (otherwise dropped), with
    the two_part_fallback still applying only when that produces zero parts.
    """
    usable_duration = usable_end - usable_start
    if usable_duration <= 0:
        return []

    if equal_split:
        if usable_duration < clip_sec:
            # Whole window doesn't even fit one clip_sec part -- split into
            # two equal halves instead of one oddly-short part or nothing,
            # as long as there's enough total content to be worth it.
            if two_part_fallback and usable_duration >= two_part_min_sec:
                half = usable_duration / 2.0
                return [(usable_start, half), (usable_start + half, half)]
            if keep_remainder and usable_duration > 5.0:
                return [(usable_start, usable_duration)]
            return []

        num_parts = max(1, round(usable_duration / clip_sec))
        part_len = usable_duration / num_parts
        return [(usable_start + i * part_len, part_len) for i in range(num_parts)]

    # --- legacy fixed-length mode ---
    parts = []
    t = usable_start
    while t + clip_sec <= usable_end + 1e-6:
        parts.append((t, clip_sec))
        t += clip_sec
    remainder = usable_end - t
    if keep_remainder and remainder > 5.0:   # only keep a remainder that's actually watchable
        parts.append((t, remainder))

    if two_part_fallback and not parts and usable_duration >= two_part_min_sec:
        half = usable_duration / 2.0
        parts = [(usable_start, half), (usable_start + half, half)]

    return parts


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def render_clip(input_path: str, output_path: str, start_sec: float, clip_sec: float,
                 watermarks: List[WatermarkBox], crf: int = 18, preset: str = "medium",
                 optimize: bool = True, max_dim: int = MAX_DIM,
                 video_bitrate: str = VIDEO_BITRATE, audio_bitrate: str = AUDIO_BITRATE,
                 encode_preset: str = "fast"):
    frame_w, frame_h = ffprobe_dimensions(input_path)

    filters = []
    if watermarks:
        for watermark in watermarks:
            # Extra removal padding beyond the detected box: soft/antialiased
            # edges of burnt-in text or a logo commonly extend a few px past
            # where edge-detection drew the line, so a purely tight box
            # leaves a faint sliver behind. Scaled to the box's own size
            # (with a small fixed floor) rather than one fixed pixel count,
            # so this pads a tiny credit line sensibly without over-padding
            # a large logo/caption block.
            removal_pad = max(6, int(round(0.12 * max(watermark.w, watermark.h))))
            safe_box = watermark.clamped(frame_w, frame_h, pad=removal_pad)
            if safe_box.w > 0 and safe_box.h > 0:
                filters.append(safe_box.as_ffmpeg_delogo())
            else:
                print("  !! A watermark region was invalid after clamping to frame bounds -- "
                      "skipping delogo for that region on this render.")

    # --- Output optimization: cap longest side to max_dim, keeping the
    # source aspect ratio and even dimensions (required by libx264's 4:2:0
    # chroma subsampling). Only downscales -- never upscales a video that's
    # already smaller than max_dim on its longest side.
    if optimize:
        longest = max(frame_w, frame_h)
        if longest > max_dim:
            if frame_w >= frame_h:
                scale_expr = f"scale={max_dim}:-2"
            else:
                scale_expr = f"scale=-2:{max_dim}"
            filters.append(scale_expr)

    cmd = ["ffmpeg", "-y", "-ss", f"{start_sec:.2f}", "-i", input_path, "-t", f"{clip_sec:.2f}"]
    if filters:
        cmd += ["-vf", ",".join(filters)]

    if optimize:
        # Bitrate-capped encode (target = max = bufsize-limited) instead of
        # a pure -crf quality encode, plus faststart so the moov atom is at
        # the front of the file for instant playback on mobile/social feeds.
        cmd += ["-c:v", "libx264", "-preset", encode_preset,
                "-b:v", video_bitrate, "-maxrate", video_bitrate, "-bufsize", video_bitrate,
                "-c:a", "aac", "-b:a", audio_bitrate,
                "-movflags", "+faststart", output_path]
    else:
        cmd += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                "-c:a", "aac", "-b:a", "160k", output_path]
    run(cmd)


# --------------------------------------------------------------------------- #
# Pipeline for one video
# --------------------------------------------------------------------------- #

def process_single(path: str, out_path_template: str, clip_sec: float, watermark_mode: str,
                    dry_run: bool, single_clip: bool, keep_remainder: bool,
                    shared_watermark_pcts: Optional[List[Tuple[float, float, float, float]]],
                    shared_intro_end: Optional[float],
                    shared_outro_len: Optional[float],
                    intro_override: Optional[float],
                    outro_override: Optional[float],
                    watermark_box_spec: Optional[str],
                    watermark_box_pct_spec: Optional[str],
                    debug_preview: bool,
                    debug_detect: bool,
                    intro_max_search: float = DEFAULT_INTRO_MIN_SCAN_SEC,
                    outro_max_search: float = DEFAULT_OUTRO_MAX_SEARCH_SEC,
                    outro_safety_margin: float = DEFAULT_OUTRO_SAFETY_MARGIN_SEC,
                    two_part_fallback: bool = True,
                    two_part_min_sec: float = 10.0,
                    watermark_max_area_frac: float = 0.45,
                    optimize: bool = True, max_dim: int = MAX_DIM,
                    video_bitrate: str = VIDEO_BITRATE, audio_bitrate: str = AUDIO_BITRATE,
                    encode_preset: str = "fast"):
    duration = ffprobe_duration(path)
    print(f"[{os.path.basename(path)}]")

    # --- intro ---
    if intro_override is not None:
        intro = BoundaryResult(intro_override, "manual_override", "high")
    elif shared_intro_end is not None:
        intro = BoundaryResult(shared_intro_end, "batch_common_prefix", "high")
    else:
        intro = detect_intro_single(path, max_search_sec=intro_max_search, debug=debug_detect)
    if intro.time_sec > duration - 5:
        intro = BoundaryResult(0.0, intro.method + "_rejected_too_long", "none")

    # --- outro ---
    # NOTE: the safety margin is only applied to DETECTED boundaries (plain
    # detection or the shared-batch-length case) -- an explicit
    # --outro-sec/--outro-override is taken at face value since the user
    # already told us exactly where to cut.
    if outro_override is not None:
        outro_start = outro_override
        outro_method, outro_conf = "manual_override", "high"
    elif shared_outro_len is not None:
        outro_start = duration - shared_outro_len
        outro_method, outro_conf = "batch_common_suffix", "high"
    else:
        outro = detect_outro_single(path, max_search_sec=outro_max_search,
                                     safety_margin=outro_safety_margin, debug=debug_detect)
        outro_start, outro_method, outro_conf = outro.time_sec, outro.method, outro.confidence
    if outro_start < intro.time_sec + 5:
        outro_start, outro_method, outro_conf = duration, outro_method + "_rejected_too_short", "none"

    usable_start, usable_end = intro.time_sec, outro_start

    # --- watermark / credit text (may be MULTIPLE separate regions: a
    # caption block and a separate logo mark, for example) ---
    frame_w, frame_h = ffprobe_dimensions(path)
    if watermark_box_spec is not None:
        wms = [watermark_from_pixels(watermark_box_spec)]
        wm_source = "manual_pixels"
    elif watermark_box_pct_spec is not None:
        wms = [watermark_from_pct(watermark_box_pct_spec, frame_w, frame_h)]
        wm_source = "manual_pct"
    elif shared_watermark_pcts is not None:
        # Rescaled from FRACTIONS of the first video's frame, not raw pixel
        # coordinates -- this is what makes --shared-watermark-from-first
        # work correctly across a batch with mixed resolutions instead of
        # reusing pixel coordinates that fall off-frame on smaller videos.
        wms = [watermark_box_from_fractions(xp, yp, wp, hp, frame_w, frame_h)
               for (xp, yp, wp, hp) in shared_watermark_pcts]
        wm_source = "shared_from_first_scaled"
    elif watermark_mode == "auto":
        # start_sec=usable_start ensures we only ever sample AFTER the intro
        # (i.e. never inside a black-screen intro) when looking for the logo.
        wms = detect_watermark_auto(path, start_sec=usable_start, end_sec=usable_end,
                                     max_area_frac=watermark_max_area_frac, debug=debug_detect)
        wm_source = "auto_detect"
        if not wms:
            print("  !! No watermark auto-detected. Run with --debug-detect --debug-preview to see "
                  "candidate scoring, or skip detection entirely with --watermark-box x,y,w,h / "
                  "--watermark-box-pct x,y,w,h.")
    elif watermark_mode == "manual":
        manual_box = detect_watermark_manual(path)
        wms = [manual_box] if manual_box is not None else []
        wm_source = "manual_select"
    else:
        wms = []
        wm_source = "none"

    # --- parts ---
    if single_clip:
        parts = [(usable_start, min(clip_sec, max(0.0, usable_end - usable_start)))]
    else:
        parts = compute_parts(usable_start, usable_end, clip_sec, keep_remainder,
                               two_part_fallback=two_part_fallback,
                               two_part_min_sec=two_part_min_sec)

    print(f"  duration         : {duration:.2f}s")
    print(f"  intro end        : {intro.time_sec:.2f}s  (method={intro.method}, confidence={intro.confidence})")
    print(f"  outro start      : {outro_start:.2f}s  (method={outro_method}, confidence={outro_conf})")
    print(f"  usable window    : {usable_start:.2f}s -> {usable_end:.2f}s  ({max(0.0, usable_end-usable_start):.2f}s usable)")
    if wms:
        boxes_desc = "; ".join(f"x={b.x} y={b.y} w={b.w} h={b.h}" for b in wms)
        print(f"  watermark region(s): {len(wms)} ({wm_source}) -- {boxes_desc}")
    else:
        print("  watermark region(s): none")
    if optimize:
        print(f"  optimize         : ON  (max-dim={max_dim}px, video-bitrate={video_bitrate}, "
              f"audio-bitrate={audio_bitrate}, preset={encode_preset}, faststart)")
    else:
        print("  optimize         : OFF (source-resolution CRF encode)")
    print(f"  parts to produce : {len(parts)}")
    for i, (pstart, pdur) in enumerate(parts, 1):
        print(f"    part {i:02d}: {pstart:.2f}s -> {pstart+pdur:.2f}s  ({pdur:.2f}s)")
    if len(parts) == 0 and not single_clip:
        usable = max(0.0, usable_end - usable_start)
        if two_part_fallback:
            print(f"  !! 0 parts: usable window ({usable:.2f}s) is shorter than --two-part-min-sec "
                  f"({two_part_min_sec:.0f}s), so even the two-equal-part fallback was skipped. "
                  f"Lower --two-part-min-sec if you still want output from this clip.")
        else:
            print(f"  !! 0 parts: usable window ({usable:.2f}s) is shorter than --clip-seconds ({clip_sec:.0f}s). "
                  f"Pass --keep-remainder to keep it as one shorter part, lower --clip-seconds, "
                  f"or drop --no-two-part-fallback to let it split into two equal parts automatically.")

    if debug_preview:
        base_dir = os.path.dirname(out_path_template) or "."
        base_name = os.path.splitext(os.path.basename(out_path_template))[0]
        os.makedirs(base_dir, exist_ok=True)
        preview_path = os.path.join(base_dir, f"{base_name}_wm_preview.png")
        # Grab a frame from the MIDDLE of the usable window, not right after
        # the intro -- a frame near usable_start can still look intro-ish
        # (fade-in, title card, etc.) and isn't representative of where the
        # watermark actually sits during normal content.
        preview_at = usable_start + (usable_end - usable_start) / 2.0
        preview_at = min(preview_at, max(0.0, usable_end - 0.5))
        save_watermark_preview(path, wms, preview_path, at_sec=preview_at)

    if dry_run:
        return

    base_dir = os.path.dirname(out_path_template) or "."
    base_name = os.path.splitext(os.path.basename(out_path_template))[0]
    ext = os.path.splitext(out_path_template)[1] or ".mp4"
    os.makedirs(base_dir, exist_ok=True)

    if len(parts) == 0:
        print("  !! No parts to render (usable window too short).")
        return

    for i, (pstart, pdur) in enumerate(parts, 1):
        if pdur <= 0:
            continue
        if len(parts) == 1:
            out_path = os.path.join(base_dir, base_name + ext)
        else:
            out_path = os.path.join(base_dir, f"{base_name}_part{i:02d}{ext}")
        render_clip(path, out_path, pstart, pdur, wms,
                    optimize=optimize, max_dim=max_dim, video_bitrate=video_bitrate,
                    audio_bitrate=audio_bitrate, encode_preset=encode_preset)
        print(f"  -> wrote {out_path}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", help="Single input video path")
    ap.add_argument("--output", help="Output path/template for single-video mode (parts get _partNN suffix)")
    ap.add_argument("--batch", help="Folder of input videos for batch mode")
    ap.add_argument("--outdir", help="Output folder for batch mode")
    ap.add_argument("--mode", choices=["single", "batch"], default="single")
    ap.add_argument("--clip-seconds", type=float, default=60.0, help="Length of each part (default 60s)")
    ap.add_argument("--single-clip", action="store_true",
                     help="Old behavior: produce only ONE clip per video instead of splitting into multiple parts.")
    ap.add_argument("--keep-remainder", action="store_true",
                     help="Keep a final short leftover part if usable content doesn't divide evenly (>5s).")
    ap.add_argument("--two-part-fallback", dest="two_part_fallback", action="store_true", default=True,
                     help="(default ON) If the usable window is too short to fit even one --clip-seconds "
                          "part, split it into TWO EQUAL-LENGTH parts instead of producing 0 parts.")
    ap.add_argument("--no-two-part-fallback", dest="two_part_fallback", action="store_false",
                     help="Disable the two-equal-part fallback; revert to producing 0 parts when the "
                          "usable window is too short for --clip-seconds.")
    ap.add_argument("--two-part-min-sec", type=float, default=10.0,
                     help="Minimum total usable seconds required before the two-part fallback will "
                          "bother splitting (default 10s). Below this, the clip is treated as too "
                          "short to be worth splitting and no parts are produced.")
    ap.add_argument("--watermark", choices=["auto", "manual", "none"], default="auto")
    ap.add_argument("--shared-watermark-from-first", action="store_true",
                     help="Batch mode: detect watermark once on the first video, reuse for all.")
    ap.add_argument("--watermark-box", default=None,
                     help="Skip detection entirely: fixed pixel box 'x,y,w,h' used for every video. "
                          "Use when all videos share one resolution.")
    ap.add_argument("--watermark-box-pct", default=None,
                     help="Skip detection entirely: box as fractions of frame 'x,y,w,h' (0-1), "
                          "rescaled per video. Use when videos have different resolutions but the "
                          "credit sits in the same relative spot on all of them.")
    ap.add_argument("--watermark-max-area-pct", type=float, default=15.0,
                     help="Auto-detect only: reject any candidate logo/credit-text region larger "
                          "than this percent of the frame area (default 15). Real watermarks are "
                          "small; raising this is rarely needed and risks catching real picture "
                          "content instead of a logo.")
    ap.add_argument("--intro-max-search", type=float, default=DEFAULT_INTRO_MIN_SCAN_SEC,
                     help="How far into the video (seconds) to look for where the intro/black screen "
                          "ends. Default 15s -- this is a hard cap: detection never looks past this "
                          "point, so content in the middle of the video is never mistaken for the "
                          "intro. Raise it only if your real intros run longer than 15s.")
    ap.add_argument("--outro-max-search", type=float, default=DEFAULT_OUTRO_MAX_SEARCH_SEC,
                     help="How far from the END of the video (seconds) to look for where the outro "
                          "begins. Default 15s -- this is a hard cap: detection never looks earlier "
                          "than this point from the end, so content in the middle of the video is "
                          "never mistaken for the outro. Raise it if your real outros run longer.")
    ap.add_argument("--outro-safety-margin", type=float, default=DEFAULT_OUTRO_SAFETY_MARGIN_SEC,
                     help="Extra seconds to strip BEFORE whatever outro boundary is detected (default "
                          "2s, sensible range 1-5s). Covers the tail of real content that's already "
                          "mid-fade/offset/dim into the outro by the time detection actually trips. "
                          "Only applied to DETECTED boundaries, not to an explicit --outro-sec.")
    ap.add_argument("--max-dim", type=int, default=MAX_DIM,
                     help=f"Optimization: cap the longest side of each output part to this many "
                          f"pixels, preserving aspect ratio (default {MAX_DIM}). Only downscales, "
                          f"never upscales. Ignored if --no-optimize is set.")
    ap.add_argument("--video-bitrate", default=VIDEO_BITRATE,
                     help=f"Optimization: capped/target video bitrate, e.g. '1.5M' or '800k' "
                          f"(default {VIDEO_BITRATE}). Ignored if --no-optimize is set.")
    ap.add_argument("--audio-bitrate", default=AUDIO_BITRATE,
                     help=f"Optimization: audio bitrate, e.g. '128k' (default {AUDIO_BITRATE}). "
                          f"Ignored if --no-optimize is set.")
    ap.add_argument("--encode-preset", default="fast",
                     help="Optimization: ffmpeg -preset used for the optimized encode (default "
                          "'fast'; 'ultrafast' is quicker but produces larger files). Ignored if "
                          "--no-optimize is set.")
    ap.add_argument("--no-optimize", dest="optimize", action="store_false", default=True,
                     help="Disable output optimization (max-dim/bitrate cap/faststart) entirely; "
                          "falls back to a plain --crf quality encode at source resolution.")
    ap.add_argument("--debug-preview", action="store_true",
                     help="Save a PNG per video with a red box around whatever region will actually "
                          "be delogo'd (detected or manual), so you can check alignment before "
                          "rendering the whole batch.")
    ap.add_argument("--debug-detect", action="store_true",
                     help="Print per-candidate scoring during auto watermark AND intro/outro detection.")
    ap.add_argument("--intro-sec", type=float, default=None,
                     help="Manually force intro length in seconds (skips detection).")
    ap.add_argument("--outro-sec", type=float, default=None,
                     help="Manually force outro START timestamp in seconds (skips detection, and "
                          "skips the safety margin -- this value is used exactly as given).")
    ap.add_argument("--no-outro-detect", action="store_true",
                     help="Disable outro detection entirely (only intro is removed).")
    ap.add_argument("--dry-run", action="store_true", help="Only print detections, do not render.")
    args = ap.parse_args()

    if args.mode == "single":
        if not args.input:
            sys.exit("--input is required in single mode")
        out_template = args.output or (os.path.splitext(args.input)[0] + "_out.mp4")
        outro_override = args.outro_sec if not args.no_outro_detect else 10**9
        process_single(args.input, out_template, args.clip_seconds, args.watermark,
                        args.dry_run, args.single_clip, args.keep_remainder,
                        shared_watermark_pcts=None, shared_intro_end=None, shared_outro_len=None,
                        intro_override=args.intro_sec, outro_override=outro_override,
                        watermark_box_spec=args.watermark_box,
                        watermark_box_pct_spec=args.watermark_box_pct,
                        debug_preview=args.debug_preview, debug_detect=args.debug_detect,
                        intro_max_search=args.intro_max_search,
                        outro_max_search=args.outro_max_search,
                        outro_safety_margin=args.outro_safety_margin,
                        two_part_fallback=args.two_part_fallback,
                        two_part_min_sec=args.two_part_min_sec,
                        watermark_max_area_frac=args.watermark_max_area_pct / 100.0,
                        optimize=args.optimize, max_dim=args.max_dim,
                        video_bitrate=args.video_bitrate, audio_bitrate=args.audio_bitrate,
                        encode_preset=args.encode_preset)

    else:  # batch
        if not args.batch:
            sys.exit("--batch <folder> is required in batch mode")
        outdir = args.outdir or (args.batch.rstrip("/\\") + "_out")
        os.makedirs(outdir, exist_ok=True)

        exts = ("*.mp4", "*.mov", "*.mkv", "*.avi", "*.webm")
        paths = []
        for e in exts:
            paths.extend(glob.glob(os.path.join(args.batch, e)))
        paths.sort()
        if not paths:
            sys.exit(f"No videos found in {args.batch}")
        print(f"Found {len(paths)} videos.")

        shared_intro_end = args.intro_sec
        shared_outro_len = None  # computed as a length, applied per-video from its own end

        if shared_intro_end is None and len(paths) >= 2:
            print("Detecting shared intro across batch...")
            r = detect_intro_batch(paths, scan_seconds=args.intro_max_search)
            print(f"  -> {r.time_sec:.2f}s (confidence={r.confidence})")
            if r.confidence != "none":
                shared_intro_end = r.time_sec

        if args.outro_sec is None and not args.no_outro_detect and len(paths) >= 2:
            print("Detecting shared outro across batch...")
            outro_len = detect_outro_batch(paths, safety_margin=args.outro_safety_margin)
            if outro_len:
                print(f"  -> {outro_len:.2f}s shared outro length, including safety margin "
                      f"(confidence=high)")
                shared_outro_len = outro_len
            else:
                print("  -> no confident shared outro found; falling back to per-video detection")

        # Stored as FRACTIONS of the first video's own frame size, not raw
        # pixel coordinates -- a batch commonly mixes resolutions (as in
        # this run: 1920x1080, 1280x720, 960x540), and reusing the first
        # video's pixel box verbatim on a smaller frame put the box
        # partially or entirely off-frame, which ffmpeg's delogo filter
        # rejects outright ("Logo area is outside of the frame"), crashing
        # every part of every differently-sized video in the batch.
        # Converting to fractions once here and rescaling per video (see
        # the shared_watermark_pcts branch in process_single) fixes that.
        # This may be MULTIPLE regions (e.g. a caption block plus a
        # separate logo mark) -- all of them get carried through together.
        shared_wm_pcts = None
        if args.watermark_box is None and args.watermark_box_pct is None:
            if args.watermark == "auto" and args.shared_watermark_from_first:
                probe_start = shared_intro_end or 0.0
                probe_end = ffprobe_duration(paths[0]) - (shared_outro_len or 0.0)
                shared_wms = detect_watermark_auto(paths[0], start_sec=probe_start, end_sec=probe_end,
                                                    max_area_frac=args.watermark_max_area_pct / 100.0,
                                                    debug=args.debug_detect)
                if shared_wms:
                    src_w, src_h = ffprobe_dimensions(paths[0])
                    shared_wm_pcts = [wm.to_fractions(src_w, src_h) for wm in shared_wms]
                    boxes_desc = "; ".join(f"x={wm.x} y={wm.y} w={wm.w} h={wm.h}" for wm in shared_wms)
                    print(f"Shared watermark region(s) (from first video, {src_w}x{src_h}): "
                          f"{len(shared_wms)} -- {boxes_desc} -- will be rescaled per video's own resolution.")
                else:
                    print("Shared watermark detection found nothing -- consider --watermark-box / "
                          "--watermark-box-pct, or --debug-preview + --debug-detect to diagnose.")
            elif args.watermark == "manual" and args.shared_watermark_from_first:
                shared_wm = detect_watermark_manual(paths[0])
                if shared_wm:
                    src_w, src_h = ffprobe_dimensions(paths[0])
                    shared_wm_pcts = [shared_wm.to_fractions(src_w, src_h)]

        for p in paths:
            base = os.path.splitext(os.path.basename(p))[0]
            out_template = os.path.join(outdir, base + ".mp4")
            per_video_outro_override = None
            if args.outro_sec is not None:
                per_video_outro_override = args.outro_sec
            elif args.no_outro_detect:
                per_video_outro_override = 10**9

            try:
                process_single(p, out_template, args.clip_seconds, args.watermark,
                                args.dry_run, args.single_clip, args.keep_remainder,
                                shared_watermark_pcts=shared_wm_pcts,
                                shared_intro_end=shared_intro_end,
                                shared_outro_len=shared_outro_len,
                                intro_override=None if shared_intro_end is not None else args.intro_sec,
                                outro_override=per_video_outro_override,
                                watermark_box_spec=args.watermark_box,
                                watermark_box_pct_spec=args.watermark_box_pct,
                                debug_preview=args.debug_preview, debug_detect=args.debug_detect,
                                intro_max_search=args.intro_max_search,
                                outro_max_search=args.outro_max_search,
                                outro_safety_margin=args.outro_safety_margin,
                                two_part_fallback=args.two_part_fallback,
                                two_part_min_sec=args.two_part_min_sec,
                                watermark_max_area_frac=args.watermark_max_area_pct / 100.0,
                                optimize=args.optimize, max_dim=args.max_dim,
                                video_bitrate=args.video_bitrate, audio_bitrate=args.audio_bitrate,
                                encode_preset=args.encode_preset)
            except Exception:
                # Don't let one bad file (corrupt frame, ffmpeg edge case,
                # weird resolution, etc.) kill the whole batch -- print the
                # full traceback for that video so it can be debugged, then
                # move on to the rest.
                import traceback
                print(f"  !! ERROR processing {os.path.basename(p)} -- skipping this video and "
                      f"continuing with the rest of the batch.")
                traceback.print_exc()
                print()
                continue


if __name__ == "__main__":
    main()
