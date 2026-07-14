# QuantConnect LEAN sidecar notice

This optional image contains a modified build of QuantConnect LEAN tag `17917`,
commit `c22774e49ee80ecef5ca84f57616f6b66fad8bc5`, licensed under Apache-2.0.
LEAN source files identify Copyright 2014 QuantConnect Corporation. The pinned
upstream source is available at:

https://github.com/QuantConnect/Lean/tree/c22774e49ee80ecef5ca84f57616f6b66fad8bc5

The image includes the exact verified upstream archive at
`/usr/share/source/lean/lean-source.tar.gz`, its full license at
`/usr/share/licenses/stonks-lean-sidecar/LEAN-APACHE-2.0`, and all Stonks
patch/adapter sources at `/usr/share/source/lean/stonks-modifications`.
`distribution-manifest.yaml` records source, base image, lock graph and
modification SHA-256 values.

Stonks removes unused/vulnerable DotNetZip and NetMQ dependency paths from the
backtest-only build. A bounded clean-room `System.IO.Compression` compatibility
layer replaces the small Ionic.Zip surface used by LEAN. The separate fixed
algorithm accepts only generated canonical schedules and writes authority-free
fill traces. These changes are not endorsed by QuantConnect.

The Python adapter and clean-room modification files are Apache-2.0. The process
boundary provides dependency and authority isolation; it does not remove
Apache-2.0 attribution, notice, source-marking, or other license obligations.
