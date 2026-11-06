# Local history consolidation plan

**Goal:** Build a small, meaningful local candidate history covering all current development, preserving original branches and the exact final tracked tree.

**Design:** Reuse the Git trees at reviewed semantic range endpoints. Create signed single-parent commits with explicit source-range/tree provenance, then atomically create one absent `consolidated/*` branch. Keep the controller checkout, index, local main and remote refs unchanged. This is a local candidate, not main approval or a release.

**Alternatives:** Replaying patches requires historical checkout/conflict handling; rewriting main directly adds unnecessary risk. The user selected local consolidation and allows a separate branch. Snapshot commits on a fresh branch provide the same final content with less work.

**Constraints:** Existing Git identity/signing and path policy; no new external dependency; no force/reset/remote operation; original source branch stays available. Ordinary commit hooks are not run by commit-tree: candidate creation does not attest quality or authorize main. Validate every selected snapshot in an isolated local clone before adopting it.

## Tasks
- [ ] Add real-Git acceptance tests for exact trees/modes/binary/deletions/assessments, preserved original refs/index, invalid ranges, stale state and existing destinations.
- [ ] Add a bounded consolidation tool using up to 16 reviewed endpoints over at most 64 linear commits, full SHA inputs and an absent destination.
- [ ] Verify every created parent/tree and configured signature before publishing the candidate ref; use compare-and-create and reobserve after uncertain failures.
- [ ] Register/document the tool; run focused tests, configured quality checks and regression profiles; commit the prerequisite.
- [ ] Use a local in-process MCP client with the project startup policy to consolidate, then validate the actual candidate in an isolated clone. External ChatGPT discovery is not required for local reconstruction and remains unverified.

## Initial grouping to review
Base: `2ae0de2d520725b17355d0f2fca90dd64240a11d`.
1. Development foundation through `f5f8d5cbb355b68fc79040fd96de86f9d0e5d870`.
2. Event automation through `1d3996acb572c65dc70c741c9dd6f11720f73643`.
3. Assessment/release/native workflow through `bc5cc7ac934a8ed9980c0174b53f63b8617cdb18`.
4. Autonomous agent workspace/publication support through the latest committed source, including this prerequisite.

Review changed paths and cumulative tests before accepting these boundaries. Preserve all committed .changes blobs exactly; do not synthesize PR provenance. The source range trailers identify original history even without PRs.
