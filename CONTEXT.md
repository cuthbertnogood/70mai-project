# 70mai publish

Dashcam SD → compose → YouTube publish pipeline.

## Language

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
