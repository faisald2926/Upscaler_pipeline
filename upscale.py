#!/usr/bin/env python3
"""
Anime 4K Upscale Pipeline
=========================
1080p → 4K archival upscale using Real-ESRGAN ncnn Vulkan.

Usage:
    python upscale.py verify          # Run quality verifier only
    python upscale.py pipeline        # Run full pipeline (verifier first)
    python upscale.py pipeline --skip-verify  # Skip verifier
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import glob
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich.text import Text

console = Console()

# ─── Config ───────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
WORK_DIR = SCRIPT_DIR / "work"
TOOLS_DIR = SCRIPT_DIR / "tools" / "realesrgan"
REALESRGAN_EXE = TOOLS_DIR / "realesrgan-ncnn-vulkan.exe"
MODELS_DIR = TOOLS_DIR / "models"

MIN_FREE_SPACE_GB = 100  # Abort if less than this

# ─── Release group lists (exact match only) ──────────────────────────────────
GROUPS_PREMIUM = {
    "beatrice-raws", "ctrlhd", "ai-raws", "coalgirls", "kawaiika", "tenshi"
}
GROUPS_WARNING = {
    "erai-raws", "subsplease", "horriblesubs"
}
GROUPS_BAD = {
    "yify", "yts", "rarbg", "fgt", "yg"
}


# ═══════════════════════════════════════════════════════════════════════════════
# UTILITY
# ═══════════════════════════════════════════════════════════════════════════════

def find_mkv() -> Path:
    """Find the first .mkv in SCRIPT_DIR."""
    mkvs = list(SCRIPT_DIR.glob("*.mkv"))
    # Exclude our output files
    mkvs = [m for m in mkvs if not m.name.endswith(".4K.upscaled.mkv")]
    if not mkvs:
        console.print("[red]No .mkv file found in script directory.[/]")
        sys.exit(1)
    if len(mkvs) > 1:
        console.print(f"[yellow]Multiple .mkv files found, using: {mkvs[0].name}[/]")
    return mkvs[0]


def run_json_cmd(cmd: list[str]) -> dict:
    """Run a command and parse JSON output."""
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        console.print(f"[red]Command failed:[/] {' '.join(cmd)}")
        console.print(r.stderr[-500:] if r.stderr else "(no stderr)")
        sys.exit(1)
    return json.loads(r.stdout)


def check_tool(name: str) -> str:
    """Check if a tool is on PATH or in known locations. Returns the path."""
    # Special case for realesrgan
    if name == "realesrgan-ncnn-vulkan":
        if REALESRGAN_EXE.exists():
            return str(REALESRGAN_EXE)
        # Try PATH
        r = shutil.which("realesrgan-ncnn-vulkan") or shutil.which("realesrgan-ncnn-vulkan.exe")
        if r:
            return r
        return ""

    # For tools that may need a refreshed PATH after winget install
    r = shutil.which(name)
    if r:
        return r

    # Try refreshing PATH from registry (Windows)
    try:
        import winreg
        machine_path = ""
        user_path = ""
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment") as key:
                machine_path = winreg.QueryValueEx(key, "Path")[0]
        except Exception:
            pass
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
                user_path = winreg.QueryValueEx(key, "Path")[0]
        except Exception:
            pass
        full_path = machine_path + ";" + user_path
        for d in full_path.split(";"):
            d = d.strip()
            if not d:
                continue
            candidate = Path(d) / (name + ".exe")
            if candidate.exists():
                return str(candidate)
            candidate = Path(d) / name
            if candidate.exists():
                return str(candidate)
    except ImportError:
        pass

    return ""


def get_free_space_gb(path: Path) -> float:
    """Get free space in GB for the drive containing path."""
    st = shutil.disk_usage(path)
    return st.free / (1024 ** 3)


def stage_done(stage: str) -> bool:
    """Check if a stage marker exists."""
    return (WORK_DIR / f".{stage}.done").exists()


def mark_done(stage: str):
    """Create a stage marker."""
    (WORK_DIR / f".{stage}.done").touch()


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 1: VERIFIER
# ═══════════════════════════════════════════════════════════════════════════════

def extract_release_group(filename: str) -> str:
    """Extract release group from filename using common patterns."""
    name = filename.rsplit(".", 1)[0]  # Remove extension

    # Pattern: [Group] Title or text
    m = re.match(r"^\[([^\]]+)\]", name)
    if m:
        return m.group(1).strip()

    # Pattern: Title - stuff - Group  or  Title.stuff-Group
    m = re.search(r"-\s*([A-Za-z][\w-]*)\s*$", name)
    if m:
        return m.group(1).strip()

    return ""


def detect_source_type(filename: str) -> tuple[str, int]:
    """Detect source type from filename. Returns (type, score_delta)."""
    fn_lower = filename.lower()
    if "remux" in fn_lower:
        return "REMUX", 30
    if "bluray" in fn_lower or "blu-ray" in fn_lower or "bdremux" in fn_lower:
        return "BluRay", 20
    if "web-dl" in fn_lower or "webdl" in fn_lower:
        return "WEB-DL", -5
    if "webrip" in fn_lower:
        return "WEBRip", -5
    if "hdtv" in fn_lower:
        return "HDTV", -20
    if "dvdrip" in fn_lower or "dvd" in fn_lower:
        return "DVD", -20
    if "cam" in fn_lower or "camrip" in fn_lower:
        return "CAM", -50
    if "screener" in fn_lower or "scr" in fn_lower:
        return "SCREENER", -50
    return "Unknown", 0


def score_bitrate(bitrate_bps: float) -> tuple[str, int]:
    """Score based on bitrate."""
    mbps = bitrate_bps / 1_000_000
    if mbps >= 25:
        return f"{mbps:.1f} Mbps", 20
    if mbps >= 12:
        return f"{mbps:.1f} Mbps", 10
    if mbps >= 6:
        return f"{mbps:.1f} Mbps", 0
    if mbps >= 3:
        return f"{mbps:.1f} Mbps", -15
    return f"{mbps:.1f} Mbps", -30


def score_codec(codec_name: str) -> tuple[str, int]:
    """Score based on codec."""
    c = codec_name.lower()
    if c in ("hevc", "h265", "h.265", "av1"):
        return codec_name.upper(), 5
    return codec_name.upper(), 0


def detect_bit_depth(pix_fmt: str, profile: str) -> tuple[int, int]:
    """Detect bit depth from pix_fmt. Returns (bit_depth, score_delta)."""
    if pix_fmt and ("10le" in pix_fmt or "10be" in pix_fmt or "p10" in pix_fmt):
        return 10, 5
    if profile and "10" in profile.lower():
        return 10, 5
    return 8, 0


def score_framerate(fps_num: int, fps_den: int) -> tuple[str, int, list[str]]:
    """Evaluate framerate. Returns (fps_str, score_delta, warnings)."""
    warnings = []
    if fps_den == 0:
        return "unknown", 0, []
    fps = fps_num / fps_den
    fps_str = f"{fps:.3f}"
    if fps > 35:
        warnings.append(f"⚠ High framerate ({fps_str} fps) — likely interpolated, not native anime")
        return fps_str, -30, warnings
    return fps_str, 0, warnings


def score_group(group: str) -> tuple[str, int, list[str]]:
    """Score release group. Uses exact match only."""
    warnings = []
    g_lower = group.lower()

    if g_lower in GROUPS_PREMIUM:
        return group, 10, []
    if g_lower in GROUPS_WARNING:
        warnings.append(f"⚠ Group '{group}' is a streaming re-encode — may have compression artifacts")
        return group, 0, warnings
    if g_lower in GROUPS_BAD:
        return group, -10, []

    return group, 0, []


def run_verifier(mkv_path: Path) -> int:
    """Run full verification. Returns score."""
    console.print()
    console.print(Panel.fit(
        f"[bold cyan]Source Quality Verifier[/]\n{mkv_path.name}",
        border_style="cyan",
    ))
    console.print()

    # ── Gather data ──
    ffprobe_path = check_tool("ffprobe")
    mediainfo_path = check_tool("mediainfo")

    # ffprobe: video stream
    probe = run_json_cmd([
        ffprobe_path, "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", str(mkv_path)
    ])

    video_stream = None
    audio_streams = []
    sub_streams = []
    for s in probe.get("streams", []):
        if s.get("codec_type") == "video" and video_stream is None:
            video_stream = s
        elif s.get("codec_type") == "audio":
            audio_streams.append(s)
        elif s.get("codec_type") == "subtitle":
            sub_streams.append(s)

    if not video_stream:
        console.print("[red]No video stream found![/]")
        sys.exit(1)

    fmt = probe.get("format", {})

    # ── Extract metrics ──
    filename = mkv_path.name
    codec_name = video_stream.get("codec_name", "unknown")
    pix_fmt = video_stream.get("pix_fmt", "")
    profile = video_stream.get("profile", "")
    width = video_stream.get("width", 0)
    height = video_stream.get("height", 0)
    duration = float(fmt.get("duration", 0))

    # Bitrate: prefer video stream BPS tag, fall back to format bitrate
    video_bitrate = 0
    tags = video_stream.get("tags", {})
    if "BPS" in tags:
        video_bitrate = float(tags["BPS"])
    elif "BPS-eng" in tags:
        video_bitrate = float(tags["BPS-eng"])
    elif "bit_rate" in video_stream:
        video_bitrate = float(video_stream["bit_rate"])
    else:
        video_bitrate = float(fmt.get("bit_rate", 0))

    # Frame rate
    r_frame_rate = video_stream.get("r_frame_rate", "0/1")
    fps_parts = r_frame_rate.split("/")
    fps_num = int(fps_parts[0])
    fps_den = int(fps_parts[1]) if len(fps_parts) > 1 else 1

    # Frame count
    frame_count = int(tags.get("NUMBER_OF_FRAMES", tags.get("NUMBER_OF_FRAMES-eng", 0)))
    if not frame_count and duration > 0 and fps_den > 0:
        frame_count = int(duration * fps_num / fps_den)

    # Release group
    group = extract_release_group(filename)

    # ── Score ──
    score = 50  # Base score
    reasons = []
    warnings = []

    # Source type
    src_type, src_delta = detect_source_type(filename)
    score += src_delta
    if src_delta != 0:
        sign = "+" if src_delta > 0 else ""
        reasons.append(f"Source type: {src_type} ({sign}{src_delta})")

    # Bitrate
    br_str, br_delta = score_bitrate(video_bitrate)
    score += br_delta
    sign = "+" if br_delta > 0 else ""
    reasons.append(f"Video bitrate: {br_str} ({sign}{br_delta})")

    # Codec
    codec_str, codec_delta = score_codec(codec_name)
    score += codec_delta
    if codec_delta != 0:
        reasons.append(f"Codec: {codec_str} (+{codec_delta})")

    # Bit depth
    bit_depth, bd_delta = detect_bit_depth(pix_fmt, profile)
    score += bd_delta
    if bd_delta != 0:
        reasons.append(f"Bit depth: {bit_depth}-bit (+{bd_delta})")

    # Framerate
    fps_str, fps_delta, fps_warnings = score_framerate(fps_num, fps_den)
    score += fps_delta
    if fps_delta != 0:
        reasons.append(f"Framerate: {fps_str} fps ({fps_delta})")
    warnings.extend(fps_warnings)

    # Release group
    grp_str, grp_delta, grp_warnings = score_group(group)
    score += grp_delta
    if grp_delta != 0:
        sign = "+" if grp_delta > 0 else ""
        reasons.append(f"Release group: {grp_str} ({sign}{grp_delta})")
    warnings.extend(grp_warnings)

    # Clamp
    score = max(0, min(100, score))

    # ── Color ──
    if score >= 75:
        color = "green"
        verdict = "EXCELLENT — proceed with upscale"
    elif score >= 55:
        color = "green"
        verdict = "GOOD — proceed with upscale"
    elif score >= 35:
        color = "yellow"
        verdict = "MARGINAL — upscale may not improve quality significantly"
    else:
        color = "red"
        verdict = "POOR — upscale not recommended, source quality too low"

    # ── Technical specs table ──
    specs = Table(title="Technical Specifications", show_header=True,
                  header_style="bold cyan", border_style="dim")
    specs.add_column("Property", style="dim")
    specs.add_column("Value")

    specs.add_row("Filename", filename)
    specs.add_row("Container", fmt.get("format_name", "unknown"))
    specs.add_row("Duration", f"{duration/60:.1f} min ({duration:.1f}s)")
    specs.add_row("Resolution", f"{width}×{height}")
    specs.add_row("Codec", f"{codec_name} ({profile})" if profile else codec_name)
    specs.add_row("Pixel Format", pix_fmt)
    specs.add_row("Bit Depth", f"{bit_depth}-bit")
    specs.add_row("Video Bitrate", f"{video_bitrate/1_000_000:.2f} Mbps ({video_bitrate/1000:.0f} kbps)")
    specs.add_row("Total Bitrate", f"{float(fmt.get('bit_rate', 0))/1_000_000:.2f} Mbps")
    specs.add_row("Framerate", f"{fps_str} fps ({r_frame_rate})")
    specs.add_row("Frame Count", str(frame_count))
    specs.add_row("File Size", f"{int(fmt.get('size', 0)) / (1024**2):.1f} MB")
    specs.add_row("Audio Streams", str(len(audio_streams)))
    specs.add_row("Subtitle Streams", str(len(sub_streams)))
    specs.add_row("Release Group", group or "Unknown")
    specs.add_row("Source Type", src_type)

    console.print(specs)
    console.print()

    # ── Score breakdown ──
    score_table = Table(title="Score Breakdown", show_header=True,
                        header_style="bold cyan", border_style="dim")
    score_table.add_column("Factor", style="dim")
    score_table.add_column("Impact")

    score_table.add_row("Base score", "50")
    for r in reasons:
        score_table.add_row(*r.rsplit(" (", 1) if " (" in r else (r, ""))

    console.print(score_table)
    console.print()

    # ── Warnings ──
    if warnings:
        console.print("[bold yellow]⚠ Warnings:[/]")
        for w in warnings:
            console.print(f"  {w}")
        console.print()

    # ── Final score ──
    score_text = Text(f"  Score: {score}/100  ", style=f"bold white on {color}")
    console.print(score_text)
    console.print(f"  [{color}]{verdict}[/]")
    console.print()

    return score


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 2: PIPELINE
# ═══════════════════════════════════════════════════════════════════════════════

def make_progress() -> Progress:
    """Create a rich Progress bar with the required columns."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    )


def get_fps(video_path: Path) -> str:
    """Get r_frame_rate from a video file."""
    ffprobe_path = check_tool("ffprobe")
    r = subprocess.run(
        [ffprobe_path, "-v", "quiet", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0",
         str(video_path)],
        capture_output=True, text=True, encoding="utf-8"
    )
    return r.stdout.strip()


def get_frame_count(video_path: Path) -> int:
    """Get total frame count from ffprobe."""
    ffprobe_path = check_tool("ffprobe")
    data = run_json_cmd([
        ffprobe_path, "-v", "quiet", "-print_format", "json",
        "-show_streams", "-select_streams", "v:0", str(video_path)
    ])
    streams = data.get("streams", [])
    if streams:
        tags = streams[0].get("tags", {})
        count = tags.get("NUMBER_OF_FRAMES", tags.get("NUMBER_OF_FRAMES-eng", ""))
        if count:
            return int(count)
        # Fallback: nb_frames
        nb = streams[0].get("nb_frames", "")
        if nb and nb != "N/A":
            return int(nb)
    # Fallback: count with ffprobe
    r = subprocess.run(
        [ffprobe_path, "-v", "quiet", "-count_frames",
         "-select_streams", "v:0", "-show_entries", "stream=nb_read_frames",
         "-of", "csv=p=0", str(video_path)],
        capture_output=True, text=True, encoding="utf-8", timeout=600
    )
    val = r.stdout.strip()
    if val and val != "N/A":
        return int(val)
    return 0


def run_ffmpeg_with_progress(cmd: list[str], total: int, task_desc: str, progress: Progress):
    """Run ffmpeg with -progress pipe:1 and update progress bar."""
    import tempfile

    task = progress.add_task(task_desc, total=total)

    # Write stderr to a temp file to avoid pipe deadlock
    stderr_file = WORK_DIR / f".ffmpeg_stderr_{os.getpid()}.log"
    stderr_fh = open(stderr_file, "w", encoding="utf-8", errors="replace")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=stderr_fh,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    current_frame = 0

    # Read stdout for progress (ffmpeg -progress pipe:1 writes to stdout)
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("frame="):
            try:
                val = int(line.split("=", 1)[1].strip())
                delta = val - current_frame
                if delta > 0:
                    progress.update(task, advance=delta)
                    current_frame = val
            except (ValueError, IndexError):
                pass
        if line == "progress=end":
            break

    proc.wait(timeout=120)
    stderr_fh.close()

    if proc.returncode != 0:
        progress.update(task, completed=current_frame)
        console.print(f"\n[red]✗ FFmpeg failed during: {task_desc}[/]")
        console.print(f"[dim]Command: {' '.join(cmd)}[/]")
        console.print("[bold red]Last 20 lines of stderr:[/]")
        try:
            stderr_lines = stderr_file.read_text(encoding="utf-8", errors="replace").strip().split("\n")
            for line in stderr_lines[-20:]:
                console.print(f"  [red]{line}[/]")
        except Exception:
            console.print("  [red](could not read stderr log)[/]")
        stderr_file.unlink(missing_ok=True)
        sys.exit(1)

    stderr_file.unlink(missing_ok=True)
    # Ensure bar shows complete
    progress.update(task, completed=total)


def stage_demux(mkv_path: Path, progress: Progress):
    """Stage 1: Demux video, audio, subs."""
    if stage_done("demux"):
        console.print("[dim]  ↳ Stage 1 (DEMUX) already done, skipping.[/]")
        return

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    ffmpeg_path = check_tool("ffmpeg")

    task = progress.add_task("[1/5] DEMUX", total=3)

    # Video
    cmd_v = [ffmpeg_path, "-y", "-i", str(mkv_path),
             "-map", "0:v:0", "-c", "copy", str(WORK_DIR / "video.mkv")]
    r = subprocess.run(cmd_v, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        console.print(f"\n[red]✗ Video demux failed[/]")
        console.print(f"[dim]Command: {' '.join(cmd_v)}[/]")
        for line in r.stderr.strip().split("\n")[-20:]:
            console.print(f"  [red]{line}[/]")
        sys.exit(1)
    progress.update(task, advance=1)
    
    # We no longer extract audio/subs here; they are mapped directly from the original file during muxing.
    progress.update(task, completed=3)

    mark_done("demux")
    console.print("[green]  ✓ Stage 1 DEMUX complete[/]")


def stage_extract_frames(progress: Progress):
    """Stage 2: Extract frames as PNGs."""
    if stage_done("extract"):
        console.print("[dim]  ↳ Stage 2 (EXTRACT FRAMES) already done, skipping.[/]")
        return

    video_path = WORK_DIR / "video.mkv"
    frames_dir = WORK_DIR / "frames_in"
    frames_dir.mkdir(parents=True, exist_ok=True)

    # Check disk space
    free_gb = get_free_space_gb(WORK_DIR)
    console.print(f"[dim]  ↳ Free disk space: {free_gb:.1f} GB[/]")
    if free_gb < MIN_FREE_SPACE_GB:
        console.print(f"[red]✗ Insufficient disk space! Need {MIN_FREE_SPACE_GB} GB, have {free_gb:.1f} GB.[/]")
        console.print("[red]  PNG frames require ~80 GB scratch space for a 23-min episode.[/]")
        sys.exit(1)

    total_frames = get_frame_count(video_path)
    console.print(f"[dim]  ↳ Total frames to extract: {total_frames}[/]")

    ffmpeg_path = check_tool("ffmpeg")
    cmd = [
        ffmpeg_path, "-y", "-i", str(video_path),
        "-pix_fmt", "rgb24",
        "-progress", "pipe:1",
        str(frames_dir / "frame_%08d.png")
    ]

    run_ffmpeg_with_progress(cmd, total_frames, "[2/5] EXTRACT FRAMES", progress)

    # Verify frame count
    extracted = len(list(frames_dir.glob("*.png")))
    console.print(f"[dim]  ↳ Extracted {extracted} frames[/]")
    if extracted < total_frames * 0.95:
        console.print(f"[red]✗ Frame extraction incomplete: {extracted}/{total_frames}[/]")
        sys.exit(1)

    mark_done("extract")
    console.print("[green]  ✓ Stage 2 EXTRACT FRAMES complete[/]")


def stage_upscale(progress: Progress):
    """Stage 3: Upscale frames with Real-ESRGAN."""
    if stage_done("upscale"):
        console.print("[dim]  ↳ Stage 3 (UPSCALE) already done, skipping.[/]")
        return

    frames_in_dir = WORK_DIR / "frames_in"
    frames_out_dir = WORK_DIR / "frames_out"
    frames_out_dir.mkdir(parents=True, exist_ok=True)

    total_frames = len(list(frames_in_dir.glob("*.png")))
    console.print(f"[dim]  ↳ Frames to upscale: {total_frames}[/]")
    console.print("[dim]  ↳ Model: realesr-animevideov3 (x2), tile: 256, GPU: 0[/]")
    console.print("[dim]  ↳ Expected: ~2-4 hours for a 23-min episode on RTX 3070[/]")

    realesrgan_path = check_tool("realesrgan-ncnn-vulkan")

    def run_upscale(tile_size: int) -> bool:
        """Run upscale with given tile size by chunking frames to avoid memory leaks/hangs."""
        chunk_size = 1000
        frames_list = sorted(list(frames_in_dir.glob("*.png")))
        total_chunks = (len(frames_list) + chunk_size - 1) // chunk_size
        
        task = progress.add_task(f"[3/5] UPSCALE (tile={tile_size})", total=total_frames)

        for chunk_idx, i in enumerate(range(0, len(frames_list), chunk_size)):
            chunk_frames = frames_list[i : i + chunk_size]
            chunk_in_dir = frames_in_dir / f"chunk_{i:06d}"
            chunk_in_dir.mkdir(exist_ok=True)
            
            console.print(f"[dim]  ↳ Chunk {chunk_idx+1}/{total_chunks} ({len(chunk_frames)} frames)[/]")
            
            # Move frames to chunk directory
            for f in chunk_frames:
                f.rename(chunk_in_dir / f.name)

            cmd = [
                realesrgan_path,
                "-i", str(chunk_in_dir),
                "-o", str(frames_out_dir),
                "-n", "realesr-animevideov3",
                "-s", "2",
                "-f", "png",
                "-t", str(tile_size),
                "-j", "1:2:2",
                "-g", "0",
                "-m", str(MODELS_DIR),
            ]

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

            # Monitor progress by polling output file count
            # (realesrgan buffers stdout so readline-based tracking doesn't update the bar)
            last_count = len(list(frames_out_dir.glob("*.png")))
            while proc.poll() is None:
                time.sleep(2)
                current_count = len(list(frames_out_dir.glob("*.png")))
                if current_count > last_count:
                    progress.update(task, advance=current_count - last_count)
                    last_count = current_count
            
            # Read any remaining output for error reporting
            remaining = proc.stdout.read()
            stderr_lines = [l.strip() for l in remaining.split("\n") if l.strip()] if remaining else []

            # Final count update after chunk finishes
            current_count = len(list(frames_out_dir.glob("*.png")))
            if current_count > last_count:
                progress.update(task, advance=current_count - last_count)

            # Move frames back to frames_in_dir
            for f in chunk_frames:
                moved_file = chunk_in_dir / f.name
                if moved_file.exists():
                    moved_file.rename(frames_in_dir / f.name)
            try:
                chunk_in_dir.rmdir()
            except OSError:
                pass

            if proc.returncode != 0:
                console.print(f"\n[red]✗ Upscale failed at chunk {chunk_idx+1}:[/]")
                if stderr_lines:
                    console.print("[bold red]Last 20 lines of output:[/]")
                    for line in stderr_lines[-20:]:
                        console.print(f"  [red]{line}[/]")
                return False

        # Final count check
        final_count = len(list(frames_out_dir.glob("*.png")))
        progress.update(task, completed=final_count)
        if final_count < total_frames * 0.95:
             console.print(f"\n[red]✗ Upscale incomplete: {final_count}/{total_frames} frames[/]")
             return False

        return True

    # Try with tile 256 first
    if not run_upscale(256):
        console.print("[yellow]  ⚠ Retrying with smaller tile size (128) — likely VRAM issue[/]")
        # Clean partial output
        for f in frames_out_dir.glob("*.png"):
            f.unlink()
        if not run_upscale(128):
            console.print("[red]✗ Upscale failed even with tile 128. Check GPU/VRAM.[/]")
            sys.exit(1)

    mark_done("upscale")
    console.print("[green]  ✓ Stage 3 UPSCALE complete[/]")


def stage_encode(progress: Progress):
    """Stage 4: Encode upscaled frames to x265 10-bit."""
    if stage_done("encode"):
        console.print("[dim]  ↳ Stage 4 (ENCODE) already done, skipping.[/]")
        return

    frames_out_dir = WORK_DIR / "frames_out"
    output_video = WORK_DIR / "video_4k.mkv"

    # Get fps from demuxed video
    fps = get_fps(WORK_DIR / "video.mkv")
    if not fps:
        fps = "24000/1001"
    console.print(f"[dim]  ↳ Encoding at {fps} fps[/]")

    total_frames = len(list(frames_out_dir.glob("*.png")))
    console.print(f"[dim]  ↳ Frames to encode: {total_frames}[/]")

    ffmpeg_path = check_tool("ffmpeg")
    cmd = [
        ffmpeg_path, "-y",
        "-framerate", fps,
        "-i", str(frames_out_dir / "frame_%08d.png"),
        "-c:v", "hevc_nvenc",
        "-preset", "p6",       # High-quality preset for NVIDIA encoder
        "-cq", "18",           # Constant Quality target (NVENC equivalent to CRF)
        "-b:v", "0",           # Required to enable pure CQ mode on NVENC
        "-pix_fmt", "p010le",  # 10-bit pixel format required for hardware encoding
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-colorspace", "bt709",
        "-progress", "pipe:1",
        str(output_video),
    ]

    run_ffmpeg_with_progress(cmd, total_frames, "[4/5] ENCODE x265 10-bit", progress)

    # Verify output exists and has size
    if not output_video.exists() or output_video.stat().st_size < 1_000_000:
        console.print("[red]✗ Encoded video is too small or missing[/]")
        sys.exit(1)

    size_gb = output_video.stat().st_size / (1024 ** 3)
    console.print(f"[dim]  ↳ Encoded video size: {size_gb:.2f} GB[/]")

    mark_done("encode")
    console.print("[green]  ✓ Stage 4 ENCODE complete[/]")


def stage_mux(mkv_path: Path, output_dir: Path, progress: Progress) -> Path:
    """Stage 5: Mux video + audio + subs into final output."""
    stem = mkv_path.stem
    output_path = output_dir / f"{stem} [4K Upscale].mkv"

    if stage_done("mux"):
        console.print("[dim]  ↳ Stage 5 (MUX) already done, skipping.[/]")
        return output_path

    video_4k = WORK_DIR / "video_4k.mkv"

    task = progress.add_task("[5/5] MUX", total=1)

    ffmpeg_path = check_tool("ffmpeg")
    cmd = [
        ffmpeg_path, "-y", 
        "-i", str(video_4k), 
        "-i", str(mkv_path),
        "-map", "0:v:0",    # Video from the 4K encode
        "-map", "1:a?",     # All audio from original source
        "-map", "1:s?",     # All subtitles from original source
        "-map", "1:t?",     # All attachments (e.g., subtitle fonts) from original source
        "-c", "copy",       # Copy everything without re-encoding
        str(output_path)
    ]

    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")

    if r.returncode != 0:
        console.print(f"\n[red]✗ Mux failed[/]")
        console.print(f"[dim]Command: {' '.join(cmd)}[/]")
        for line in r.stderr.strip().split("\n")[-20:]:
            console.print(f"  [red]{line}[/]")
        sys.exit(1)

    progress.update(task, advance=1)

    # Verify
    if not output_path.exists() or output_path.stat().st_size < 1_000_000:
        console.print("[red]✗ Final output is too small or missing[/]")
        sys.exit(1)

    size_gb = output_path.stat().st_size / (1024 ** 3)
    console.print(f"[dim]  ↳ Final output: {output_path.name} ({size_gb:.2f} GB)[/]")

    mark_done("mux")
    console.print("[green]  ✓ Stage 5 MUX complete[/]")

    return output_path


def run_pipeline(mkv_path: Path, output_dir: Path = None):
    """Run the 5-stage pipeline."""
    if output_dir is None:
        output_dir = mkv_path.parent

    console.print()
    console.print(Panel.fit(
        "[bold magenta]4K Upscale Pipeline[/]",
        subtitle=f"Source: {mkv_path.name}",
        border_style="magenta",
    ))
    console.print()

    WORK_DIR.mkdir(parents=True, exist_ok=True)

    with make_progress() as progress:
        # Stage 1: Demux
        stage_demux(mkv_path, progress)

        # Stage 2: Extract frames
        stage_extract_frames(progress)

        # Stage 3: Upscale
        stage_upscale(progress)

        # Stage 4: Encode
        stage_encode(progress)

        # Stage 5: Mux
        output = stage_mux(mkv_path, output_dir, progress)

    console.print()
    console.print(Panel.fit(
        f"[bold green]✓ Pipeline complete![/]\n"
        f"Output: [cyan]{output}[/]",
        border_style="green",
    ))


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

@click.group()
def cli():
    """Anime 4K Upscale Pipeline — 1080p → 4K archival upscale."""
    pass


@cli.command()
@click.option("--file", "mkv_file", default=None, help="Path to specific .mkv file")
def verify(mkv_file):
    """Run the source quality verifier."""
    # Check deps
    _check_deps(verify_only=True)
    mkv_path = Path(mkv_file) if mkv_file else find_mkv()
    score = run_verifier(mkv_path)

    if score < 35:
        console.print("[bold red]❌ Score too low (<35). Refusing to continue.[/]")
        sys.exit(1)
    elif score < 55:
        console.print("[bold yellow]⚠ Score is marginal (35-54). Review before proceeding.[/]")
    else:
        console.print("[bold green]✓ Source passes quality check.[/]")


@cli.command()
@click.option("--skip-verify", is_flag=True, help="Skip verifier step")
@click.option("--input", "input_path", default=None, help="Path to a .mkv file or a folder of .mkv files")
def pipeline(skip_verify, input_path):
    """Run the full upscale pipeline on one or a folder of MKV files."""
    _check_deps(verify_only=False)

    # Determine files to process
    files_to_process = []
    source_dir = SCRIPT_DIR  # default output location

    if input_path:
        p = Path(input_path).resolve()
        if p.is_file() and p.suffix.lower() == ".mkv":
            files_to_process = [p]
            source_dir = p.parent
        elif p.is_dir():
            files_to_process = sorted(list(p.glob("*.mkv")))
            source_dir = p
        else:
            console.print("[red]Invalid input path. Must be an .mkv file or a directory.[/]")
            sys.exit(1)
    else:
        # Default: Process all MKVs in the script directory
        files_to_process = sorted(list(SCRIPT_DIR.glob("*.mkv")))
        
    # Filter out files that have already been upscaled
    files_to_process = [f for f in files_to_process if "[4K Upscale]" not in f.name and ".4K.upscaled" not in f.name]
    
    if not files_to_process:
        console.print("[red]No new .mkv files found to process.[/]")
        sys.exit(1)

    # ── Batch Summary Banner ──
    total_size = sum(f.stat().st_size for f in files_to_process) / (1024 ** 3)
    console.print()
    summary = Table(title="Batch Queue", show_header=True, header_style="bold cyan", border_style="magenta")
    summary.add_column("#", style="bold", width=4)
    summary.add_column("File", style="white")
    summary.add_column("Size", style="dim", justify="right")
    for idx, f in enumerate(files_to_process):
        size_mb = f.stat().st_size / (1024 ** 2)
        summary.add_row(str(idx + 1), f.name, f"{size_mb:.0f} MB")
    console.print(summary)
    console.print(f"[bold cyan]Total:[/] {len(files_to_process)} file(s), {total_size:.1f} GB")
    console.print(f"[bold cyan]Output:[/] {source_dir}")
    console.print()

    completed = 0
    skipped = 0
    for idx, mkv_path in enumerate(files_to_process):
        console.rule(f"[bold] Episode {idx+1}/{len(files_to_process)}: {mkv_path.name} ", style="magenta")
        
        # Check if output already exists
        output_file = source_dir / f"{mkv_path.stem} [4K Upscale].mkv"
        if output_file.exists():
            console.print(f"[yellow]  ⚠ Output already exists, skipping: {output_file.name}[/]")
            skipped += 1
            continue

        if not skip_verify:
            score = run_verifier(mkv_path)
            if score < 35:
                console.print(f"[bold red]❌ Score too low (<35) for {mkv_path.name}. Skipping.[/]")
                skipped += 1
                continue
            if score < 55:
                console.print("[bold yellow]⚠ Score is marginal. Proceeding anyway.[/]")

        # Clean work directory completely before each new file
        console.print("[dim]  ↳ Preparing clean work directory...[/]")
        for marker in [".demux.done", ".extract.done", ".upscale.done", ".encode.done", ".mux.done"]:
            (WORK_DIR / marker).unlink(missing_ok=True)
        for leftover in ["video.mkv", "video_4k.mkv"]:
            (WORK_DIR / leftover).unlink(missing_ok=True)
        for d in ["frames_in", "frames_out"]:
            p = WORK_DIR / d
            if p.exists():
                shutil.rmtree(p)

        run_pipeline(mkv_path, output_dir=source_dir)
        completed += 1

        # Post-pipeline cleanup: free disk space for the next episode
        console.print("[dim]  ↳ Cleaning up scratch files...[/]")
        for d in ["frames_in", "frames_out"]:
            p = WORK_DIR / d
            if p.exists():
                shutil.rmtree(p)
        for leftover in ["video.mkv", "video_4k.mkv"]:
            (WORK_DIR / leftover).unlink(missing_ok=True)

    # ── Final Summary ──
    console.print()
    console.print(Panel.fit(
        f"[bold green]✓ Batch Complete![/]\n"
        f"  Processed: [cyan]{completed}[/] file(s)\n"
        f"  Skipped:   [yellow]{skipped}[/] file(s)\n"
        f"  Output:    [cyan]{source_dir}[/]",
        border_style="green",
        title="Summary",
    ))


def _check_deps(verify_only: bool):
    """Check all required dependencies exist."""
    console.print(Panel.fit("[bold]Checking dependencies...[/]", border_style="dim"))

    deps = {
        "ffmpeg": check_tool("ffmpeg"),
        "ffprobe": check_tool("ffprobe"),
        "mediainfo": check_tool("mediainfo"),
    }
    if not verify_only:
        deps["realesrgan-ncnn-vulkan"] = check_tool("realesrgan-ncnn-vulkan")

    all_ok = True
    for name, path in deps.items():
        if path:
            console.print(f"  [green]✓[/] {name}: {path}")
        else:
            console.print(f"  [red]✗[/] {name}: NOT FOUND")
            all_ok = False

    if not all_ok:
        console.print("\n[red]Missing dependencies. Install them and retry.[/]")
        sys.exit(1)

    # Check models if not verify-only
    if not verify_only:
        model_param = MODELS_DIR / "realesr-animevideov3-x2.param"
        model_bin = MODELS_DIR / "realesr-animevideov3-x2.bin"
        if model_param.exists() and model_bin.exists():
            console.print(f"  [green]✓[/] Model files: {MODELS_DIR}")
        else:
            console.print(f"  [red]✗[/] Model files missing in {MODELS_DIR}")
            console.print(f"  [dim]  Expected: {model_param.name} and {model_bin.name}[/]")
            sys.exit(1)

    console.print()


if __name__ == "__main__":
    cli()
