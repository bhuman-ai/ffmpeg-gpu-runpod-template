# ffmpeg-gpu-runpod-template
- GPU-enabled FFmpeg template for RunPod (CUDA 12.x)
- Supports inputs from `http(s)://`, `gs://`, and `s3://`
- Outputs to `gs://` or `s3://` buckets
  - Works with Cloudflare R2 via S3 API
  - Or upload via HTTPS presigned PUT (`output_put_url`)
  - Optional public-read fallback for inputs via `R2_PUBLIC_BASE_URL`

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
  - Optional input fallback: `R2_PUBLIC_BASE_URL` (e.g., `https://videos.example.com`), `R2_PUBLIC_BUCKET` (limit fallback to a specific bucket)

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

#### Public GET fallback (no credentials on RunPod)
If your RunPod template cannot include S3/R2 HMAC credentials, you can still read inputs from R2 using a public HTTP base:

- Make your videos bucket (or a CDN in front) publicly readable for the input keyspace.
- Set `R2_PUBLIC_BASE_URL` to your public base, e.g. `https://videos.example.com` or `https://<bucket>.<account>.r2.cloudflarestorage.com`.
- Optionally set `R2_PUBLIC_BUCKET` to restrict fallback usage to a single bucket.

When the worker receives a `s3://<bucket>/<key>` input and no S3 credentials are configured, it will try `GET <R2_PUBLIC_BASE_URL>/<key>` automatically. If neither S3 nor public HTTP is available, it fails with `NO_PRESIGN_METHOD_AVAILABLE`.

### Keep Everything in One R2 Folder (Recommended)
To ensure RunPod only touches R2 paths, stage your inputs into a canonical folder before processing:

- Master audio → `s3://videos/pipelines/<job_id>/inputs/audio/master.m4a`
- Presenter image → `s3://videos/pipelines/<job_id>/inputs/image/presenter.png`

Use the `STAGE_OBJECT` task to copy from an HTTP source into R2:
```
{
  "input": {
    "task": "STAGE_OBJECT",
    "parameters": {
      "source_uri": "https://example.com/master.m4a",
      "dest_uri": "s3://videos/pipelines/job-123/inputs/audio/master.m4a"
    }
  }
}
```
Then use those staged keys for subsequent tasks (e.g., `AUDIO_TRIM`, `ENCODING`).

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
      "original_video_uri": "s3://videos/pipelines/job-123/inputs/video/source.mp4",
      "output_video_uri": "s3://videos/pipelines/job-123/stage/downsample/downsampled.mp4",
      "resolution": "360p"  // or 240, 360, 480 etc.
    }
  }
}
```

## Notes
- Public URL inputs are streamed to a temp file before processing.
- Outputs currently upload to S3/GCS; HTTP destinations aren’t supported.
- This build uses NVENC/NVDEC; choose a GPU-enabled Serverless runtime.
Audio trim (canonical videos path in a single bucket):
```
{
  "input": {
    "task": "AUDIO_TRIM",
    "parameters": {
      "source_uri": "s3://videos/pipelines/job-123/inputs/audio/master.m4a",
      "start_sec": 12.5,
      "duration_sec": 6.2,
      "output_video_uri": "s3://videos/pipelines/job-123/stage/segmentation/audio_segments/segment_0.wav"
    }
  }
}
```

Segmentation Orchestration (API route)
```
POST /api/pipeline/segment
{
  "job_id": "job-123",
  // Prefer pre-staged R2 URIs; fallback to HTTP staging if provided
  "master_audio_r2_uri": "s3://videos/pipelines/job-123/inputs/audio/master.m4a",  // optional if http provided
  "presenter_image_r2_uri": "s3://videos/pipelines/job-123/inputs/image/presenter.png", // optional
  "master_audio_http_url": "https://example.com/master.m4a",            // used only if master_audio_r2_uri omitted
  "presenter_image_http_url": "https://example.com/presenter.png",      // optional
  // Provide segments inline or via a manifest URI
  "segments": [                                                          // optional if segments_uri provided
    { "start_sec": 0, "duration_sec": 6.2 },
    { "start_sec": 6.2, "duration_sec": 5.8 }
  ],
  "segments_uri": "s3://videos/pipelines/job-123/stage/segmentation/segments.json" // or https://...
}
```

This endpoint stages master audio (and optional image) into `s3://videos/pipelines/<job_id>/inputs/...` and then triggers `AUDIO_TRIM` on RunPod for each segment, writing outputs to `s3://videos/pipelines/<job_id>/stage/segmentation/audio_segments/segment_{i}.wav`.

Environment required by the route:
- `RUNPOD_ENDPOINT` (default provided in code)
- `RUNPOD_API_KEY`
- `VIDEOS_BUCKET` (default `videos`)
- Optional for fetching `segments_uri` over HTTP when given as `s3://`: `VIDEOS_PUBLIC_BASE_URL` (or reuse `R2_PUBLIC_BASE_URL`) and `VIDEOS_PUBLIC_BUCKET` (scope)

Fully Presigned IO (no Runpod creds)
- Set these envs on this API (not RunPod):
  - `S3_ENDPOINT_URL`, `S3_REGION` (e.g., `auto`), `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`.
- The route will generate a presigned GET for the master audio key and a presigned PUT for each output segment, and call RunPod `AUDIO_TRIM` using those URLs. RunPod needs no S3 credentials.
- Ensure the master audio already exists in R2 under `s3://videos/pipelines/<job_id>/inputs/audio/master.*` or pass `master_audio_r2_uri`. The route will not stage via RunPod in fully‑presigned mode.

Segmentation Status (API route)
```
GET /api/pipeline/status?job_id=<job_id>&expected=<N>
```
- Checks existence of `segment_{i}.wav` under `s3://videos/pipelines/<job_id>/stage/segmentation/audio_segments/` using HTTP HEAD/GET against `VIDEOS_PUBLIC_BASE_URL`.
- Returns ready count and per-index availability. If `expected` is omitted, probes the first 64 indices.

Required env:
- `VIDEOS_PUBLIC_BASE_URL` (or `R2_PUBLIC_BASE_URL`)
- Optional: `VIDEOS_BUCKET` (defaults `videos`)
