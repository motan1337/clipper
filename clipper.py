#!/usr/bin/env python3
"""
clipper.py  minimal cli video editor on top of ffmpeg.

Workflow:
  1. Scans cwd for numbered video files (1.mp4, 2.mkv, etc) and sorts ascending.
  2. Probes each with ffprobe.
  3. Asks for output resolution (1080p / 1440p / 4K / original).
  4. Auto detects best available encoder (NVENC > QSV > AMF/VAAPI > libx264).
  5. Asks for a trim range per clip in HH:MM:SS*HH:MM:SS form.
  6. Trims each to a temp file, then concat-copies them to output.<ext>.

Targets Linux x86_64, Linux aarch64, Windows x86_64, Windows arm64.
Requires ffmpeg + ffprobe on PATH.
"""

from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

# tool discovery

def find_tool(name: str) -> Optional[str]:
    """shutil.which handles .exe on Windows via PATHEXT, but be explicit."""
    p = shutil.which(name)
    if p:
        return p
    if platform.system() == "Windows":
        return shutil.which(name + ".exe")
    return None


FFMPEG = find_tool("ffmpeg")
FFPROBE = find_tool("ffprobe")

if not FFMPEG or not FFPROBE:
    print("ERROR: ffmpeg and ffprobe must be installed and on PATH.", file=sys.stderr)
    print("  Linux:   apt/dnf/pacman install ffmpeg", file=sys.stderr)
    print("  Windows: winget install ffmpeg   (or download from ffmpeg.org)", file=sys.stderr)
    sys.exit(1)


# time parsing and formatting

_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2}):(\d{2})\s*$")
_RANGE_RE = re.compile(r"^\s*(\d{1,2}:\d{2}:\d{2})\s*\*\s*(\d{1,2}:\d{2}:\d{2})\s*$")


def parse_time(s: str) -> Optional[int]:
    m = _TIME_RE.match(s)
    if not m:
        return None
    h, mi, se = (int(x) for x in m.groups())
    if mi >= 60 or se >= 60:
        return None
    return h * 3600 + mi * 60 + se


def parse_range(s: str) -> Optional[tuple[int, int]]:
    m = _RANGE_RE.match(s)
    if not m:
        return None
    a, b = parse_time(m.group(1)), parse_time(m.group(2))
    if a is None or b is None or b <= a:
        return None
    return a, b


def fmt_time(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


# probing

def probe(path: Path) -> Optional[dict]:
    """Return basic info about the first video stream + container, or None."""
    cmd = [
        FFPROBE, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError:
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None

    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if video is None:
        return None

    duration = float(data.get("format", {}).get("duration", 0) or 0)

    # bitrate
    vbitrate = None
    if video.get("bit_rate"):
        vbitrate = int(video["bit_rate"])
    elif data.get("format", {}).get("bit_rate"):
        total = int(data["format"]["bit_rate"])
        abr = int(audio["bit_rate"]) if audio and audio.get("bit_rate") else 128_000
        vbitrate = max(total - abr, total // 2)

    fps = 30.0
    if video.get("avg_frame_rate") and "/" in video["avg_frame_rate"]:
        num, den = video["avg_frame_rate"].split("/", 1)
        try:
            n, d = float(num), float(den)
            if d:
                fps = n / d
        except ValueError:
            pass

    return {
        "duration": duration,
        "width": int(video["width"]),
        "height": int(video["height"]),
        "vcodec": video.get("codec_name", "unknown"),
        "vbitrate": vbitrate,
        "acodec": audio.get("codec_name") if audio else None,
        "abitrate": int(audio["bit_rate"]) if audio and audio.get("bit_rate") else None,
        "fps": fps,
    }


# encoder

def _ffmpeg_has_encoder(name: str) -> bool:
    try:
        out = subprocess.check_output(
            [FFMPEG, "-hide_banner", "-encoders"],
            text=True, stderr=subprocess.STDOUT, timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return any(line.split()[1:2] == [name] for line in out.splitlines() if line.startswith(" V"))


def _smoke_test(extra_in: list[str], encoder: str, vf: Optional[str] = None) -> bool:
    """Try to encode a single black frame with the given encoder."""
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error"]
    cmd += extra_in
    cmd += ["-f", "lavfi", "-i", "color=c=black:s=128x128:d=0.1"]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-frames:v", "1", "-c:v", encoder, "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=15)
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def detect_encoder() -> tuple[str, list[str], str]:
    """
    Return (encoder, hwaccel_args_for_decode, friendly_name).
    hwaccel_args_for_decode go BEFORE -i in the trim command (VAAPI needs this).
    """
    sysname = platform.system()
    candidates: list[tuple[str, list[str], Optional[str], str]] = []

    if _ffmpeg_has_encoder("h264_nvenc"):
        candidates.append(("h264_nvenc", [], None, "NVIDIA NVENC"))
    if _ffmpeg_has_encoder("h264_qsv"):
        candidates.append(("h264_qsv", [], None, "Intel QSV"))
    if sysname == "Windows" and _ffmpeg_has_encoder("h264_amf"):
        candidates.append(("h264_amf", [], None, "AMD AMF"))
    if sysname == "Linux" and _ffmpeg_has_encoder("h264_vaapi"):
        if Path("/dev/dri/renderD128").exists():
            candidates.append((
                "h264_vaapi",
                ["-vaapi_device", "/dev/dri/renderD128"],
                "format=nv12,hwupload",
                "VAAPI (AMD/Intel on Linux)",
            ))

    for enc, hw, vf, label in candidates:
        if _smoke_test(hw, enc, vf):
            return enc, hw, label

    return "libx264", [], "CPU (libx264)"


# files

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".flv", ".ts", ".mpg", ".mpeg", ".wmv"}


def find_numbered_files(directory: Path) -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    for p in directory.iterdir():
        if not p.is_file() or p.suffix.lower() not in VIDEO_EXTS:
            continue
        try:
            n = int(p.stem)
        except ValueError:
            continue
        if n > 0:
            found.append((n, p))
    found.sort(key=lambda x: x[0])
    return found


# asking

_RES_OPTIONS = {
    "1": ("1080p", 1920, 1080),
    "2": ("1440p", 2560, 1440),
    "3": ("4K",    3840, 2160),
    "4": ("original", None, None),
}


def prompt_resolution() -> tuple[str, Optional[int], Optional[int]]:
    print("\nOutput resolution:")
    print("  1) 1080p")
    print("  2) 1440p")
    print("  3) 4K")
    print("  4) Keep original")
    while True:
        choice = input("Choice [1-4]: ").strip()
        if choice in _RES_OPTIONS:
            return _RES_OPTIONS[choice]
        print("  Invalid. Pick 1, 2, 3 or 4.")


def prompt_clip_range(path: Path, duration: float) -> tuple[int, int]:
    print(f"\n[{path.name}]  duration = {fmt_time(duration)}")
    while True:
        s = input("  Range (HH:MM:SS*HH:MM:SS): ").strip()
        rng = parse_range(s)
        if rng is None:
            print("  Bad format. Example: 00:00:10*00:01:30  (end must be > start)")
            continue
        start, end = rng
        if end > duration + 0.5:
            print(f"  End {fmt_time(end)} is past clip end {fmt_time(duration)}.")
            continue
        return start, end


# ffmpeg

def build_scale_filter(encoder: str, src_w: int, src_h: int, tgt_w: int, tgt_h: int) -> Optional[str]:
    """Produce a -vf string. None means no filter needed."""
    needs_resize = (tgt_w, tgt_h) != (src_w, src_h)

    if encoder == "h264_vaapi":
        if needs_resize:
            return (
                f"format=nv12,hwupload,"
                f"scale_vaapi=w={tgt_w}:h={tgt_h}:force_original_aspect_ratio=decrease"
            )
        return "format=nv12,hwupload"

    if not needs_resize:
        return None

    return (
        f"scale={tgt_w}:{tgt_h}:force_original_aspect_ratio=decrease,"
        f"pad={tgt_w}:{tgt_h}:(ow-iw)/2:(oh-ih)/2:color=black"
    )


def build_trim_cmd(
    src: Path,
    dst: Path,
    start: int,
    end: int,
    tgt_w: int,
    tgt_h: int,
    encoder: str,
    hwaccel_pre: list[str],
    info: dict,
) -> list[str]:
    duration = end - start
    cmd: list[str] = [FFMPEG, "-hide_banner", "-y"]

    cmd += hwaccel_pre

    cmd += ["-ss", str(start), "-i", str(src), "-t", str(duration)]

    vf = build_scale_filter(encoder, info["width"], info["height"], tgt_w, tgt_h)
    if vf:
        cmd += ["-vf", vf]

    cmd += ["-c:v", encoder]

    if info.get("vbitrate"):
        vb = info["vbitrate"]
        cmd += ["-b:v", str(vb), "-maxrate", str(int(vb * 1.5)), "-bufsize", str(vb * 2)]
    elif encoder == "libx264":
        cmd += ["-crf", "20", "-preset", "medium"]

    cmd += ["-r", f"{info['fps']:.5f}"]

    # audio
    if info.get("acodec"):
        ab = info.get("abitrate") or 192_000
        cmd += ["-c:a", "aac", "-b:a", str(ab)]
    else:
        cmd += ["-an"]

    if dst.suffix.lower() == ".mp4":
        cmd += ["-movflags", "+faststart"]

    cmd += [str(dst)]
    return cmd


def concat_copy(parts: list[Path], output: Path) -> bool:
    """Concat with stream copy works because all parts share codec/res/fps."""
    list_file = output.parent / "_clipper_concat.txt"
    try:
        with open(list_file, "w", encoding="utf-8") as f:
            for p in parts:
                safe = str(p.resolve()).replace("\\", "/").replace("'", "'\\''")
                f.write(f"file '{safe}'\n")
        cmd = [
            FFMPEG, "-hide_banner", "-y",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c", "copy", str(output),
        ]
        return subprocess.run(cmd).returncode == 0
    finally:
        try:
            list_file.unlink()
        except OSError:
            pass


# main

def main() -> int:
    cwd = Path.cwd()
    print(f"Working directory: {cwd}")

    files = find_numbered_files(cwd)
    if not files:
        print("No numbered video files (1.mp4, 2.mkv, …) found in this directory.")
        return 1

    nums = [n for n, _ in files]
    missing = [i for i in range(1, nums[-1] + 1) if i not in nums]
    if missing:
        print(f"  ! gap warning: missing {missing}")

    print(f"\nFound {len(files)} file(s):")
    for n, p in files:
        print(f"  {n}: {p.name}")

    # probe
    print("\nProbing…")
    probed: list[tuple[int, Path, dict]] = []
    for n, p in files:
        info = probe(p)
        if info is None:
            print(f"  ERROR: could not probe {p.name}")
            return 1
        probed.append((n, p, info))
        vb = f"{info['vbitrate']/1000:.0f}kbps" if info["vbitrate"] else "?"
        print(f"  {p.name}: {info['width']}x{info['height']}  {info['vcodec']}  "
              f"{fmt_time(info['duration'])}  {vb}  {info['fps']:.2f}fps")

    # resolution
    res_label, tgt_w, tgt_h = prompt_resolution()
    if tgt_w is None:
        first = probed[0][2]
        tgt_w, tgt_h = first["width"], first["height"]
        if any(i["width"] != tgt_w or i["height"] != tgt_h for _, _, i in probed):
            print(f"  ! clips have mixed resolutions; using first clip's "
                  f"{tgt_w}x{tgt_h} for all parts.")

    # encoder
    print("\nDetecting encoder…")
    encoder, hwaccel_pre, encoder_label = detect_encoder()
    print(f"  using: {encoder_label}  ({encoder})")

    # trim
    print("\nEnter the trim range for each clip:")
    ranges: list[tuple[int, Path, dict, int, int]] = []
    for n, p, info in probed:
        start, end = prompt_clip_range(p, info["duration"])
        ranges.append((n, p, info, start, end))

    # output
    exts = {p.suffix.lower() for _, p, _, _, _ in ranges}
    out_ext = next(iter(exts)) if len(exts) == 1 else ".mp4"
    output = cwd / f"output{out_ext}"
    if output.exists():
        print(f"\n  ! {output.name} exists and will be overwritten.")

    # output 2
    print("\n=== Summary ===")
    print(f"  resolution: {tgt_w}x{tgt_h}  ({res_label})")
    print(f"  encoder:    {encoder_label}")
    print(f"  output:     {output.name}")
    total = 0
    for _, p, _, s, e in ranges:
        d = e - s
        total += d
        print(f"    {p.name}: {fmt_time(s)} → {fmt_time(e)}  ({fmt_time(d)})")
    print(f"  total:      {fmt_time(total)}")

    if input("\nProceed? [y/N]: ").strip().lower() != "y":
        print("Aborted.")
        return 0

    # Trim 2 concat
    tmp_dir = cwd / ".clipper_tmp"
    tmp_dir.mkdir(exist_ok=True)
    parts: list[Path] = []
    try:
        for i, (n, p, info, start, end) in enumerate(ranges, start=1):
            part_ext = ".mp4" if out_ext == ".mp4" else out_ext
            part = tmp_dir / f"part_{i:03d}{part_ext}"
            print(f"\n[{i}/{len(ranges)}] trimming {p.name} → {fmt_time(end - start)}")
            cmd = build_trim_cmd(p, part, start, end, tgt_w, tgt_h, encoder, hwaccel_pre, info)
            r = subprocess.run(cmd)
            if r.returncode != 0:
                print(f"  ERROR: ffmpeg failed on {p.name}")
                return 1
            parts.append(part)

        if len(parts) == 1:
            shutil.move(str(parts[0]), str(output))
            parts.clear()
        else:
            print(f"\nconcatenating → {output.name}")
            if not concat_copy(parts, output):
                print("ERROR: concat step failed.")
                return 1

        print(f"\n✓ done: {output}")
        return 0
    finally:
        for part in parts:
            try:
                part.unlink()
            except OSError:
                pass
        try:
            tmp_dir.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted.")
        sys.exit(130)