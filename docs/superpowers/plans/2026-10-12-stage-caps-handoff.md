# Stage caps and dictionary-to-important handoff

Goal: preserve the running frozen dictionary pipeline, calibrate after its whole-track summary, and start a fresh important-only run with calibrated stage limits.

Approved intent: stage-specific concurrency; 64 front and 128 dictionary extraction are Codex candidate values, not user-provided optimal values. Later user steering requires dictionary completion -> calibration -> important books, rather than waiting for both tracks.

Constraints:
- Do not edit the existing NAS RUN/code, inputs, checkpoints or dictionary outputs.
- Never signal the old controller before a valid dictionary/SUMMARY.json from this launch exists.
- Verify PID start time, argv, UID, session and process group before any signal. Retire only that launch after its dictionary stage is complete.
- Preserve the interrupted old important preparation, if any; never reuse it as a completed run.
- New roots, source snapshots, provenance and model profiles for calibration and important execution.
- Front: candidate64, retries16/4, one model request worker per book, bounded global CPU preparation8.
- Dictionary extraction: candidate128, retries32/8, eight jobs with16/4/1 workers per job.
- Cleaning, boundary and mounting use an independent conservative model budget; important N1/N3 extraction remains CPU/rule based.
- Complete each eligible cohort round before its globally reduced retries; successful checkpoints are not repeated.
- Offline tests may use literal loopback fake services only. Real calibration cannot run until the boundary guard has retired the old launch and drained its gateway.

Workspace ruling: reuse the existing independent review checkout on codex/dictionary-routing-cleaning. Production uses a separate immutable NAS snapshot. Preserve the dirty baseline; no commit or push is needed for this delivery.

Tasks:
- [ ] Add meaningful failing tests for phase limits, bounded front preparation and real front parallelism, strict PDF preparation, important-only execution and shared retry barriers.
- [ ] Implement configurable per-stage limits and separate front CPU/model pools; update documentation and launch examples.
- [ ] Add boundary handoff tests (absent/invalid summary, PID reuse, wrong session, unrelated child, protected source hashes, real isolated process group) before implementing a background handoff helper.
- [ ] Add bounded calibration tests (fixed packet population, bad/truncated response handling, cap choice, no stable level, body/context bounds, trace/provenance) before implementing the post-dictionary calibration.
- [ ] Run targeted and Linux integration/native regressions, including observed fake-upstream64/16/4 and128/32/8 caps; obtain one independent review and fix material findings.
- [ ] Freeze and upload the tested successor code to a new root; register and detach the boundary guard. Verify it is waiting, zero calibration requests, unchanged old source hashes and active dictionary controller.

Completion: code/test evidence, fresh successor source snapshot, guardian launch receipt and a concrete post-dictionary calibration -> important-only command chain. Real throughput, selected caps and semantic acceptance remain pending until their actual runs finish.
