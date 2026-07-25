# Service OIDC 金鑰輪替

Remote worker/sidecar 只掛載 public JWKS；只有 core runner 可讀取 RS256 private
key。每個 receiver 使用不同 audience，token 綁定 permission、receiver、單一 target、
attempt generation/nonce、canonical request hash 與 deadline。

## 正常輪替

1. 產生至少 RSA-2048 的新金鑰 `K2`。Private key 必須是 regular file、不可為
   symlink，Unix 權限為 `0600`；不得放入 image、repo、log 或 remote runtime。
2. 將 `K2` public JWK 加入 mounted JWKS，保留舊 `K1`，確認 `kid` 唯一且
   `use=sig`、`alg=RS256`。
3. 對六個 receiver 逐一 rolling restart。Verifier 只在 startup 載入 JWKS；單純
   替換檔案不會讓既有 process reload。
4. 以 `K1` 與 `K2` 對每個 receiver 發出 exact-target probe；wrong audience、target、
   nonce 或 payload 必須維持 403/401。
5. 將 core signer 切換成 `K2`，停止簽發 `K1`。
6. 等待「設定的最大 token lifetime + clock skew」完整經過，確認沒有 `K1`
   dispatch，再從 JWKS 移除 `K1`。
7. 再次 rolling restart 全部 receiver，確認 `K1` 被拒絕、`K2` 正常，並封存輪替
   audit evidence。

任一步驟失敗時，停止新 dispatch、保留 overlap JWKS、將 signer 回切仍受信任的
key，完成 receiver 健康檢查後才恢復 queue consumption。

## 私鑰疑似外洩

立即停止 core dispatch 與 issuer、隔離內部 worker network、建立新 key/audience
組合並重啟所有 verifier。Static verifier 在重啟前仍信任 startup 時載入的 key，
因此不得只更新 JWKS 檔。完成最大 token lifetime + skew 的隔離窗後移除舊 key，
檢查 duplicate compute、stale generation/nonce 與 core completion fencing audit；
remote result 永遠不能直接 commit DB、建立 order 或 override risk。

Service bearer 只能在 private/internal network 傳送。目前 repository 尚未驗證跨主機
TLS/mTLS、orchestrator network policy 或 public ingress；因此現有證據只允許 single-host
internal transport。未具備並驗證加密 transport 時不得開放 worker ingress 到外部網路。
