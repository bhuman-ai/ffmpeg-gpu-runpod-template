""" Example handler file. """

import os
import math
import boto3
import shlex
import runpod
import tempfile
import subprocess
from urllib.parse import urlparse
import requests
from botocore.config import Config as BotoConfig

HANDLER_VERSION = "2025-10-06-raw-ffmpeg-v2"

S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL", "https://storage.googleapis.com")
S3_REGION = os.environ.get("S3_REGION", "auto")
S3_ACCESS_KEY = os.environ.get("HMAC_KEY") or os.environ.get("AWS_ACCESS_KEY_ID")
S3_SECRET_KEY = os.environ.get("HMAC_SECRET") or os.environ.get("AWS_SECRET_ACCESS_KEY")
S3_ADDRESSING_STYLE = os.environ.get("S3_ADDRESSING_STYLE", "virtual")  # "virtual" or "path"

s3 = boto3.client(
    "s3",
    region_name=S3_REGION,
    endpoint_url=S3_ENDPOINT_URL,
    aws_access_key_id=S3_ACCESS_KEY,
    aws_secret_access_key=S3_SECRET_KEY,
    config=BotoConfig(
        signature_version="s3v4",
        s3={"addressing_style": S3_ADDRESSING_STYLE},
    ),
)


def get_bucket_key(uri):
    uri = uri.replace("gs://", "").replace("s3://", "")
    bucket, key = uri.split("/", maxsplit=1)
    filename = os.path.basename(key)
    return bucket, key, filename


def is_http_uri(uri: str) -> bool:
    try:
        parsed = urlparse(uri)
        return parsed.scheme in ("http", "https")
    except Exception:
        return False


def download_uri_to_file(uri: str, filename: str):
    if is_http_uri(uri):
        with requests.get(uri, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(filename, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):  # 1MB
                    if chunk:
                        f.write(chunk)
        return
    # Assume gs:// or s3://
    bucket, key, _ = get_bucket_key(uri)
    s3.download_file(Bucket=bucket, Key=key, Filename=filename)


def upload_file_to_destination(dest: str, filename: str, content_type: str = "video/mp4"):
    """Upload to s3:// or gs:// via boto3, or to http(s):// via PUT (presigned URL)."""
    if is_http_uri(dest):
        with open(filename, "rb") as f:
            resp = requests.put(dest, data=f, headers={"Content-Type": content_type}, timeout=120)
        if resp.status_code >= 300:
            raise Exception(f"HTTP PUT upload failed: {resp.status_code} {resp.text[:512]}")
        return
    bucket, key, _ = get_bucket_key(dest)
    s3.upload_file(Filename=filename, Bucket=bucket, Key=key)


def get_ffmpeg_bin():
    candidate = os.environ.get("FFMPEG_BIN")
    if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    if os.path.isfile("/ffmpeg") and os.access("/ffmpeg", os.X_OK):
        return "/ffmpeg"
    return "ffmpeg"


def run_ffmpeg(parts):
    try:
        proc = subprocess.run(parts, shell=False, capture_output=True, text=True)
        print("Complete command:")
        try:
            print(" ".join(shlex.quote(p) for p in parts))
        except Exception:
            pass
        print(f"Return code: {proc.returncode}")
        if proc.stdout:
            print(proc.stdout)
        if proc.stderr:
            print(proc.stderr)
        return proc
    except Exception as e:
        print(f"ffmpeg execution error: {e}")
        raise


def probe_duration_seconds(path: str) -> float:
    """Return media duration in seconds using ffprobe; 0.0 if unknown."""
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            shell=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return max(0.0, float(proc.stdout.strip()))
    except Exception as e:
        print(f"ffprobe error for {path}: {e}")
    return 0.0


def encode_video(
    input_video: str,
    input_audio: str,
    subtitles: str,
    output_video: str,
    subtitles_enabled: bool = True,
    matroska: bool = False,
):
    def run(parts):
        cmd = " ".join(parts)
        print("Complete command:")
        print(cmd)
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        print(f"Return code: {proc.returncode}")
        if proc.stdout:
            print(proc.stdout)
        if proc.stderr:
            print(proc.stderr)
        return proc

    # Try 1: Full GPU path (NVDEC + CUDA + NVENC)
    vf_filters = []
    if subtitles_enabled:
        vf_filters.append(f"ass={subtitles}:fontsdir=/assets")
        vf_filters.append("hwupload_cuda")

    gpu_cmd = [get_ffmpeg_bin()]
    gpu_cmd += ["-hwaccel", "nvdec", "-hwaccel_output_format", "cuda"]
    gpu_cmd += ["-i", shlex.quote(input_video), "-i", shlex.quote(input_audio)]
    if vf_filters:
        gpu_cmd += ["-vf", ",".join(vf_filters)]
    if matroska:
        gpu_cmd += ["-f", "matroska"]
    gpu_cmd += ["-map", "0:v", "-map", "1:a", "-c:v", "h264_nvenc", "-c:a", "aac", shlex.quote(output_video)]

    result = run(gpu_cmd)
    if result.returncode == 0 and os.path.exists(output_video):
        try:
            if os.path.getsize(output_video) > 0:
                return
            else:
                print("encode_video: GPU path produced 0-byte file, will retry")
        except Exception:
            pass

    # Try 2: CPU filters + NVENC encode
    print("GPU pipeline failed; retrying with software filters + NVENC.")
    sw_cmd = [get_ffmpeg_bin(), "-i", shlex.quote(input_video), "-i", shlex.quote(input_audio)]
    if subtitles_enabled:
        sw_cmd += ["-vf", f"ass={subtitles}:fontsdir=/assets"]
    if matroska:
        sw_cmd += ["-f", "matroska"]
    sw_cmd += ["-map", "0:v", "-map", "1:a", "-c:v", "h264_nvenc", "-c:a", "aac", shlex.quote(output_video)]
    result = run(sw_cmd)
    if result.returncode == 0 and os.path.exists(output_video):
        try:
            if os.path.getsize(output_video) > 0:
                return
            else:
                print("encode_video: SW+NVENC produced 0-byte file, will retry")
        except Exception:
            pass

    # Try 3: CPU encode with libx264
    print("NVENC not available or failed; retrying with libx264.")
    cpu_cmd = [get_ffmpeg_bin(), "-i", shlex.quote(input_video), "-i", shlex.quote(input_audio)]
    if subtitles_enabled:
        cpu_cmd += ["-vf", f"ass={subtitles}:fontsdir=/assets"]
    if matroska:
        cpu_cmd += ["-f", "matroska"]
    cpu_cmd += ["-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-c:a", "aac", shlex.quote(output_video)]
    result = run(cpu_cmd)
    if result.returncode != 0:
        print("All encoding strategies failed.")
    else:
        try:
            if not os.path.exists(output_video) or os.path.getsize(output_video) == 0:
                raise Exception("encode_video: CPU output missing or 0 bytes")
        except Exception as e:
            print(str(e))
            raise


def downsample_video(
    input_video: str,
    output_video: str,
    resolution=240
):
    ratio = f"{int(resolution*16/9)}:{resolution}"
    def run_cmd(parts):
        cmd_line = " ".join(parts)
        print("Complete command:")
        print(cmd_line)
        proc = subprocess.run(cmd_line, shell=True, capture_output=True, text=True)
        print(f"Return code: {proc.returncode}")
        if proc.stdout:
            print(proc.stdout)
        if proc.stderr:
            print(proc.stderr)
        return proc

    # Try 1: GPU scale + NVENC
    cmd = [get_ffmpeg_bin(), "-y", "-hwaccel", "cuvid", "-hwaccel_output_format", "cuda",
           "-i", shlex.quote(input_video), "-vcodec", "h264_nvenc",
           "-vf", f'scale_cuda=\"{ratio}\"', "-cq", "26", "-c:a", "copy", "-movflags", "+faststart", shlex.quote(output_video)]
    result = run_cmd(cmd)

    if result.returncode != 0 or not os.path.exists(output_video):
        # Try 2: software scale + NVENC encode
        print("GPU scale failed or output missing; falling back to software scale.")
        cmd2 = [get_ffmpeg_bin(), "-y", "-i", shlex.quote(input_video),
                "-vf", f"scale={ratio}", "-c:v", "h264_nvenc", "-cq", "26", "-c:a", "copy", "-movflags", "+faststart",
                shlex.quote(output_video)]
        result = run_cmd(cmd2)
        if result.returncode != 0 or not os.path.exists(output_video):
            # Try 3: software scale + libx264
            print("NVENC encode failed; falling back to libx264.")
            cmd3 = [get_ffmpeg_bin(), "-y", "-i", shlex.quote(input_video),
                    "-vf", f'scale={ratio}', "-c:v", "libx264", "-crf", "23", "-c:a", "copy", "-movflags", "+faststart",
                    shlex.quote(output_video)]
            result = run_cmd(cmd3)


def concatenate_videos(
    segment_files,
    output_video: str,
    crf: int = 23,
    audio_kbps: int = 128,
):
    n = len(segment_files)
    assert n >= 2, "Need at least 2 segments to concatenate"

    def run_cmd(parts):
        cmd_line = " ".join(parts)
        print("Complete command:")
        print(cmd_line)
        proc = subprocess.run(cmd_line, shell=True, capture_output=True, text=True)
        print(f"Return code: {proc.returncode}")
        if proc.stdout:
            print(proc.stdout)
        if proc.stderr:
            print(proc.stderr)
        return proc

    # Build inputs
    base = [get_ffmpeg_bin(), "-y"]
    for f in segment_files:
        base += ["-i", shlex.quote(f)]

    # Build filter_complex to normalize streams and concat
    chains = []
    for i in range(n):
        chains.append(f"[{i}:v]scale=trunc(iw/2)*2:trunc(ih/2)*2,setpts=PTS-STARTPTS[v{i}]")
        chains.append(f"[{i}:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,aresample=async=0:first_pts=0,asetpts=PTS-STARTPTS[a{i}]")
    concat_inputs = "".join([f"[v{i}][a{i}]" for i in range(n)])
    filter_complex = f"{';'.join(chains)};{concat_inputs}concat=n={n}:v=1:a=1[outv][outa]"

    # Try 1: NVENC encode
    cmd1 = base + [
        "-filter_complex", shlex.quote(filter_complex),
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "h264_nvenc", "-cq", "24",
        "-c:a", "aac", "-b:a", f"{audio_kbps}k",
        "-movflags", "+faststart",
        shlex.quote(output_video)
    ]
    res = run_cmd(cmd1)
    if res.returncode == 0 and os.path.exists(output_video):
        try:
            if os.path.getsize(output_video) > 0:
                return
            else:
                print("concatenate_videos: NVENC produced 0-byte output, try libx264")
        except Exception:
            pass

    # Try 2: libx264
    print("NVENC failed; falling back to libx264 for concat.")
    cmd2 = base + [
        "-filter_complex", shlex.quote(filter_complex),
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", "fast", "-crf", str(crf),
        "-c:a", "aac", "-b:a", f"{audio_kbps}k",
        "-movflags", "+faststart",
        shlex.quote(output_video)
    ]
    res = run_cmd(cmd2)
    if res.returncode == 0 and os.path.exists(output_video):
        try:
            if os.path.getsize(output_video) > 0:
                return
        except Exception:
            pass
    raise Exception("Concatenation failed or produced empty output.")


def handler(job_main):
    """ Handler function that will be used to process jobs. """
    job = job_main["input"]
    print(job)
    task = job['task']
    event = job['parameters']

    if task == "ENCODING":
        _id = event.get("id")
        language = event.get("language")
        subtitles_enabled = event.get("subtitles", False)
        name = event.get("name", "exported_video.mp4")
        input_video_name = event.get("input_video_name", "video.mp4")

        # Legacy S3/GCS path style
        bucket = event.get("bucket")
        bucket_parent_folder = event.get("bucket_parent_folder")

        # New: explicit URIs for inputs and (optionally) output
        input_video_uri = event.get("input_video_uri")
        input_audio_uri = event.get("input_audio_uri")
        subtitles_uri = event.get("subtitles_uri")
        output_video_uri = event.get("output_video_uri")  # expects gs:// or s3://

        with tempfile.TemporaryDirectory() as tmpdirname:
            input_video = os.path.join(tmpdirname, "video.mp4")
            input_audio = os.path.join(tmpdirname, "exported_with_music.wav")
            subtitle_file = os.path.join(tmpdirname, f"subtitles_{language or 'en'}.ass")
            output_video = os.path.join(tmpdirname, "exported_video.mp4")

            if input_video_uri and input_audio_uri:
                # Download using URIs (http/https/gs/s3)
                print(f"Downloading via URIs: {input_video_uri}, {input_audio_uri}")
                download_uri_to_file(input_video_uri, input_video)
                download_uri_to_file(input_audio_uri, input_audio)
                if subtitles_enabled:
                    assert subtitles_uri, "subtitles_uri must be provided when subtitles=true"
                    print(f"Downloading subtitles: {subtitles_uri}")
                    download_uri_to_file(subtitles_uri, subtitle_file)
            else:
                # Legacy flow: construct keys and use S3-compatible storage
                assert bucket is not None, "bucket is required when URIs are not provided"
                assert bucket_parent_folder is not None, "bucket_parent_folder is required when URIs are not provided"
                video_key = f"{bucket_parent_folder}/{_id}/{input_video_name}"
                audio_key = f"{bucket_parent_folder}/{_id}/exported_with_music.wav"
                subtitles_key = f"{bucket_parent_folder}/{_id}/subtitles_{language}.ass"
                print(bucket, video_key, input_video)
                s3.download_file(Bucket=bucket, Key=video_key, Filename=input_video)
                s3.download_file(Bucket=bucket, Key=audio_key, Filename=input_audio)
                s3.download_file(Bucket=bucket, Key=subtitles_key, Filename=subtitle_file)

            # Encode audio, video and subtitles
            encode_video(
                input_video,
                input_audio,
                subtitle_file,
                output_video,
                subtitles_enabled,
            )

            if not os.path.exists(output_video):
                raise Exception("Video was unable to encode.")

            # Upload the resultant video
            output_put_url = event.get("output_put_url")  # optional: presigned HTTPS PUT
            if output_put_url:
                upload_file_to_destination(output_put_url, output_video)
                uploaded_to = output_put_url
            elif output_video_uri:
                upload_file_to_destination(output_video_uri, output_video)
                uploaded_to = output_video_uri
            else:
                assert bucket is not None and bucket_parent_folder is not None, "Provide output_video_uri or bucket/bucket_parent_folder"
                exported_video_key = f"{bucket_parent_folder}/{_id}/{name}"
                s3.upload_file(Filename=output_video, Bucket=bucket, Key=exported_video_key)
                uploaded_to = f"gs://{bucket}/{exported_video_key}"

            return {
                '_id': _id,
                'statusCode': 200,
                'output_uri': uploaded_to,
                'body': 'Video re-encoding and upload completed!'
            }
    elif task == "DOWNSAMPLING":
        original_video_uri = event.get("original_video_uri")
        output_video_uri = event.get("output_video_uri")
        resolution = int(str(event.get("resolution", "240")).strip("p"))

        with tempfile.TemporaryDirectory() as tmpdirname:
            original_video = os.path.join(tmpdirname, "video.mp4")
            output_video = os.path.join(tmpdirname, "output.mp4")

            print(original_video_uri, output_video_uri, resolution)
            # Support http/https or s3/gs input
            download_uri_to_file(original_video_uri, original_video)

            # Encode audio, video and subtitles
            downsample_video(
                original_video,
                output_video,
                resolution=resolution
            )
            if not os.path.exists(output_video):
                raise Exception("Video was unable to encode.")

            # Upload the resultant video to the destination
            output_put_url = event.get("output_put_url")  # optional: presigned HTTPS PUT
            if output_put_url:
                upload_file_to_destination(output_put_url, output_video)
            else:
                bucket, exported_video_key, _ = get_bucket_key(output_video_uri)
                s3.upload_file(Filename=output_video, Bucket=bucket, Key=exported_video_key)
            return {
                'statusCode': 200,
                'body': 'Video downsampling successful!'
            }
    elif task == "FFMPEG_RAW":
        # Execute an arbitrary ffmpeg command with placeholder-substituted inputs/outputs.
        # parameters: { inputs: [{uri: "..."}, ...], args: ["-i", "{in0}", "...", "{out0}"],
        #              output_video_uri?: s3://..., output_put_url?: https://... }
        params = event
        args = params.get("args", [])
        inputs = params.get("inputs", [])
        output_video_uri = params.get("output_video_uri")
        output_put_url = params.get("output_put_url")

        if not isinstance(args, list) or not args:
            raise Exception("Provide 'args' array for ffmpeg.")

        with tempfile.TemporaryDirectory() as tmpdirname:
            # Download inputs
            in_map = {}
            for idx, inp in enumerate(inputs or []):
                uri = inp.get("uri") if isinstance(inp, dict) else None
                if not uri:
                    raise Exception(f"inputs[{idx}].uri missing")
                local = os.path.join(tmpdirname, f"in{idx}.bin")
                print(f"Downloading input {idx}: {uri}")
                download_uri_to_file(uri, local)
                in_map[f"{{in{idx}}}"] = local

            # Prepare output placeholder(s) — support {out0} only for now
            out_path = os.path.join(tmpdirname, "out0.mp4")
            out_map = {"{out0}": out_path}

            # Substitute placeholders
            resolved_args = []
            for a in args:
                if not isinstance(a, str):
                    a = str(a)
                # Replace input placeholders
                for ph, path in in_map.items():
                    a = a.replace(ph, path)
                # Replace output placeholders
                for ph, path in out_map.items():
                    a = a.replace(ph, path)
                resolved_args.append(a)

            # Ensure there are no unresolved placeholders remaining
            leftover = [a for a in resolved_args if "{in" in a or "{out" in a]
            if leftover:
                raise Exception(f"Unresolved placeholders in args: {leftover}")

            cmd = [get_ffmpeg_bin()] + resolved_args
            proc = run_ffmpeg(cmd)

            if not os.path.exists(out_path):
                raise Exception("Output file not found after ffmpeg execution.")
            try:
                sz = os.path.getsize(out_path)
            except Exception:
                sz = 0
            if sz <= 0:
                raise Exception("FFMPEG_RAW produced 0-byte output.")

            dest = output_put_url or output_video_uri
            if not dest:
                raise Exception("Provide output_video_uri or output_put_url for upload.")
            upload_file_to_destination(dest, out_path)

            return {
                'statusCode': 200,
                'body': 'FFMPEG command executed successfully!'
            }
    elif task == "AUDIO_TRIM":
        source_uri = event.get("source_uri")
        start_sec = float(event.get("start_sec", 0))
        duration_sec = float(event.get("duration_sec", 0))
        # Optional: pad tail to exact integer seconds to satisfy strict croppers (e.g., AudioCrop)
        target_sec = event.get("target_sec")
        target_sec = float(target_sec) if target_sec is not None else None
        output_video_uri = event.get("output_video_uri")
        output_put_url = event.get("output_put_url")
        if not source_uri:
            raise Exception("source_uri is required")

        with tempfile.TemporaryDirectory() as tmpdirname:
            src = os.path.join(tmpdirname, "in.m4a")
            out = os.path.join(tmpdirname, "out.wav")
            download_uri_to_file(source_uri, src)

            # Build trimming + padding command.
            # Always attempt to pad output to the next whole second unless an explicit target_sec is provided.
            args = [get_ffmpeg_bin(), "-y"]
            # Input seek/trim to reduce decode work
            if start_sec > 0:
                args += ["-ss", str(start_sec)]
            if duration_sec > 0:
                args += ["-t", str(duration_sec)]
            args += ["-i", src]

            # Decide final output duration target
            pad_target = None
            if target_sec and target_sec > 0:
                pad_target = int(math.ceil(target_sec))
            else:
                # If duration_sec provided, round it up; otherwise base on source duration - start
                if duration_sec > 0:
                    desired = max(0.0, float(duration_sec))
                else:
                    src_len = probe_duration_seconds(src)
                    desired = max(0.0, src_len - max(0.0, start_sec))
                # Tolerate tiny floating errors close to an integer (e.g., 6.00001)
                frac, whole = math.modf(desired)
                if frac < 1e-3:
                    pad_target = int(whole)
                else:
                    pad_target = int(math.ceil(desired))
                if pad_target <= 0:
                    pad_target = 1

            # Add a small safety margin to avoid decoder/codec priming truncation (esp. with AAC); keep WAV to be exact.
            pad_target_plus = pad_target + 0.25
            # Apply padding via filter and cut exactly to target using atrim
            args += ["-af", f"apad,atrim=0:{pad_target_plus:.2f}"]
            # Encode to WAV (exact sample count, no encoder delay). Force stereo 48k for downstream consistency.
            args += ["-vn", "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", out]
            proc = run_ffmpeg(args)
            if proc.returncode != 0 or not os.path.exists(out):
                raise Exception("Audio trim failed")
            try:
                if os.path.getsize(out) <= 0:
                    raise Exception("Audio trim produced 0-byte file")
            except Exception as e:
                raise

            dest = output_put_url or output_video_uri
            if not dest:
                raise Exception("Provide output_video_uri or output_put_url")
            upload_file_to_destination(dest, out, content_type="audio/wav")

            return { 'statusCode': 200, 'body': 'Audio trim successful!' }
    elif task == "PING":
        return {
            'statusCode': 200,
            'version': HANDLER_VERSION,
            'env': {
                'S3_ENDPOINT_URL': S3_ENDPOINT_URL,
                'S3_REGION': S3_REGION,
            },
            'body': 'pong'
        }
    elif task == "CONCATENATION":
        segment_urls = event.get("segment_urls") or []
        output_video_uri = event.get("output_video_uri")
        output_put_url = event.get("output_put_url")
        crf = int(event.get("crf", 23))
        audio_kbps = int(event.get("audio_kbps", 128))

        if not isinstance(segment_urls, list) or len(segment_urls) < 2:
            raise Exception("Provide segment_urls with at least 2 items")

        with tempfile.TemporaryDirectory() as tmpdirname:
            local_segments = []
            for idx, uri in enumerate(segment_urls):
                p = os.path.join(tmpdirname, f"seg_{idx}.mp4")
                print(f"Downloading segment {idx}: {uri}")
                download_uri_to_file(uri, p)
                local_segments.append(p)

            output_video = os.path.join(tmpdirname, "concatenated.mp4")
            concatenate_videos(local_segments, output_video, crf=crf, audio_kbps=audio_kbps)
            if not os.path.exists(output_video):
                raise Exception("Concatenation failed.")
            try:
                if os.path.getsize(output_video) <= 0:
                    raise Exception("Concatenation produced 0-byte output")
            except Exception as e:
                raise

            dest = output_put_url or output_video_uri
            if not dest:
                raise Exception("Provide output_video_uri or output_put_url")
            upload_file_to_destination(dest, output_video)
            return {
                'statusCode': 200,
                'body': 'Video concatenation successful!'
            }
    



runpod.serverless.start({"handler": handler})
