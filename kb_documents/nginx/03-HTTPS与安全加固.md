---
title: Nginx HTTPS、TLS 加固与访问安全
domain: Nginx
level: 进阶
tags: [HTTPS, TLS, 证书, 安全加固, 限流]
last_updated: 2026-11-11
---

# Nginx HTTPS、TLS 加固与访问安全

> 一句话结论：HTTPS 不是“配个证书就完事”，而是**证书链、协议版本、密钥管理、重定向、安全响应头和访问控制**的组合；配置错误可能既不安全，也不可用。

## 1. 证书与私钥

Nginx 需要：

- `ssl_certificate`：服务器证书 + 中间证书（通常按“叶子证书 → 中间证书”顺序拼接）；
- `ssl_certificate_key`：私钥；
- 可选 `ssl_trusted_certificate`：用于 OCSP stapling 或客户端证书校验的信任链。

检查证书链：

```bash
openssl s_client -connect example.com:443 -servername example.com -showcerts
openssl x509 -in fullchain.pem -noout -subject -issuer -dates
```

要点：

- 只配叶子证书，部分客户端会因缺少中间证书而失败；
- 不要把根证书放进 `ssl_certificate`，增加握手体积；
- 私钥权限最小化（如 `600 root:root`），不要提交到代码仓库；
- 证书和私钥通过 Secret Manager、KMS 或挂载卷管理，定期轮换；
- 证书到期监控必须独立于 Nginx 自身，避免“监控也挂了”。

## 2. 基础 HTTPS 配置

```nginx
server {
    listen 80;
    server_name example.com www.example.com;
    return 301 https://example.com$request_uri;
}

server {
    listen 443 ssl;
    http2 on;
    server_name example.com www.example.com;

    ssl_certificate     /etc/nginx/certs/fullchain.pem;
    ssl_certificate_key /etc/nginx/certs/privkey.pem;

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers 'ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305';
    ssl_prefer_server_ciphers off;

    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 1d;
    ssl_session_tickets off;

    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    location / {
        proxy_pass http://app_backend;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

说明：

- `http2 on;` 适用于较新版本；旧版本使用 `listen 443 ssl http2;`，该写法在新版本中已弃用；
- TLS 1.0/1.1 已不应启用；
- `ssl_ciphers` 主要控制 TLS 1.2 及以下；TLS 1.3 的套件由 OpenSSL 配置决定；
- `ssl_prefer_server_ciphers off` 让客户端选择，通常兼容性更好；合规场景可按要求调整；
- `ssl_session_tickets off` 提升前向安全性，但会增加握手；多台 Nginx 需要共享 ticket key 才能跨节点恢复会话；
- HSTS 只应通过 HTTPS 响应下发，且确认所有子域都支持 HTTPS 后再加 `includeSubDomains`/`preload`。

## 3. 重定向的细节

```nginx
server {
    listen 80;
    server_name example.com;
    return 301 https://example.com$request_uri;
}
```

注意：

- 使用规范域名，避免 `$host` 被恶意 Host 头利用造成开放重定向；
- `$request_uri` 保留原始路径和查询串；
- 多域名站点要分别配置或使用 `map` 做白名单；
- 不要在 HTTPS server 内再写“如果 $scheme = http 就跳转”的复杂逻辑，直接用独立 80 server 更清晰；
- 反向代理后面的应用如果自己生成绝对 URL，需正确传递 `X-Forwarded-Proto`，否则可能循环重定向。

## 4. OCSP Stapling

```nginx
ssl_stapling on;
ssl_stapling_verify on;
ssl_trusted_certificate /etc/nginx/certs/chain.pem;
resolver 1.1.1.1 8.8.8.8 valid=300s;
resolver_timeout 5s;
```

- `ssl_trusted_certificate` 应是签发链，不是叶子证书；
- 需要能访问 CA 的 OCSP 响应器；
- 部分 CA 已调整或停止 OCSP 服务，配置前确认现状；
- 如果 OCSP 不可用，Nginx 可能不回 stapling，但不一定影响握手（取决于客户端策略）。

## 5. 安全响应头

```nginx
add_header X-Content-Type-Options "nosniff" always;
add_header X-Frame-Options "SAMEORIGIN" always;
add_header Referrer-Policy "strict-origin-when-cross-origin" always;
add_header Permissions-Policy "geolocation=(), microphone=(), camera=()" always;
add_header Content-Security-Policy "default-src 'self'; object-src 'none'; base-uri 'self'" always;
```

注意：

- `X-Frame-Options` 已被 CSP `frame-ancestors` 逐步取代，但保留可兼容旧客户端；
- CSP 必须结合前端资源、内联脚本、第三方域名设计，盲目启用会破坏页面；
- `always` 让错误响应也带上头；
- **`add_header` 继承规则**：如果当前层级定义了任何 `add_header`，父层的 `add_header` 不会自动继承。常见做法是把安全头放到 `include` 片段中，在每个需要的层级显式引入。

隐藏版本号：

```nginx
server_tokens off;
```

这不会删除 `Server` 头，只是去掉版本号。若要完全移除，需要 `headers-more` 等第三方模块，且不要把它当作主要安全措施。

## 6. 访问控制与认证

### 6.1 IP 白名单

```nginx
location /admin/ {
    allow 10.0.0.0/8;
    allow 192.168.1.0/24;
    deny all;

    proxy_pass http://admin_backend;
}
```

如果 Nginx 前面还有代理，必须正确配置 `real_ip`，否则 `allow/deny` 看到的是代理 IP。

### 6.2 Basic Auth

```nginx
location /staging/ {
    auth_basic "Restricted";
    auth_basic_user_file /etc/nginx/.htpasswd;
    proxy_pass http://staging_backend;
}
```

适合临时保护测试环境，不适合作为正式的用户认证体系；务必配合 HTTPS。

### 6.3 客户端证书（mTLS）

```nginx
ssl_client_certificate /etc/nginx/certs/client-ca.pem;
ssl_verify_client on;
ssl_verify_depth 2;
```

后端可通过 `$ssl_client_verify`、`$ssl_client_s_dn` 获取认证信息。需要设计证书签发、吊销（CRL/OCSP）、轮换和过期告警。

## 7. 限流与连接控制

```nginx
http {
    limit_req_zone  $binary_remote_addr zone=api:10m rate=10r/s;
    limit_conn_zone $binary_remote_addr zone=perip:10m;

    server {
        location /api/ {
            limit_req zone=api burst=20 nodelay;
            limit_conn perip 20;
            limit_req_status 429;
            limit_conn_status 429;

            proxy_pass http://app_backend;
        }
    }
}
```

- `rate=10r/s` 是平均速率，`burst` 允许突发；
- `nodelay` 立即处理突发请求，不排队；不使用时请求会延迟排队；
- `limit_conn` 限制并发连接数，防止慢连接占满 worker；
- 共享内存 `zone` 大小决定能记录多少 IP；
- 限流应结合业务返回码与重试策略，避免客户端疯狂重试放大压力。

还应设置：

```nginx
client_max_body_size 10m;
client_body_timeout 15s;
client_header_timeout 10s;
keepalive_timeout 30s;
send_timeout 30s;
```

防止慢速请求耗尽连接资源。

## 8. 反向代理安全

```nginx
proxy_set_header Host              $host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;

proxy_hide_header X-Powered-By;
proxy_hide_header Server;
```

注意：

- 不要盲目信任客户端传入的 `X-Forwarded-For`；只接受可信代理追加的值；
- 后端若直接暴露公网，攻击者可绕过 Nginx 的访问控制；
- 禁止将 Nginx 配置为开放正向代理（例如 `proxy_pass $scheme://$http_host` 这类变量拼接）；
- 限制 `proxy_pass` 的目标，避免 SSRF；
- 对上传目录关闭脚本执行权限；
- 静态文件使用 `try_files` 防止路径穿越。

## 9. TLS 验证与监控

```bash
# 检查协议与证书
openssl s_client -connect example.com:443 -servername example.com -tls1_2
openssl s_client -connect example.com:443 -servername example.com -tls1_3

# 检查 HTTP/2
curl -I --http2 https://example.com

# 检查 HSTS
curl -sI https://example.com | grep -i strict-transport-security

# 证书到期时间
openssl s_client -connect example.com:443 -servername example.com 2>/dev/null \
  | openssl x509 -noout -dates
```

监控项：

- 证书剩余有效期（提前 30/14/7 天告警）；
- TLS 握手失败率；
- 协议版本分布（是否仍有 TLS 1.0/1.1）；
- 4xx/5xx、429 比例；
- OCSP stapling 状态；
- 私钥/证书文件变更审计。

## 10. 证书自动化（Let's Encrypt 示例）

```bash
# webroot 方式，HTTP-01 验证
certbot certonly --webroot \
  -w /var/www/certbot \
  -d example.com -d www.example.com

# 部署后校验并平滑重载
nginx -t && nginx -s reload
```

建议：

- 先用 staging 环境验证；
- 续期使用 systemd timer/cron，并监控失败；
- 续期后自动 `nginx -t` 再 reload，失败则保留旧证书；
- 私钥权限、ACL、备份策略明确；
- 不要把 ACME challenge 路径交给后端应用。

## 11. 小结

TLS 加固的核心是**正确证书链 + 现代协议 + 安全密钥管理 + 明确的访问边界**。安全头、限流、mTLS 都是叠加层，不能替代应用自身的认证授权。上线后用外部扫描、协议测试和证书到期监控验证，而不是仅凭 `nginx -t` 通过就认为安全。
