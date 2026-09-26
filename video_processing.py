"""
video_processing.py

Core ffmpeg/ffprobe logic: remove silence from the "lower" video, then
speed up the "upper" video so its duration matches. No GUI dependency —
usable from a web request handler, a CLI, or tests.
"""

import json
import os
import re
import subprocess
from pathlib import Path

# Cap ffmpeg's own thread count. On shared-vCPU containers (Railway's
# free/trial plan reports the *host's* full CPU count to the process),
# libx264 will otherwise spin up dozens of threads and can get OOM-killed
# well before it's CPU-bound. Override with the FFMPEG_THREADS env var.
FFMPEG_THREADS = os.environ.get("FFMPEG_THREADS", "2")


def run(cmd, capture=True):
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        stderr_tail = result.stderr[-4000:] if result.stderr else ""
        # returncode 137 (128+9) or a negative code (-9) means the process
        # was killed by SIGKILL, almost always the OS's OOM killer on a
        # memory-constrained host — that's a clearer diagnosis than
        # whatever partial ffmpeg output happened to be captured.
        if result.returncode in (137, -9):
            raise RuntimeError(
                "ffmpeg was killed, most likely because the server ran out "
                "of memory. Try a shorter or lower-resolution video, or "
                "upgrade the hosting plan for more RAM."
            )
        raise RuntimeError(f"Command failed ({' '.join(cmd)}):\n{stderr_tail}")
    return result


def ffprobe_json(path):
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    return json.loads(run(cmd).stdout)


def get_duration(path):
    return float(ffprobe_json(path)["format"]["duration"])


def _parse_rate(rate_str):
    if not rate_str or rate_str == "0/0":
        return None
    num, _, den = rate_str.partition("/")
    try:
        num, den = float(num), float(den)
        return num / den if den else None
    except ValueError:
        return None


def get_stream_info(path):
    info = ffprobe_json(path)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    fmt = info["format"]
    return {
        "duration": float(fmt["duration"]),
        "v_codec": v["codec_name"] if v else None,
        "width": v.get("width") if v else None,
        "height": v.get("height") if v else None,
        "fps": _parse_rate(v.get("avg_frame_rate")) if v else None,
        "v_bitrate": int(v["bit_rate"]) if v and v.get("bit_rate") else None,
        "pix_fmt": v.get("pix_fmt") if v else None,
        "a_codec": a["codec_name"] if a else None,
        "a_bitrate": int(a["bit_rate"]) if a and a.get("bit_rate") else None,
        "a_sample_rate": int(a["sample_rate"]) if a and a.get("sample_rate") else None,
        "a_channels": a.get("channels") if a else None,
    }


def encode_args_for(info, crf_override=None):
    # "veryfast" trades a little compression efficiency for much lower
    # memory/CPU use, which matters on a resource-capped host. Bump to
    # "medium"/"slow" if you're running with more RAM (e.g. Hobby+ plan
    # or locally).
    preset = os.environ.get("FFMPEG_PRESET", "veryfast")
    v_args = ["-c:v", "libx264", "-pix_fmt", info.get("pix_fmt") or "yuv420p",
              "-threads", FFMPEG_THREADS]
    if crf_override is not None:
        v_args += ["-crf", str(crf_override), "-preset", preset]
    elif info.get("v_bitrate"):
        kbps = max(int(info["v_bitrate"] / 1000), 300)
        v_args += ["-b:v", f"{kbps}k", "-maxrate", f"{int(kbps*1.5)}k",
                   "-bufsize", f"{kbps*2}k", "-preset", preset]
    else:
        v_args += ["-crf", "18", "-preset", preset]

    a_args = ["-c:a", "aac"]
    a_bitrate = info.get("a_bitrate")
    kbps = max(int(a_bitrate / 1000), 96) if a_bitrate else 192
    a_args += ["-b:a", f"{kbps}k"]
    if info.get("a_sample_rate"):
        a_args += ["-ar", str(info["a_sample_rate"])]
    if info.get("a_channels"):
        a_args += ["-ac", str(info["a_channels"])]
    return v_args, a_args


SILENCE_START_RE = re.compile(r"silence_start:\s*([0-9.]+)")
SILENCE_END_RE = re.compile(r"silence_end:\s*([0-9.]+)")


def detect_silence(path, noise_db=-30, min_silence=0.5):
    cmd = ["ffmpeg", "-i", str(path),
           "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
           "-f", "null", "-"]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding="utf-8", errors="replace")
    log = result.stderr
    starts = [float(m.group(1)) for m in SILENCE_START_RE.finditer(log)]
    ends = [float(m.group(1)) for m in SILENCE_END_RE.finditer(log)]
    if len(starts) > len(ends):
        ends.append(get_duration(path))
    return list(zip(starts, ends))


def compute_keep_segments(duration, silences, padding=0.08):
    padded = []
    for s, e in silences:
        s2 = min(s + padding, e)
        e2 = max(e - padding, s2)
        if e2 > s2:
            padded.append((s2, e2))

    keep = []
    cursor = 0.0
    for s, e in padded:
        if s > cursor:
            keep.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < duration:
        keep.append((cursor, duration))
    return [(s, e) for s, e in keep if e - s > 0.02]


def build_silence_removed_video(path, keep_segments, out_path, info, crf_override, log):
    if not keep_segments:
        raise RuntimeError("No non-silent segments found — try a different silence threshold.")

    filter_parts, v_labels, a_labels = [], [], []
    for i, (s, e) in enumerate(keep_segments):
        filter_parts.append(f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{i}]")
        filter_parts.append(f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}]")
        v_labels.append(f"[v{i}]")
        a_labels.append(f"[a{i}]")
    concat_inputs = "".join(f"{v}{a}" for v, a in zip(v_labels, a_labels))
    filter_parts.append(f"{concat_inputs}concat=n={len(keep_segments)}:v=1:a=1[vout][aout]")
    filter_complex = ";".join(filter_parts)

    v_args, a_args = encode_args_for(info, crf_override)
    cmd = ["ffmpeg", "-y", "-hwaccel", "none", "-i", str(path),
           "-filter_complex", filter_complex,
           "-map", "[vout]", "-map", "[aout]",
           *v_args, *a_args,
           "-force_key_frames", "expr:eq(n,0)",
           "-vsync", "cfr", "-fflags", "+genpts",
           "-avoid_negative_ts", "make_zero",
           "-movflags", "+faststart", str(out_path)]
    log(f"Removing silence: {len(keep_segments)} segment(s) kept...")
    run(cmd, capture=False)


def build_speed_adjusted_video(path, speed_factor, out_path, info, crf_override, log):
    v_filter = f"setpts=PTS/{speed_factor:.10f}"

    remaining = speed_factor
    chain = []
    while remaining > 2.0:
        chain.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        chain.append("atempo=0.5")
        remaining /= 0.5
    chain.append(f"atempo={remaining:.10f}")
    a_filter = ",".join(chain)

    v_args, a_args = encode_args_for(info, crf_override)
    cmd = ["ffmpeg", "-y", "-hwaccel", "none", "-i", str(path),
           "-filter:v", v_filter, "-filter:a", a_filter,
           *v_args, *a_args,
           "-force_key_frames", "expr:eq(n,0)",
           "-vsync", "cfr", "-fflags", "+genpts",
           "-avoid_negative_ts", "make_zero",
           "-movflags", "+faststart", str(out_path)]
    log(f"Speeding up upper video at {speed_factor:.4f}x...")
    run(cmd, capture=False)


def process_videos(upper_path, lower_path, outdir, noise_db=-30, min_silence=0.5,
                    padding=0.08, crf=None, log=print):
    """
    Runs the full pipeline and returns a dict with output paths and stats.
    """
    upper_path, lower_path, outdir = Path(upper_path), Path(lower_path), Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    lower_out = outdir / f"{lower_path.stem}_silence_removed.mp4"
    upper_out = outdir / f"{upper_path.stem}_speed_matched.mp4"

    log("Probing source files...")
    lower_info = get_stream_info(lower_path)
    upper_info = get_stream_info(upper_path)
    upper_duration = upper_info["duration"]
    lower_duration = lower_info["duration"]

    log("Detecting silence in lower video...")
    silences = detect_silence(lower_path, noise_db, min_silence)
    keep_segments = compute_keep_segments(lower_duration, silences, padding)

    build_silence_removed_video(lower_path, keep_segments, lower_out, lower_info, crf, log)
    lower_final_duration = get_duration(lower_out)

    if lower_final_duration <= 0:
        raise RuntimeError("Resulting lower video has zero duration.")

    speed_factor = upper_duration / lower_final_duration
    build_speed_adjusted_video(upper_path, speed_factor, upper_out, upper_info, crf, log)
    upper_final_duration = get_duration(upper_out)

    return {
        "lower_out": str(lower_out),
        "upper_out": str(upper_out),
        "upper_original_duration": upper_duration,
        "lower_original_duration": lower_duration,
        "lower_final_duration": lower_final_duration,
        "upper_final_duration": upper_final_duration,
        "speed_factor": speed_factor,
        "silent_segments_found": len(silences),
        "segments_kept": len(keep_segments),
    }
