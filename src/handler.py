""" Example handler file. """

import os
import boto3
import shlex
import runpod
import tempfile
import subprocess
from urllib.parse import urlparse
import requests
from botocore.config import Config as BotoConfig

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


def get_ffmpeg_bin():
    candidate = os.environ.get("FFMPEG_BIN")
    if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    if os.path.isfile("/ffmpeg") and os.access("/ffmpeg", os.X_OK):
        return "/ffmpeg"
    return "ffmpeg"


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
        return subprocess.run(cmd, shell=True)

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
        return

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
        return

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
        return subprocess.run(cmd_line, shell=True)

    # Try 1: GPU scale + NVENC
    cmd = [get_ffmpeg_bin(), "-hwaccel", "cuvid", "-hwaccel_output_format", "cuda",
           "-i", shlex.quote(input_video), "-vcodec", "h264_nvenc",
           "-vf", f'scale_cuda="{ratio}"', "-cq", "26", shlex.quote(output_video)]
    result = run_cmd(cmd)

    if result.returncode != 0 or not os.path.exists(output_video):
        # Try 2: software scale + NVENC encode
        print("GPU scale failed or output missing; falling back to software scale.")
        cmd2 = [get_ffmpeg_bin(), "-i", shlex.quote(input_video),
                "-vf", f'scale={ratio}', "-c:v", "h264_nvenc", "-cq", "26",
                shlex.quote(output_video)]
        result = run_cmd(cmd2)
        if result.returncode != 0 or not os.path.exists(output_video):
            # Try 3: software scale + libx264
            print("NVENC encode failed; falling back to libx264.")
            cmd3 = [get_ffmpeg_bin(), "-i", shlex.quote(input_video),
                    "-vf", f'scale={ratio}', "-c:v", "libx264", "-crf", "23",
                    shlex.quote(output_video)]
            result = run_cmd(cmd3)


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
            if output_video_uri:
                bkt, out_key, _ = get_bucket_key(output_video_uri)
                s3.upload_file(Filename=output_video, Bucket=bkt, Key=out_key)
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

            # Upload the resultant video to the destination S3 bucket
            bucket, exported_video_key, _ = get_bucket_key(output_video_uri)
            s3.upload_file(Filename=output_video, Bucket=bucket, Key=exported_video_key)
            return {
                'statusCode': 200,
                'body': 'Video downsampling successful!'
            }
    



runpod.serverless.start({"handler": handler})
