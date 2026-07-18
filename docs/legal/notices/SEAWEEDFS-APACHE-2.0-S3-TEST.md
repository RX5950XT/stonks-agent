# SeaweedFS Apache-2.0 S3 Test Runtime

P6.6 的 S3-compatible integration smoke 使用未修改的 SeaweedFS 4.34
container image：

- Repository：https://github.com/seaweedfs/seaweedfs
- Source commit：`c6cf5a5bd7c87694c8d71ab41571f1412170ab2a`
- Image：`chrislusf/seaweedfs:4.34`
- Image digest：`sha256:6620371e8af8282056685c652d4637265698c9e2c2d59f9594e6ac333ad6c634`
- License：Apache-2.0
- Upstream LICENSE SHA-256：`d789d433cc11da163273d1e39be2e8fa67642f9a58ef220d3f258fa9c14ef613`

本專案不複製或修改 SeaweedFS source。Smoke image 只用於測試
SigV4、conditional PUT、checksum/metadata round trip 與短效 presigned GET；
不作為 production artifact backend 發布，也不代表 AWS KMS、IAM、Object Lock
或所有 S3 vendor 的相容性。Apache License 2.0 全文亦收錄於專案根目錄
`LICENSE`。
