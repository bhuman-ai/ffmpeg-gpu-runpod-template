# ffmpeg-gpu-runpod-template
- GPU-enabled FFmpeg template for RunPod (CUDA 12.x)
- Supports inputs from `http(s)://`, `gs://`, and `s3://`
- Outputs to `gs://` or `s3://` buckets
  - Works with Cloudflare R2 via S3 API
  - Or upload via HTTPS presigned PUT (`output_put_url`)

## Deploy to RunPod Serverless
- Build image: `docker build -t <registry>/<repo>:<tag> .`
- Push image: `docker push <registry>/<repo>:<tag>`
- Create a Serverless template in RunPod using your image.
- Command is set via Dockerfile (`python3.11 -u /handler.py`).
- Set environment variables as needed:
  - `S3_ENDPOINT_URL` (default: `https://storage.googleapis.com`)
  - `S3_REGION` (default: `auto`)
  - `S3_ADDRESSING_STYLE` (default: `virtual`; set `path` if needed)
  - `HMAC_KEY`/`HMAC_SECRET` (GCS HMAC) or `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` (AWS S3, Cloudflare R2)

### Cloudflare R2 Setup
- Create/Get an R2 bucket (e.g., `uploaded-audio`).
- Create R2 S3 API credentials: R2 > Settings > S3 API Tokens > Create API Token (Object Read/Write).
- Collect:
  - `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`
  - Your account ID (from R2; used in endpoint URL)
- Set in RunPod Template env vars:
  - `S3_ENDPOINT_URL` = `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`
  - `S3_REGION` = `auto`
  - `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` = from token
  - Optional: `S3_ADDRESSING_STYLE` = `virtual` (default) or `path`
- Use `s3://<bucket>/<key>` URIs for outputs, e.g. `s3://uploaded-audio/exports/out.mp4`.

### Alternative: Presigned URL Upload (no credentials in worker)
- Generate a presigned PUT URL for the target object (valid for e.g. 1 hour).
- Then pass it in the payload as `output_put_url` instead of `output_video_uri`.

Using AWS CLI against R2 (macOS/Linux):
```
aws configure set default.s3.signature_version s3v4
aws --endpoint-url https://<ACCOUNT_ID>.r2.cloudflarestorage.com \
    s3 presign s3://uploaded-audio/exports/out.mp4 --expires-in 3600
```
The command prints a long `https://...` URL. Use it like:
```
{
  "input": {
    "task": "DOWNSAMPLING",
    "parameters": {
      "original_video_uri": "https://example.com/video.mp4",
      "output_put_url": "https://<presigned-url>",
      "resolution": "360p"
    }
  }
}
```

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
      "output_video_uri": "s3://uploaded-audio/exports/downsampled.mp4",  // R2 example
      "resolution": "360p"  // or 240, 360, 480 etc.
    }
  }
}
```

## Notes
- Public URL inputs are streamed to a temp file before processing.
- Outputs currently upload to S3/GCS; HTTP destinations aren’t supported.
- This build uses NVENC/NVDEC; choose a GPU-enabled Serverless runtime.
