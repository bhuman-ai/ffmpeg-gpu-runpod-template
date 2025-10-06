# ffmpeg-gpu-runpod-template
- GPU-enabled FFmpeg template for RunPod (CUDA 12.x)
- Supports inputs from `http(s)://`, `gs://`, and `s3://`
- Outputs to `gs://` or `s3://` buckets

## Deploy to RunPod Serverless
- Build image: `docker build -t <registry>/<repo>:<tag> .`
- Push image: `docker push <registry>/<repo>:<tag>`
- Create a Serverless template in RunPod using your image.
- Command is set via Dockerfile (`python3.11 -u /handler.py`).
- Set environment variables as needed:
  - `S3_ENDPOINT_URL` (default: `https://storage.googleapis.com`)
  - `S3_REGION` (default: `auto` for GCS; e.g. `us-east-1` for AWS)
  - `HMAC_KEY`/`HMAC_SECRET` (GCS HMAC) or `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` (AWS S3)

## Invocation Payloads

Encoding (using public URLs for inputs):
```
{
  "input": {
    "task": "ENCODING",
    "parameters": {
      "id": "job-123",
      "language": "en",
      "subtitles": true,
      "input_video_uri": "https://example.com/video.mp4",
      "input_audio_uri": "https://example.com/audio.wav",
      "subtitles_uri": "https://example.com/subtitles_en.ass",
      "output_video_uri": "gs://my-bucket/exports/job-123/output.mp4"
    }
  }
}
```

Encoding (legacy GCS/S3-style parameters):
```
{
  "input": {
    "task": "ENCODING",
    "parameters": {
      "id": "job-123",
      "language": "en",
      "subtitles": true,
      "name": "exported_video.mp4",
      "input_video_name": "video.mp4",
      "bucket": "my-bucket",
      "bucket_parent_folder": "exports"
    }
  }
}
```

Downsampling (input can be public URL):
```
{
  "input": {
    "task": "DOWNSAMPLING",
    "parameters": {
      "original_video_uri": "https://example.com/big.mp4",
      "output_video_uri": "gs://my-bucket/exports/downsampled.mp4",
      "resolution": "360p"  // or 240, 360, 480 etc.
    }
  }
}
```

## Notes
- Public URL inputs are streamed to a temp file before processing.
- Outputs currently upload to S3/GCS; HTTP destinations aren’t supported.
- This build uses NVENC/NVDEC; choose a GPU-enabled Serverless runtime.
