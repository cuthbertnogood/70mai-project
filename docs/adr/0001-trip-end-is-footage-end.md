# Trip.end is end of footage

Compose and Merge readiness use a Trip wall window `[start, end]`. Setting `end` to the last clip's filename timestamp (clip start) made single-clip trips have a zero-length window and multi-clip trips drop the last clip's duration — so compose failed with `timeline covers only Xs of Ys` even when merges were present. We define Trip.end as last clip start + last clip duration (fallback: start + duration_sec), and Merge readiness for Normal uses the same 98% aligned-duration check as compose.
