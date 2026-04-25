# Anime 4K AI Upscaling Pipeline

A fully automated, production-grade AI upscaling pipeline designed specifically for upgrading 1080p Anime MKV files to premium 4K archival quality.

## Features
- **Intelligent Resumability**: Safe to abort at any time. Uses `.done` scratch markers to resume exactly where it left off.
- **VRAM Safe Batching**: Intelligently chunks frames to prevent VRAM memory leaks or GPU hangs when processing huge directories of frames.
- **Hardware Encoding (NVENC)**: Drops 8-hour CPU encoding times down to 10-15 minutes using dedicated NVIDIA GPU silicon.
- **Flawless Metadata Preservation**: Perfectly maps and copies all Audio, Subtitles, and custom Font attachments (`.ttf` files used in ASS subtitles) from the original source.
- **Batch Folder Processing**: Automatically iterates through an entire folder of MKVs, processing them one by one unattended.
- **Source Quality Verifier**: Scans and grades source MKV files based on bitrate, codec, bit-depth, and release groups to ensure optimal AI upscaling quality.

## Requirements
- Windows OS
- NVIDIA RTX GPU (Tested on RTX 3070)
- Python 3.10+
- `ffmpeg`, `ffprobe`, `mediainfo` added to system PATH
- `realesrgan-ncnn-vulkan.exe` (Extract to `./tools/realesrgan/`)
- Python packages: `rich`, `click`

## Usage

### Batch Process a Folder
```powershell
python upscale.py pipeline --input "C:\path\to\your\anime\folder"
```

### Process a Specific File
```powershell
python upscale.py pipeline --input "episode_01.mkv"
```

### Verify Source Quality
```powershell
python upscale.py verify --file "episode_01.mkv"
```

## Pipeline Stages
1. **DEMUX**: Extracts the raw video stream.
2. **EXTRACT FRAMES**: Slices the video into tens of thousands of individual PNG frames.
3. **UPSCALE (Real-ESRGAN)**: AI upscales each frame from 1080p to true 4K (x2 scale).
4. **ENCODE (HEVC NVENC)**: Compresses the 4K frames into an ultra-fast, high-quality x265 video.
5. **MUX**: Stitches the original audio, subtitles, and fonts back into the brand new 4K MKV.
