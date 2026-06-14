# clipper

A small command line video editor that trims and stitches numbered video files
using ffmpeg with automatic GPU encoder detection.

Drop 1.mp4, 2.mkv, 3.mp4 into a folder, run clipper.py, tell it which
slice of each clip to keep, and it produces a single output.mp4 (or matching
extension) in the same folder.

---

## Features

- **Auto discovers numbered files.** Scans the current directory for 1.<ext>,
  2.<ext> sorting them ascending, and warns about gaps.
- **Per clip trim ranges** through a simple interactive prompt.
- **Hardware encoder auto selection** with a real smoke test (not just feature
  detection):
  - NVIDIA NVENC
  - Intel Quick Sync (QSV)
  - AMD AMF on Windows / VAAPI on Linux
  - libx264 (CPU) fallback
- **Resolution selector**: 1080p, 1440p, 4K, or keep original.
- **Preserves source bitrate** by probing each input with ffprobe and matching
  the output bitrate.
- **Cross platform compatibily**: Linux x86_64, Linux aarch64, Windows x86_64, Windows
  arm64.
- **Single file**, no pip install, no requirements.txt.

---

## Requirements

- Python 3.9+
- ffmpeg and ffprobe available on your PATH

### Installing ffmpeg

**Windows**

```powershell
winget install Gyan.FFmpeg
```

Or download a build from [ffmpeg.org](https://ffmpeg.org/download.html) and add
its bin/ folder to your PATH.

**Linux**

```
# Debian / Ubuntu
sudo apt install ffmpeg

# Fedora
sudo dnf install ffmpeg

# Arch
sudo pacman -S ffmpeg
```

Verify it works:

```
ffmpeg -version
ffprobe -version
```

### GPU drivers (optional)

You don't need a GPU, the tool falls back to CPU encoding. If you want
hardware acceleration:

| GPU             | What you need                                                                |
| --------------- | ---------------------------------------------------------------------------- |
| NVIDIA          | Standard NVIDIA driver. Most ffmpeg builds include NVENC.                    |
| AMD (Windows)   | AMD driver (AMF ships with it). Use a recent ffmpeg build with h264_amf.     |
| AMD (Linux)     | mesa-va-drivers package; /dev/dri/renderD128 must exist.                     |
| Intel (any OS)  | Intel Media Driver (intel-media-va-driver on Debian/Ubuntu).                 |

The tool runs a 1 frame test encode through each candidate before picking it,
so misconfigured GPUs get skipped automatically, you'll never wait 5 minutes
to find out NVENC didn't actually work :3

---

## Installation

No installer. Grab the file

``` make it executable
chmod +x clipper.py
```

---

## Usage

1. Put your video files into one folder, named with ascending numbers:

   ```
   myproject/
     1.mp4
     2.mkv
     3.mp4
   ```

2. From inside that folder, run:

   ```
   python3 clipper.py
   ```

   (Or python clipper.py on Windows.)

3. Answer the prompts.

4. The result is output.mp4 (or .mkv if all inputs were .mkv) in the
   same folder.

### File naming rules

- Filename stem must be a positive numbers! 1.mp4, 2.mkv, 42.mov.
- 01.mp4, clip1.mp4, Clip 1.mp4 wont be detected!
- Supported extensions: .mp4, .mkv, .mov, .avi, .webm, .m4v,
  .flv, .ts, .mpg, .mpeg, .wmv.

---

## Time syntax

The trim range for each clip uses this format:

```
HH:MM:SS*HH:MM:SS
```

- Two timestamps separated by a single "*".
- Each timestamp is hours:minutes:seconds. Hours can be 1 or 2 digits.
- The second timestamp must be **strictly later** than the first.
- The second timestamp must not exceed the clip's duration.

Examples:

| Range                   | Meaning                                          |
| ----------------------- | ------------------------------------------------ |
| 00:00:10*00:01:30       | Keep from 0:10 to 1:30 (80 seconds total)        |
| 00:05:00*00:05:45       | Keep a 45 second slice starting at 5:00          |
| 01:00:00*01:30:00       | Keep 30 minutes starting at 1 hour in            |
| 0:00:00*0:00:30         | Keep the first 30 seconds                        |

If you mistype, the tool reprompts; it doesnt crash or guess.

---

## What youll see when it runs

Run from a folder with 1.mp4 (15s) and 2.mp4 (10s):

```
Working directory: /home/motan/obs

Found 2 file(s):
  1: 1.mp4
  2: 2.mp4

Probing…
  1.mp4: 1920x1080  h264  00:00:15  4500kbps  30.00fps
  2.mp4: 1920x1080  h264  00:00:10  4500kbps  30.00fps

Output resolution:
  1) 1080p
  2) 1440p
  3) 4K
  4) Keep original
Choice [1-4]: 4

Detecting encoder…
  using: NVIDIA NVENC  (h264_nvenc)

Enter the trim range for each clip:

[1.mp4]  duration = 00:00:15
  Range (HH:MM:SS*HH:MM:SS): 00:00:02*00:00:08

[2.mp4]  duration = 00:00:10
  Range (HH:MM:SS*HH:MM:SS): 00:00:01*00:00:05

=== Summary ===
  resolution: 1920x1080  (original)
  encoder:    NVIDIA NVENC
  output:     output.mp4
    1.mp4: 00:00:02 → 00:00:08  (00:00:06)
    2.mp4: 00:00:01 → 00:00:05  (00:00:04)
  total:      00:00:10

Proceed? [y/N]: y

[1/2] trimming 1.mp4 → 00:00:06
[ffmpeg output]
[2/2] trimming 2.mp4 → 00:00:04
[ffmpeg output]

concatenating → output.mp4
[ffmpeg output]

✓ done: /home/motan/obs/output.mp4
```

### Prompt reference

| Stage                | What it asks                                          | Valid input                          |
| -------------------- | ----------------------------------------------------- | ------------------------------------ |
| Resolution           | Choice [1-4]:                                         | 1, 2, 3, or 4                        |
| Trim range (per file)| Range (HH:MM:SS*HH:MM:SS):                            | A range, e.g. 00:00:05*00:01:00      |
| Confirm              | Proceed? [y/N]:                                       | y to continue, anything else aborts  |

---

## How encoder selection works

On startup the tool:

1. Asks ffmpeg which encoders are compiled in ffmpeg -encoders.
2. Tries candidates in this order: NVENC -> QSV -> AMF (Windows) /
   VAAPI (Linux) -> libx264.
3. For each candidate, runs a 1frame test encode. The first to succeed wins.

This catches the common "ffmpeg has the encoder built in but the driver isnt
actually working" case, which would otherwise fail several minutes into your
encode.

If every GPU candidate fails, it silently uses libx264. Your encode will still
work just slower.

---

## Output

- **Filename**: output.<ext> in the current working directory.
- **Extension**: matches the input extension if all inputs share one. Mixed
  inputs (e.g. .mp4 + .mkv) -> .mp4.
- **Resolution**: whatever you picked. With "original" + mixed resolution
  inputs, the first clips resolution is used (concat needs uniform size).
- **Video bitrate**: matches each sources bitrate.
- **Frame rate**: preserved per source.
- **Audio**: reencoded to AAC at source bitrate. Stream copy isnt safe across
  mid-frame trim points, so the tool re-encodes for accuracy.

---

## Troubleshooting

**ffmpeg and ffprobe must be installed and on PATH**
Install ffmpeg. On Windows, confirm the
folder containing ffmpeg.exe is in your PATH environment variable. Open a
new shell after editing PATH.

**No numbered video files found**
Files must literally be 1.mp4, 2.mkv, etc. Not clip1.mp4. Not 01.mp4.
Not 1 - intro.mp4. Rename them first.

**gap warning: missing [3]**
You have e.g. 1.mp4, 2.mp4, 4.mp4 with no 3.mp4. The tool continues
with what it finds. Ignore the warning if intentional.

**Encoder picks libx264 even though I have a GPU**
The smoke test failed. Reproduce manually to see the actual error:

```
ffmpeg -hide_banner -f lavfi -i color=c=black:s=128x128:d=0.1 \
       -frames:v 1 -c:v h264_nvenc -f null -
```

Replace h264_nvenc with whichever encoder you expected
(h264_qsv, h264_amf, h264_vaapi).

Common causes:
- GPU drivers not installed or out of date.
- Your ffmpeg build doesnt include the encoder (verify with
  ffmpeg -encoders | grep <name>).
- On Linux: user not in the video and render groups
  (sudo usermod -aG video,render $USER, then re-login).

**Nonmonotonic DT`** during concat
Usually a harmless warning, the output still plays. If playback actually
breaks, open an issue with the source file's ffprobe output.

**Encode is slow on Windows ARM**
Windows arm64 has no useful hardware encoder available, so it falls back to
libx264. That's expected. A future version may add Apple Silicon
VideoToolbox; AMF/NVENC on Windows ARM aren't there yet.

---

## What it doesnt do (yet)

- macOS / VideoToolbox encoding
- HEVC output (currently h264 only)
- Custom clip ordering beyond filename order
- Filters, fades, transitions
- Resume on partial failure

---

## License

 do whatever you want, no warranty.
