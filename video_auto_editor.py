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

If AUTO detection isn't landing on your credit text (common for horizontal
text lines, since they're made of several separate letter/word blobs), you
have two more reliable options:

  1. Check what it WOULD remove before committing to a whole batch:

        python video_auto_editor.py --input movie.mp4 --debug-preview

     This writes movie_wm_preview.png with a red box around whatever region
     will actually be delogo'd -- detected OR manually specified -- so you
     can verify alignment on one frame before rendering anything.

  2. Skip detection entirely and specify the exact region yourself, either
     in fixed pixels (same box on every video -- use this if all your
     videos share one resolution):

        --watermark-box 1200,880,700,180        # x,y,w,h in pixels

     ...or as PERCENTAGES of the frame (use this if videos have different
     resolutions but the credit sits in the same relative spot on all of
     them -- this is the more "dynamic" option for a mixed-resolution batch):

        --watermark-box-pct 0.62,0.80,0.35,0.12   # x,y,w,h as fractions 0-1

--------------------------------------------------------------------------

Requirements
------------
    pip install opencv-python numpy imagehash pillow --break-system-packages
    ffmpeg must be installed and on PATH.
"""

import argparse
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


def detect_intro_single(path: str, max_search_sec: float = 90.0) -> BoundaryResult:
    duration = ffprobe_duration(path)
    window = min(max_search_sec, duration * 0.4)
    stats = _sample_frame_stats(path, window, start_offset=0.0)
    if len(stats) < 4:
        return BoundaryResult(0.0, "insufficient_data", "none")

    black_runs = _find_black_runs(stats)
    candidate_gaps = [g for g in black_runs if g[0] > 0.5]
    if candidate_gaps:
        return BoundaryResult(round(candidate_gaps[0][1], 2), "black_frame_gap", "high")

    cuts = _strongest_cuts(stats)
    if cuts:
        best_t, best_score = max(cuts, key=lambda x: x[1])
        if best_score > 0.35:
            return BoundaryResult(round(best_t, 2), "scene_cut", "medium")

    return BoundaryResult(0.0, "no_clear_boundary", "none")


def detect_outro_single(path: str, max_search_sec: float = 60.0) -> BoundaryResult:
    """Scans the LAST portion of the video for the earliest strong boundary
    (black gap or scene cut) -- that's treated as where the outro/credits/end
    card begins. Earliest (not strongest/last) is used deliberately: outros
    often contain several cuts (credits, end-card, subscribe CTA...), and we
    want the first one, i.e. where main content actually stops."""
    duration = ffprobe_duration(path)
    window = min(max_search_sec, duration * 0.4)
    start_offset = max(0.0, duration - window)
    stats = _sample_frame_stats(path, window, start_offset=start_offset)
    if len(stats) < 4:
        return BoundaryResult(duration, "insufficient_data", "none")

    black_runs = _find_black_runs(stats)
    # ignore a black run that's basically the true end of file (nothing after it to trim)
    candidate_gaps = [g for g in black_runs if g[0] < duration - 0.5]
    if candidate_gaps:
        return BoundaryResult(round(candidate_gaps[0][0], 2), "black_frame_gap", "high")

    cuts = _strongest_cuts(stats)
    strong_cuts = [c for c in cuts if c[1] > 0.35]
    if strong_cuts:
        earliest_t, _score = min(strong_cuts, key=lambda x: x[0])
        return BoundaryResult(round(earliest_t, 2), "scene_cut", "medium")

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


def detect_intro_batch(paths: List[str], scan_seconds: float = 30.0,
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
                        hamming_thresh: int = 8) -> Optional[float]:
    """Returns the shared outro LENGTH (seconds, counted from each video's own
    end backward) if all videos share a common trailing sequence, else None.
    Because each video may have a different total duration, this compares
    each video's own final `scan_seconds` against every other video's own
    final `scan_seconds`, aligned from the end."""
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
    return float(matched) if matched >= 2 else None


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
        breathing room, not just to stay in-bounds."""
        x_min, y_min = margin, margin
        x_max, y_max = frame_w - margin, frame_h - margin

        x = max(self.x - pad, x_min)
        y = max(self.y - pad, y_min)
        w = self.w + 2 * pad
        h = self.h + 2 * pad

        w = min(w, x_max - x)
        h = min(h, y_max - y)
        w = max(2, w - (w % 2))
        h = max(2, h - (h % 2))
        return WatermarkBox(x, y, w, h)

    def as_ffmpeg_delogo(self) -> str:
        return f"delogo=x={self.x}:y={self.y}:w={self.w}:h={self.h}:show=0"


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


def detect_watermark_auto(path: str, start_sec: float, end_sec: float, sample_count: int = 40,
                           min_area_frac: float = 0.0005, max_area_frac: float = 0.12,
                           merge_gap_px: int = 28, debug: bool = False
                           ) -> Optional[WatermarkBox]:
    """Finds a static, persistently-edged region (logo OR text credit line)
    and returns ONE bounding box covering it.

    Two changes vs. a naive corner-logo detector, specifically to handle
    horizontal credit TEXT (which is made of several disconnected letter/word
    blobs rather than one solid shape):
      1. All candidate blobs are clustered/merged by proximity into full
         text-line-sized boxes before scoring, instead of keeping only the
         single best individual contour.
      2. Scoring rewards proximity to ANY frame edge (top/bottom/left/right),
         not just the four corners -- credit lines commonly run along the
         bottom edge without being tucked into a corner.
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
        return None

    n = len(frames_gray)
    edge_freq = edge_accum / (255.0 * n)

    stack = np.stack(frames_gray, axis=0).astype(np.float32)
    variance = stack.var(axis=0)
    var_norm = variance / (variance.max() + 1e-6)

    persistent_mask = ((edge_freq > 0.70) & (var_norm < 0.18)).astype(np.uint8) * 255
    # Wider horizontal kernel: bridges gaps BETWEEN LETTERS/WORDS on the same
    # line so a text credit closes into one connected blob per line instead
    # of staying as many disconnected letter-shaped fragments.
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 9))
    persistent_mask = cv2.dilate(persistent_mask, kernel, iterations=2)
    persistent_mask = cv2.morphologyEx(persistent_mask, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(persistent_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        if debug:
            print("  [watermark-detect] no contours passed the persistent-edge mask at all")
        return None

    h_frame, w_frame = frames_gray[0].shape
    frame_area = h_frame * w_frame

    raw_boxes = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        area_frac = (w * h) / frame_area
        if not (min_area_frac <= area_frac <= max_area_frac):
            continue
        raw_boxes.append((x, y, w, h))

    if debug:
        print(f"  [watermark-detect] {len(contours)} raw contours -> {len(raw_boxes)} within area range")

    if not raw_boxes:
        return None

    # Merge nearby/overlapping blobs (letters/words of the same credit line)
    # into single boxes before scoring.
    clustered = _cluster_boxes(raw_boxes, gap=merge_gap_px)

    if debug:
        print(f"  [watermark-detect] {len(clustered)} clusters after merging (gap={merge_gap_px}px)")

    best_box, best_score = None, -1.0
    for (x, y, w, h) in clustered:
        area_frac = (w * h) / frame_area
        if not (min_area_frac <= area_frac <= max_area_frac * 2):  # allow merged text lines to run a bit larger
            continue
        cx, cy = x + w / 2, y + h / 2
        # Distance to the NEAREST edge (not just corners) -- rewards a box
        # sitting along the top/bottom/left/right border generally.
        dist_to_edge = min(cx, w_frame - cx, cy, h_frame - cy)
        edge_score = 1.0 / (1.0 + dist_to_edge / (min(w_frame, h_frame) * 0.5))
        density = persistent_mask[y:y+h, x:x+w].mean() / 255.0
        score = edge_score * 0.55 + density * 0.45
        if debug:
            print(f"    candidate x={x} y={y} w={w} h={h} area_frac={area_frac:.4f} "
                  f"edge_score={edge_score:.3f} density={density:.3f} score={score:.3f}")
        if score > best_score:
            best_score, best_box = score, WatermarkBox(x, y, w, h)

    if best_box is not None:
        best_box = best_box.clamped(w_frame, h_frame, pad=0)  # ensure raw box itself is in-bounds

    return best_box


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
    x = round(xp * frame_w)
    y = round(yp * frame_h)
    w = round(wp * frame_w)
    h = round(hp * frame_h)
    return WatermarkBox(x, y, w, h)


def save_watermark_preview(path: str, wm: Optional[WatermarkBox], out_path: str, at_sec: float = 2.0):
    """Grabs one frame and draws a red rectangle around the box that WOULD be
    delogo'd, so you can check alignment before rendering a whole batch."""
    frame_w, frame_h = ffprobe_dimensions(path)
    cmd = ["ffmpeg", "-y", "-ss", f"{at_sec:.2f}", "-i", path, "-frames:v", "1"]
    if wm is not None:
        safe = wm.clamped(frame_w, frame_h)
        drawbox = f"drawbox=x={safe.x}:y={safe.y}:w={safe.w}:h={safe.h}:color=red@0.9:thickness=4"
        cmd += ["-vf", drawbox]
    cmd += [out_path]
    run(cmd)
    if wm is None:
        print(f"  -> preview saved (NO watermark box detected/specified): {out_path}")
    else:
        print(f"  -> preview saved: {out_path}  [box: x={wm.x} y={wm.y} w={wm.w} h={wm.h}]")


# --------------------------------------------------------------------------- #
# Splitting usable content into N-second parts
# --------------------------------------------------------------------------- #

def compute_parts(usable_start: float, usable_end: float, clip_sec: float,
                   keep_remainder: bool) -> List[Tuple[float, float]]:
    """Returns list of (part_start, part_duration)."""
    parts = []
    t = usable_start
    while t + clip_sec <= usable_end + 1e-6:
        parts.append((t, clip_sec))
        t += clip_sec
    remainder = usable_end - t
    if keep_remainder and remainder > 5.0:   # only keep a remainder that's actually watchable
        parts.append((t, remainder))
    return parts


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def render_clip(input_path: str, output_path: str, start_sec: float, clip_sec: float,
                 watermark: Optional[WatermarkBox], crf: int = 18, preset: str = "medium"):
    filters = []
    if watermark is not None:
        frame_w, frame_h = ffprobe_dimensions(input_path)
        safe_box = watermark.clamped(frame_w, frame_h)
        if safe_box.w > 0 and safe_box.h > 0:
            filters.append(safe_box.as_ffmpeg_delogo())
        else:
            print("  !! Watermark box invalid after clamping to frame bounds -- skipping delogo for this render.")

    cmd = ["ffmpeg", "-y", "-ss", f"{start_sec:.2f}", "-i", input_path, "-t", f"{clip_sec:.2f}"]
    if filters:
        cmd += ["-vf", ",".join(filters)]
    cmd += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-c:a", "aac", "-b:a", "160k", output_path]
    run(cmd)


# --------------------------------------------------------------------------- #
# Pipeline for one video
# --------------------------------------------------------------------------- #

def process_single(path: str, out_path_template: str, clip_sec: float, watermark_mode: str,
                    dry_run: bool, single_clip: bool, keep_remainder: bool,
                    shared_watermark: Optional[WatermarkBox],
                    shared_intro_end: Optional[float],
                    shared_outro_len: Optional[float],
                    intro_override: Optional[float],
                    outro_override: Optional[float],
                    watermark_box_spec: Optional[str],
                    watermark_box_pct_spec: Optional[str],
                    debug_preview: bool,
                    debug_detect: bool):
    duration = ffprobe_duration(path)

    # --- intro ---
    if intro_override is not None:
        intro = BoundaryResult(intro_override, "manual_override", "high")
    elif shared_intro_end is not None:
        intro = BoundaryResult(shared_intro_end, "batch_common_prefix", "high")
    else:
        intro = detect_intro_single(path)
    if intro.time_sec > duration - 5:
        intro = BoundaryResult(0.0, intro.method + "_rejected_too_long", "none")

    # --- outro ---
    if outro_override is not None:
        outro_start = outro_override
        outro_method, outro_conf = "manual_override", "high"
    elif shared_outro_len is not None:
        outro_start = duration - shared_outro_len
        outro_method, outro_conf = "batch_common_suffix", "high"
    else:
        outro = detect_outro_single(path)
        outro_start, outro_method, outro_conf = outro.time_sec, outro.method, outro.confidence
    if outro_start < intro.time_sec + 5:
        outro_start, outro_method, outro_conf = duration, outro_method + "_rejected_too_short", "none"

    usable_start, usable_end = intro.time_sec, outro_start

    # --- watermark / credit text ---
    frame_w, frame_h = ffprobe_dimensions(path)
    if watermark_box_spec is not None:
        wm = watermark_from_pixels(watermark_box_spec)
        wm_source = "manual_pixels"
    elif watermark_box_pct_spec is not None:
        wm = watermark_from_pct(watermark_box_pct_spec, frame_w, frame_h)
        wm_source = "manual_pct"
    elif shared_watermark is not None:
        wm = shared_watermark
        wm_source = "shared_from_first"
    elif watermark_mode == "auto":
        wm = detect_watermark_auto(path, start_sec=usable_start, end_sec=usable_end, debug=debug_detect)
        wm_source = "auto_detect"
    elif watermark_mode == "manual":
        wm = detect_watermark_manual(path)
        wm_source = "manual_select"
    else:
        wm = None
        wm_source = "none"

    # --- parts ---
    if single_clip:
        parts = [(usable_start, min(clip_sec, max(0.0, usable_end - usable_start)))]
    else:
        parts = compute_parts(usable_start, usable_end, clip_sec, keep_remainder)

    print(f"[{os.path.basename(path)}]")
    print(f"  duration         : {duration:.2f}s")
    print(f"  intro end        : {intro.time_sec:.2f}s  (method={intro.method}, confidence={intro.confidence})")
    print(f"  outro start      : {outro_start:.2f}s  (method={outro_method}, confidence={outro_conf})")
    print(f"  usable window    : {usable_start:.2f}s -> {usable_end:.2f}s  ({max(0.0, usable_end-usable_start):.2f}s usable)")
    print(f"  watermark box    : {'x=%d y=%d w=%d h=%d (%s)' % (wm.x, wm.y, wm.w, wm.h, wm_source) if wm else 'none'}")
    print(f"  parts to produce : {len(parts)}")
    for i, (pstart, pdur) in enumerate(parts, 1):
        print(f"    part {i:02d}: {pstart:.2f}s -> {pstart+pdur:.2f}s  ({pdur:.2f}s)")

    if debug_preview:
        base_dir = os.path.dirname(out_path_template) or "."
        base_name = os.path.splitext(os.path.basename(out_path_template))[0]
        os.makedirs(base_dir, exist_ok=True)
        preview_path = os.path.join(base_dir, f"{base_name}_wm_preview.png")
        save_watermark_preview(path, wm, preview_path, at_sec=min(2.0, max(0.0, usable_start + 1.0)))

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
        render_clip(path, out_path, pstart, pdur, wm)
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
    ap.add_argument("--debug-preview", action="store_true",
                     help="Save a PNG per video with a red box around whatever region will actually "
                          "be delogo'd (detected or manual), so you can check alignment before "
                          "rendering the whole batch.")
    ap.add_argument("--debug-detect", action="store_true",
                     help="Print per-candidate scoring during auto watermark detection.")
    ap.add_argument("--intro-sec", type=float, default=None,
                     help="Manually force intro length in seconds (skips detection).")
    ap.add_argument("--outro-sec", type=float, default=None,
                     help="Manually force outro START timestamp in seconds (skips detection).")
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
                        shared_watermark=None, shared_intro_end=None, shared_outro_len=None,
                        intro_override=args.intro_sec, outro_override=outro_override,
                        watermark_box_spec=args.watermark_box,
                        watermark_box_pct_spec=args.watermark_box_pct,
                        debug_preview=args.debug_preview, debug_detect=args.debug_detect)

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
            r = detect_intro_batch(paths)
            print(f"  -> {r.time_sec:.2f}s (confidence={r.confidence})")
            if r.confidence != "none":
                shared_intro_end = r.time_sec

        if args.outro_sec is None and not args.no_outro_detect and len(paths) >= 2:
            print("Detecting shared outro across batch...")
            outro_len = detect_outro_batch(paths)
            if outro_len:
                print(f"  -> {outro_len:.2f}s shared outro length (confidence=high)")
                shared_outro_len = outro_len
            else:
                print("  -> no confident shared outro found; falling back to per-video detection")

        shared_wm = None
        if args.watermark_box is None and args.watermark_box_pct is None:
            if args.watermark == "auto" and args.shared_watermark_from_first:
                probe_start = shared_intro_end or 0.0
                probe_end = ffprobe_duration(paths[0]) - (shared_outro_len or 0.0)
                shared_wm = detect_watermark_auto(paths[0], start_sec=probe_start, end_sec=probe_end,
                                                   debug=args.debug_detect)
                if shared_wm:
                    print(f"Shared watermark box (from first video): x={shared_wm.x} y={shared_wm.y} "
                          f"w={shared_wm.w} h={shared_wm.h}")
                else:
                    print("Shared watermark detection found nothing -- consider --watermark-box / "
                          "--watermark-box-pct, or --debug-preview + --debug-detect to diagnose.")
            elif args.watermark == "manual" and args.shared_watermark_from_first:
                shared_wm = detect_watermark_manual(paths[0])

        for p in paths:
            base = os.path.splitext(os.path.basename(p))[0]
            out_template = os.path.join(outdir, base + ".mp4")
            per_video_outro_override = None
            if args.outro_sec is not None:
                per_video_outro_override = args.outro_sec
            elif args.no_outro_detect:
                per_video_outro_override = 10**9

            process_single(p, out_template, args.clip_seconds, args.watermark,
                            args.dry_run, args.single_clip, args.keep_remainder,
                            shared_watermark=shared_wm,
                            shared_intro_end=shared_intro_end,
                            shared_outro_len=shared_outro_len,
                            intro_override=None if shared_intro_end is not None else args.intro_sec,
                            outro_override=per_video_outro_override,
                            watermark_box_spec=args.watermark_box,
                            watermark_box_pct_spec=args.watermark_box_pct,
                            debug_preview=args.debug_preview, debug_detect=args.debug_detect)


if __name__ == "__main__":
    main()
