# 70mai publish

Dashcam SD → compose → YouTube publish pipeline.

## Language

### Footage / planning

**Trip**:
A contiguous recording session for one record type (Normal/Event/Parking), bounded by session gap. `start` is the wall time of the first clip; `end` is the wall time when footage stops (last clip start + its duration), not the last clip's filename timestamp.
_Avoid_: session (unqualified), chunk (when meaning one drive segment)

**Trip coverage**:
How much of a Trip's planned duration is present on SSD merges/timeline for that wall window. Compose and Merge readiness use the same threshold (98%).
_Avoid_: merges ready (without a coverage number)

**Merge readiness**:
Whether Front+Back SSD merges cover every Trip in a Chunk at Trip coverage ≥ 98%. If not, import for that window is required before compose.
_Avoid_: skip import, merges exist

**Chunk**:
One ~target-length YouTube upload unit packed from one or more Trips (or one mega-file for Event/Parking).
_Avoid_: part (when meaning the logical upload unit rather than `part_NN.mp4`)

### Publish / network

**Upload Exit**:
The outbound network path used for YouTube Data API uploads from the machine that holds the composed MP4.
_Avoid_: VPN (when meaning the general-purpose client tunnel), proxy (unqualified)

**Proxy exit**:
An Upload Exit where the MP4 stays on the Mac and HTTPS to Google is forwarded through a remote host (e.g. SSH SOCKS or WireGuard on a FI VPS).
_Avoid_: upload via VPS, VPN upload

**Relay**:
An Upload Exit where the MP4 is copied to a remote host and the YouTube upload process runs there.
_Avoid_: upload via VPS, remote upload (unqualified)

**Pipeline host**:
A remote machine that runs compose and upload (not only the Upload Exit).
_Avoid_: VPS upload server
