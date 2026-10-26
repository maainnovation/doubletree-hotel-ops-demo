# Owner-review pilot: image and resource gate

This is a separate, **initially disabled** OCR-enabled image. It does not change
the native-PDF rollback `Dockerfile.pilot`, authorize additional recipients,
create credentials, upgrade hosting, or enable sending. The owner-only rollout
remains limited to its approved 10 days / 500 outgoing messages / monitored $10
budget; partners remain disabled. Configuration and scope checks are owned by
the review runtime, not by this benchmark.

## Build contract (not yet a verified image)

Use `Dockerfile.review-pilot`, with `scripts.run_twilio_review_pilot` as the
supervisor. It installs Tesseract English, libseccomp2, DejaVu and Liberation
fonts. Direct Python dependencies add the locally tested PyMuPDF 2.0 and
pytesseract 0.3.13 to the pinned native pilot requirements. The intended Python
version is 3.12.14, with Debian Trixie and the tested Tesseract 5.5.0 engine
(build checks reject a different engine version). The image runs as UID/GID 1000, copies only `api`, `services`,
and `scripts`, and leaves `/app` root-owned and non-writable. Build context
exclusions must remain in force. A mounted private disk at `/var/data` must be
writable by UID 1000 and inaccessible to other users; never mount it over source.

Required build verification before staging:

1. Build the new file independently; record the resulting image digest, base
   digest, apt package versions and resolved transitive Python dependencies.
2. Run `pip check`, Tesseract English-language discovery, and libseccomp loading.
3. Check nonroot startup, source write denial, private-disk permissions, and
   disabled health responses. No account/provider setup is part of this step.
4. Run the complete regression suite in the image's resolved dependency set.

The repository supplies no promise that the base tag, apt resolution or image
build has already been verified. The local executor did not have Docker. No
image or deployment was published by the resource implementation task.
PyMuPDF 2.0 declares ONNX/layout integrations as package dependencies even though
the isolated worker disables their unused imports. Do not bypass dependency
checks with `--no-deps`; record the full resolved image size. The image filesystem
and the separately mounted 1 GiB private data disk are different resource budgets.

## Mandatory concurrency and memory contract

- Exactly **one API process**, no replicas. Upload admission takes a nonwaiting
  lock before reading the body; concurrent intake returns 429. Keep that lock
  through persistence and buffer release. Byte limits are 10 MiB PDF / 5 MiB
  PNG or JPEG; the OCR adapter accepts photos up to 12 megapixels and renders
  working images at no more than 4 megapixels / 2800 pixels on the longest edge.
- Exactly **one worker** owns a protected lifetime `flock` on
  `.review-worker.lock`, opened with `O_NOFOLLOW` and checked as a singly linked
  regular private file. A second worker must refuse startup. WhatsApp and app
  jobs run sequentially; do not start a second OCR task.
- The API may hold one upload while that worker processes a different report.
  The probe retains 20 MiB for chunks plus a joined maximum upload. Its local
  mode additionally keeps an API-resident copy, intentionally conservative.
- Construct a new `CgroupMemoryGuard` for each assessment and pass it to
  `evaluate_forecast_media_isolated(..., memory_guard=guard)`. The guard requires
  readable cgroup v2 evidence and a finite memory limit no greater than 512 MiB,
  at least 192 MiB free before request serialization and spawn, at least 64 MiB
  during evaluation, and unchanged OOM counters. Missing evidence denies work.
- The isolated worker retains the **384 MiB per-process address-space cap**,
  35-second wall deadline, sanitized environment, inherited network denial,
  bounded output and process-group cleanup. The guard polls every 50 ms while
  waiting and checks after exit. A refusal is an explicit resource failure,
  never an automatic OCR retry or automatic schedule acceptance.

Example integration (no credentials involved):

```python
from scripts.measure_review_runtime import CgroupMemoryGuard
from services.forecast_media_worker import evaluate_forecast_media_isolated

assessment = evaluate_forecast_media_isolated(
    data, mime, hotel_code, capacity, now,
    report_profile=profile, hotel_name=hotel_name,
    memory_guard=CgroupMemoryGuard(),
)
```

**This is a watchdog, not a hard descendant memory partition.** A sudden native
allocation can outrun polling. The 384 MiB cap applies independently to the
media worker and Tesseract; summing RSS or quoting that cap cannot prove a 512
MiB service safe. The host cgroup remains the hard ceiling. An OOM/restart must
fail closed, preserve durable job state and uncertain-send recovery, and must
not silently resend or overwrite a roster. Native PDF complexity, compressed
image expansion and allocator behavior remain reasons to reject an input.

## Explicit synthetic measurement

Default invocation does nothing. Local diagnostic:

```sh
python -m scripts.measure_review_runtime --run-synthetic --mode local
```

After an independently approved **disabled** staging deployment, run inside the
existing 512 MiB / 0.5 CPU service:

```sh
python -m scripts.measure_review_runtime --run-synthetic --mode attached-disabled
```

The script never calls a provider, sends messages, performs HTTP requests,
reads user documents or inherits credentials into its children. Child network
syscalls are denied. All fixtures are generated fictional reports in a private
temporary directory and removed after the run. Four representative cases cover
10 MiB native PDF, 10 MiB scanned PDF, and 5 MiB PNG/JPEG with 12-megapixel source
images. Padding fills the byte limits without introducing a second report.

Attached mode observes exactly one actual API and worker in its own cgroup,
checking PID plus start-time identity throughout and after the probe; a lost or
restarted service blocks qualification rather than reducing the baseline. It adds a
separate imported worker harness and upload buffers to the already resident
service, so this overhead is included rather than subtracted. It samples
whole-cgroup memory every 20 ms, requires readable kernel historical peak for a staging pass,
CPU quota, OOM counter changes and temporary-space headroom. Historical peaks
are **not reset**; a previously high peak conservatively blocks qualification.
Process-tree RSS is also reported but is explicitly diagnostic: it can double
count shared pages and omits page cache. It is never used as aggregate proof.

A measured-stage pass requires all four cases to finish, actual staged service
processes, exactly 512 MiB cgroup limit, no more than 0.5 CPU, at least 64 MiB
aggregate measured margin, no OOM increments, and at least 128 MiB free temporary
space. Fixture generation additionally requires 256 MiB free space beforehand,
and the sampler stops work if less than 128 MiB remains. Missing cgroup evidence, a local-only run, failed/unsupported cases or
insufficient margin produce exit code 2, not a fabricated green result. A
margin breach stops the running synthetic process tree and no case is retried.
This representative pass is necessary, not sufficient proof for every hostile
input or final production authorization.

If the disabled-stage probe cannot pass, **do not activate or automatically
upgrade the hosting tier**. Report the failing case and decide whether to
restrict supported input sizes/complexity or seek separate cost approval.

### Local evidence, 2026-10-26 UTC

The final local command using the actual new API/worker import modules completed
all four maximum-byte cases in about 7.5 seconds on this executor. The native
PDF retained seven valid days; scan/PNG/JPEG each returned both 49-cell OCR
transcriptions with 56 numeric/date tokens, always human-review-required.
The fixture uses clearly legible 64-pixel synthetic source text; this is not a
general OCR-accuracy claim.

Sampled **summed process RSS** peaked at 388,636,672 bytes (370.6 MiB), including
the resident API import, worker harness, isolated OCR/Tesseract descendants and
upload buffers. PPID-stat traversal was necessary because this executor omits
the optional `/proc/PID/task/PID/children` files. This figure double-counts shared
pages and excludes page cache; it is diagnostic, **not aggregate 512 MiB proof**.
The executor exposes neither the required cgroup counters nor a verified 0.5-CPU
quota. The probe therefore correctly returned exit code 2 and
`measured_stage_gate_passed: false`. The disabled Render measurement and actual
Docker-image regression remain required; no resource activation gate is waived.

## Authoritative references

- [Dockerfile reference](https://docs.docker.com/reference/dockerfile/) documents
  image copy, user and command semantics.
- [Linux cgroup v2 documentation](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html)
  defines aggregate memory controls/counters and hierarchical CPU limits.
- [Debian Trixie Tesseract package](https://packages.debian.org/trixie/tesseract-ocr)
  supplies the 5.5.0 engine line used by the local OCR regression evidence;
  the older Bookworm package is not the tested engine.
