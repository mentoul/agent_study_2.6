---
title: Kubernetes 发布与流量：Deployment、Service、Ingress
domain: Kubernetes
level: 进阶
tags: [Deployment, Service, Ingress, 滚动更新, 零停机]
last_updated: 2026-11-11
---

# Kubernetes 发布与流量：Deployment、Service、Ingress

> 一句话结论：Deployment 管“副本如何滚动”，Service 管“稳定虚拟 IP 与四层负载均衡”，Ingress 管“七层路由”。三层各司其职，任何一层配置错位都会表现为“服务不可达”。

## 1. Deployment：无状态应用的标准控制器

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
  namespace: prod
spec:
  replicas: 3
  revisionHistoryLimit: 5
  progressDeadlineSeconds: 600
  minReadySeconds: 10
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  selector:
    matchLabels:
      app.kubernetes.io/name: web
  template:
    metadata:
      labels:
        app.kubernetes.io/name: web
    spec:
      containers:
        - name: web
          image: registry.example.com/web:1.2.3
          ports:
            - name: http
              containerPort: 8080
          readinessProbe:
            httpGet:
              path: /readyz
              port: http
            periodSeconds: 5
            failureThreshold: 3
          livenessProbe:
            httpGet:
              path: /healthz
              port: http
            periodSeconds: 10
            failureThreshold: 3
          resources:
            requests:
              cpu: 200m
              memory: 256Mi
            limits:
              cpu: "1"
              memory: 512Mi
```

关键字段：

| 字段 | 作用 |
| --- | --- |
| `replicas` | 期望副本数，HPA 可接管 |
| `maxSurge` | 滚动更新时最多超出期望副本数多少 |
| `maxUnavailable` | 滚动更新时最多有多少副本不可用 |
| `minReadySeconds` | Pod 就绪后需稳定多久才算可用，避免“刚启动就接流量” |
| `progressDeadlineSeconds` | 超过该时间未取得进展则标记失败 |
| `revisionHistoryLimit` | 保留多少历史 ReplicaSet，便于回滚 |
| `selector` | 必须匹配模板 labels，且创建后通常不可修改 |

### 1.1 滚动更新过程

```text
旧 ReplicaSet（3） ──► 新 ReplicaSet（0）
      │                     │
      ├─ 创建新 Pod（maxSurge）
      ├─ 等待新 Pod Ready + minReadySeconds
      ├─ 缩容旧 Pod（受 maxUnavailable 限制）
      └─ 重复直到完成
```

`maxUnavailable: 0` + `maxSurge: 1` 是常见的“先起新、再停旧”策略，适合不能降容量的服务，但需要集群有额外资源。

### 1.2 常用命令

```bash
kubectl rollout status deployment/web -n prod
kubectl rollout history deployment/web -n prod
kubectl rollout undo deployment/web -n prod
kubectl rollout undo deployment/web -n prod --to-revision=3
kubectl rollout restart deployment/web -n prod

kubectl scale deployment/web --replicas=5 -n prod
kubectl set image deployment/web web=registry.example.com/web:1.2.4 -n prod
```

注意：

- 修改 Pod 模板（镜像、环境变量、资源等）才会触发滚动更新；仅改 `replicas` 不触发；
- 修改 Service 的 selector 不会触发 Deployment 更新，但会立即改变流量去向；
- `kubectl rollout undo` 回滚的是 Pod 模板，不会回滚数据库迁移或外部配置；
- 回滚前确认旧镜像仍可拉取、旧版本兼容当前数据格式。

## 2. Service：稳定的四层入口

Pod IP 会变化，Service 提供稳定虚拟 IP 和 DNS 名称。

```yaml
apiVersion: v1
kind: Service
metadata:
  name: web
  namespace: prod
spec:
  type: ClusterIP
  selector:
    app.kubernetes.io/name: web
  ports:
    - name: http
      port: 80
      targetPort: http   # 对应容器 named port
      protocol: TCP
```

### 2.1 四种类型

| 类型 | 说明 | 典型用途 |
| --- | --- | --- |
| `ClusterIP` | 集群内虚拟 IP | 内部服务调用 |
| `NodePort` | 每个节点开放一个端口 | 开发/裸金属入口 |
| `LoadBalancer` | 请求云厂商/负载均衡器 | 公网或外部入口 |
| `ExternalName` | DNS CNAME，无代理 | 指向外部域名 |

`NodePort` 会占用节点端口（默认 30000–32767），并可能暴露到所有节点；生产公网入口通常用 `LoadBalancer` 或 Ingress。

### 2.2 端口关系

```text
客户端 → Service port → targetPort（Pod 容器端口）
```

- `port`：Service 暴露的端口；
- `targetPort`：Pod 上的端口，可以是数字或命名端口；
- `nodePort`：NodePort 类型在节点上开放的端口；
- 命名端口要求 Pod 中 `containerPort` 也声明同名 `name`。

```bash
kubectl get svc web -n prod
kubectl get endpointslices -n prod -l kubernetes.io/service-name=web
kubectl describe svc web -n prod
```

### 2.3 为什么 Service 没有 Endpoint？

常见原因：

1. selector 与 Pod labels 不匹配；
2. Pod 未 Ready（readinessProbe 失败）；
3. Pod 处于 `Terminating` 或已删除；
4. 端口名/协议不一致；
5. EndpointSlice 控制器异常；
6. 使用了 `publishNotReadyAddresses` 的错误预期。

```bash
kubectl get pods -n prod --show-labels
kubectl get endpointslices -n prod -o yaml
kubectl describe pod <pod> -n prod
```

### 2.4 Headless Service

```yaml
spec:
  clusterIP: None
  selector:
    app.kubernetes.io/name: db
  ports:
    - port: 5432
```

Headless Service 不分配虚拟 IP，DNS 直接返回 Pod IP，适合：

- StatefulSet 的稳定网络标识；
- 客户端自行做负载均衡/服务发现；
- 需要访问每个副本的场景。

### 2.5 会话保持与流量策略

```yaml
spec:
  sessionAffinity: ClientIP
  sessionAffinityConfig:
    clientIP:
      timeoutSeconds: 10800
```

- `ClientIP` 粒度粗，NAT 后大量用户共享 IP；
- 更好的会话保持通常在应用层（Cookie/Token）或 Ingress/网关实现；
- `externalTrafficPolicy: Local` 只把外部流量转到本节点有 Pod 的节点，可保留客户端 IP，但节点无本地 Endpoint 时会丢流量；
- `internalTrafficPolicy: Local` 对集群内流量类似，需评估高可用。

## 3. Ingress：七层路由

Ingress 只是路由规则，必须有一个 **Ingress Controller** 实际监听入口并生成代理配置。仅创建 Ingress 资源而没有控制器，不会产生任何效果。

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: web
  namespace: prod
spec:
  ingressClassName: nginx
  tls:
    - hosts:
        - www.example.com
      secretName: example-tls
  rules:
    - host: www.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: web
                port:
                  name: http
```

### 3.1 关键字段

- `ingressClassName`：选择控制器；集群可能有多个 Ingress Controller；
- `rules.host`：域名匹配，支持精确和通配符（取决于控制器）；
- `pathType`：
  - `Exact`：精确匹配；
  - `Prefix`：按路径段前缀匹配；
  - `ImplementationSpecific`：由控制器解释，行为不统一；
- `tls.secretName`：包含 `tls.crt`/`tls.key` 的 Secret；
- 默认后端：没有匹配规则时的兜底。

路径优先级通常遵循“最长匹配优先”，但不同控制器有差异，跨控制器迁移时要验证。

### 3.2 Ingress 与 Service 的关系

```text
客户端 → Ingress Controller（Pod/负载均衡器）
        → Ingress 规则匹配
        → Service（ClusterIP）
        → EndpointSlice
        → Pod
```

常见问题：

- Ingress Controller 未安装/未就绪；
- `ingressClassName` 拼错，规则被忽略；
- Service 名称/端口错误；
- NetworkPolicy 阻止了 Controller 到 Pod 的流量；
- TLS Secret 不存在或证书与域名不匹配；
- 控制器注解（rewrite、body size、timeout）与当前控制器不兼容。

```bash
kubectl get ingressclass
kubectl get ingress -n prod
kubectl describe ingress web -n prod
kubectl logs -n ingress-nginx deploy/ingress-nginx-controller
```

### 3.3 Ingress 与 Gateway API

Gateway API 是更现代的路由 API，区分了基础设施提供者（GatewayClass/Gateway）和路由所有者（HTTPRoute 等），表达力更强。新平台可以评估 Gateway API；已有 Ingress 不必盲目迁移，但要关注控制器支持与团队熟悉度。

## 4. 零停机发布清单

### 4.1 应用侧

- [ ] 启动后先完成初始化，再通过 readiness；
- [ ] 处理 `SIGTERM`，停止接收新请求并排空在途请求；
- [ ] 数据库迁移向前兼容（先加字段、后读写切换、最后清理）；
- [ ] 新旧版本可同时运行，避免共享状态不兼容；
- [ ] 健康检查不依赖不稳定的下游。

### 4.2 Kubernetes 侧

- [ ] `maxUnavailable: 0` 或根据容量设置；
- [ ] `minReadySeconds` 避免刚就绪就被判定稳定；
- [ ] `terminationGracePeriodSeconds` 大于排空时间；
- [ ] `preStop` 留出 Endpoint 传播时间；
- [ ] PodDisruptionBudget 防止自愿驱逐导致容量不足；
- [ ] 集群有足够资源容纳 surge Pod；
- [ ] Service selector 稳定，不要频繁改标签。

### 4.3 流量侧

- [ ] Ingress/负载均衡健康检查指向 readiness 端点；
- [ ] 连接 draining 时间与后端保持一致；
- [ ] 灰度/金丝雀有独立指标与回滚开关；
- [ ] 发布期间监控错误率、延迟、Pod 重启。

## 5. 灰度与蓝绿

### 5.1 蓝绿

部署两套 Deployment（蓝色稳定、绿色新版本），通过切换 Service selector 或 Ingress 后端一次性切流。

优点：切换快、回滚快。
缺点：需要双倍资源；数据库兼容性要求高；切换瞬间所有流量进入新版本，风险集中。

### 5.2 金丝雀

保留稳定版本，同时创建小规模新版本 Deployment，通过：

- 两个 Service + Ingress 按权重/Header 分流（取决于控制器）；
- 服务网格（Istio、Linkerd）按比例分流；
- 网关/负载均衡器加权。

逐步扩大流量，观察错误率、延迟和业务指标，再全量。

### 5.3 不要用同一个 Deployment 做“部分副本新版本”

Deployment 的所有 Pod 使用同一个 Pod 模板。手工改某个 Pod 的镜像不会持久，也会被控制器替换。灰度应使用独立 Deployment/Service 或专门的发布系统。

## 6. 排障路径

```text
Ingress 是否有地址？
  └─ 控制器是否运行、Service 是否存在
       └─ Service 是否有 EndpointSlice？
            └─ Pod 是否 Ready、selector 是否匹配
                 └─ 容器端口/targetPort 是否正确
                      └─ 应用是否监听 0.0.0.0
                           └─ NetworkPolicy/安全组是否放行
```

常用命令：

```bash
kubectl get ingress,svc,endpointslice,pod -n prod -o wide
kubectl describe ingress web -n prod
kubectl describe svc web -n prod
kubectl get pod -n prod -l app.kubernetes.io/name=web
kubectl logs -n ingress-nginx deploy/ingress-nginx-controller --tail=200
kubectl port-forward -n prod svc/web 8080:80
```

`port-forward` 绕过 Ingress，可直接验证 Service/Pod 是否正常；如果 `port-forward` 正常但域名访问失败，问题更可能在 Ingress/DNS/入口层。

## 7. 小结

Deployment 负责副本与滚动策略，Service 负责稳定的四层访问与 Endpoint 管理，Ingress 负责七层域名/路径路由。零停机的关键不在 YAML 数量，而在**readiness、优雅终止、滚动参数、容量和流量切换**是否协同。发布前用清单逐项验证，发布后用指标确认，而不是只看 Pod 是否 `Running`。
