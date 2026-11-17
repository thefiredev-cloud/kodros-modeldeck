# Public research and feature provenance

Checked 2026-10-21 before implementation with `gh search repos --owner 0xSero --limit 50 --json name,description,url`, `gh repo view` and the GitHub content API. No upstream code, art or logo is copied. This is an independent implementation of interaction ideas.

| ModelDeck feature | Public source and what was actually read | Relationship |
|---|---|---|
| Fleet home, model list and hardware cards | https://github.com/0xSero/omarchy-local-ai#what-it-does and https://github.com/0xSero/omarchy-local-ai/blob/main/docs/design.md | Reimplements home/models/hardware separation and a searchable model catalogue. Extends one-machine model management to five named fleet hosts. |
| Model detail with discovered ID, context and endpoint | https://github.com/0xSero/harness-bridge#web-ui and https://github.com/0xSero/harness-bridge#dialects | Reimplements endpoint-first model discovery and detail selection. Does not copy harness launch or permission-bypass defaults. |
| Persistent measured history, honest load state | https://github.com/0xSero/omarchy-local-ai/blob/main/docs/design.md | Copies the principle that status and charts come from actual engine/log observations, not a clock-driven progress bar. Uses telemetry/Prometheus/SQLite rather than upstream gateway logs. |
| Quiet dark visual reference | https://github.com/0xSero/omarchy-local-ai/blob/main/preview.png | Inspected the 1600×900 public image. It is a promotional image, not a dashboard screenshot. Only its restrained dark palette informs this interface. No promotional artwork or claims ship. |
| Hardware-specific recipes and explicit evidence | https://github.com/0xSero/DeepSeek-V4.1-Flash-Two-Sparks#measured and #image-publication-and-acceptance | Reimplements separating a ready engine from accepted gates, model context and measured timing. Does not import its Docker recipes, images, claimed speed or 11–13 minute cold-start estimate. |
| Two-step cluster hot-swap review and measured outage history | Fleet-specific extension, informed by the previous recipes/evidence pattern | Not claimed as a feature authored by Sero. Uses only the pre-existing cluster `swap-gate swap` helper, never a new engine control path or raw `/swap` passthrough. The ~3.5 minute estimate is a dated measurement from one fleet, not a current observation. |

All three repository license metadata reads returned MIT. This repository contains no copied upstream code, so no redistribution notice is required. Upstream model weights and third-party kernels have separate licenses; this app distributes none.

Public X posts by the same author were read only for commentary. They are author-reported and are not benchmark evidence. No X speed or SOTA claims enter the app.
