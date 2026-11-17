---
title: Kubernetes 架构、核心对象与 Pod 生命周期
domain: Kubernetes
level: 入门到进阶
tags: [Kubernetes, 架构, Pod, 探针, 生命周期]
last_updated: 2026-11-11
---

# Kubernetes 架构、核心对象与 Pod 生命周期

> 一句话结论：Kubernetes 是一个**声明式、最终一致**的控制系统；你提交的是期望状态，控制器不断把实际状态拉回期望状态。理解这一点，比记住多少 `kubectl` 命令更重要。

## 1. 整体架构

```text
                         ┌─────────────────────────────┐
                         │         Control Plane       │
                         │                             │
 kubectl / CI / Operator │  kube-apiserver  ◄──► etcd │
        │                │        ▲                    │
        └───────────────►│        │                    │
                         │  kube-scheduler             │
                         │  controller-manager         │
                         └────────────┬────────────────┘
                                      │ watch / 上报
                    ┌─────────────────┼─────────────────┐
                    │                 │                 │
              ┌─────▼─────┐     ┌─────▼─────┐     ┌─────▼─────┐
              │  Node 1   │     │  Node 2   │     │  Node 3   │
              │ kubelet   │     │ kubelet   │     │ kubelet   │
              │ containerd│     │ containerd│     │ containerd│
              │ kube-proxy│     │ kube-proxy│     │ kube-proxy│
              │ CNI       │     │ CNI       │     │ CNI       │
              └───────────┘     └───────────┘     └───────────┘
```

### 1.1 控制面

| 组件 | 职责 |
| --- | --- |
| `kube-apiserver` | 唯一与 etcd 直接交互的组件；认证、授权、准入、REST API |
| `etcd` | 集群状态存储；强一致键值库，必须备份 |
| `kube-scheduler` | 为新 Pod 选择节点（过滤 + 打分） |
| `kube-controller-manager` | 运行各种控制器（Deployment、Node、EndpointSlice 等） |
| `cloud-controller-manager` | 对接云厂商的负载均衡、节点、存储等能力 |

### 1.2 节点

| 组件 | 职责 |
| --- | --- |
| `kubelet` | 管理本节点 Pod，调用容器运行时，执行探针，上报状态 |
| 容器运行时 | 实际拉镜像、创建容器（containerd、CRI-O 等） |
| `kube-proxy` | 实现 Service 的虚拟 IP/负载均衡（iptables、IPVS 或替代方案） |
| CNI 插件 | 为 Pod 分配 IP、配置网络与网络策略 |
| CSI/设备插件 | 存储卷、GPU 等扩展资源 |

### 1.3 声明式与调谐

```text
期望状态（YAML） → apiserver 写入 etcd
                         │
                         ▼
                  控制器 watch 变化
                         │
                         ▼
              比较实际状态，采取动作
                         │
                         ▼
                  更新 status / 创建资源
```

这解释了为什么：

- `kubectl apply` 后 Pod 不会“瞬间”出现；
- 删掉由 Deployment 管理的 Pod，ReplicaSet 会自动补一个；
- 修改 Deployment 镜像会触发滚动更新；
- 手工改 Pod 的某些字段不会持久化，需改上层控制器。

## 2. 核心对象关系

```text
Deployment
   └── ReplicaSet
          └── Pod
                 ├── Container
                 ├── Volume
                 └── ServiceAccount

Service ──► EndpointSlice ──► Pod IP
Ingress ──► Service

StatefulSet ──► Pod（稳定网络标识 + PVC 模板）
DaemonSet   ──► 每个/部分节点一个 Pod
Job/CronJob ──► 一次性/定时任务
```

常用命令：

```bash
kubectl api-resources
kubectl explain deployment.spec
kubectl get deploy,rs,pod,svc,ep -o wide
kubectl get pod -o yaml
kubectl describe pod <pod>
```

## 3. Pod 是什么

Pod 是 Kubernetes 调度的最小单位，包含一个或多个共享网络命名空间、存储卷的容器。

- 同一 Pod 内容器共享 IP 和端口空间，可通过 `localhost` 通信；
- 容器共享挂载的 Volume；
- Pod 是**临时**的：节点故障、驱逐、滚动更新都可能重建，IP 会变化；
- 不要把有状态数据只放在容器文件系统里；
- 一个 Pod 通常放“紧密协作”的容器（主容器 + sidecar），而不是把多个无关服务塞在一起。

### 3.1 最小 Pod

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: demo
  labels:
    app: demo
spec:
  containers:
    - name: app
      image: nginx:1.27-alpine
      ports:
        - containerPort: 80
      resources:
        requests:
          cpu: 100m
          memory: 128Mi
        limits:
          cpu: 500m
          memory: 256Mi
```

生产环境通常不直接创建裸 Pod，而是用 Deployment/StatefulSet/DaemonSet 管理。

### 3.2 Init 容器与 Sidecar

- **Init 容器**：按顺序执行，全部成功后才启动业务容器；适合初始化配置、等待依赖、下载模型；
- **Sidecar**：与主容器并行，代理流量、收集日志、同步配置；
- 较新版本支持“可重启的 Init 容器”作为原生 Sidecar，具体可用性取决于集群版本与特性门控；
- Init 容器失败会阻止 Pod 启动，需查看其日志与状态。

## 4. Pod 生命周期

### 4.1 阶段（Phase）

| 阶段 | 含义 |
| --- | --- |
| `Pending` | 已创建，但尚未调度或镜像/卷未就绪 |
| `Running` | 已绑定节点，至少一个容器在运行或正在启动 |
| `Succeeded` | 所有容器成功退出，且不会重启 |
| `Failed` | 所有容器已退出，至少一个失败 |
| `Unknown` | 无法获取状态（通常节点通信异常） |

注意：`Running` 不等于“业务可用”。容器可能正在启动、探针未通过、端口未监听。

### 4.2 Conditions

```bash
kubectl get pod <pod> -o jsonpath='{range .status.conditions[*]}{.type}={.status} {.reason}{"\n"}{end}'
```

常见条件：

- `PodScheduled`：是否已调度；
- `Initialized`：Init 容器是否完成；
- `ContainersReady`：所有容器是否就绪；
- `Ready`：Pod 是否可接收流量；
- `DisruptionTarget`：是否因驱逐/抢占而终止。

### 4.3 容器状态

```bash
kubectl get pod <pod> -o jsonpath='{.status.containerStatuses[*].name}{" "}{.status.containerStatuses[*].state}{"\n"}'
```

- `Waiting`：等待镜像、卷、配置等；
- `Running`：运行中；
- `Terminated`：已退出，含 `exitCode`、`reason`、`startedAt`、`finishedAt`；
- `restartCount`：重启次数，是判断 CrashLoop 的重要指标。

### 4.4 重启策略

```yaml
spec:
  restartPolicy: Always   # Always / OnFailure / Never
```

- `Always`：容器退出就重启，适合长期服务；
- `OnFailure`：非零退出才重启；
- `Never`：不重启，适合一次性任务。

Deployment 的 Pod 通常使用 `Always`；Job 常用 `OnFailure` 或 `Never`。

## 5. 探针：让平台知道“活着”和“可接流量”

```yaml
containers:
  - name: app
    image: registry.example.com/app:1.2.3
    ports:
      - containerPort: 8080
    startupProbe:
      httpGet:
        path: /healthz
        port: 8080
      periodSeconds: 2
      failureThreshold: 30
    readinessProbe:
      httpGet:
        path: /readyz
        port: 8080
      periodSeconds: 5
      failureThreshold: 3
    livenessProbe:
      httpGet:
        path: /healthz
        port: 8080
      periodSeconds: 10
      failureThreshold: 3
```

### 5.1 三种探针

| 探针 | 失败后果 | 用途 |
| --- | --- | --- |
| `startupProbe` | 重启容器 | 给慢启动应用更长的启动时间 |
| `readinessProbe` | 从 Service EndpointSlice 摘除 | 控制是否接流量 |
| `livenessProbe` | 重启容器 | 检测不可恢复的卡死 |

### 5.2 常见错误

1. **liveness 检查依赖下游**：数据库抖动导致所有 Pod 被重启，雪崩。liveness 只检查进程自身能否响应。
2. **readiness 检查过于简单**：端口通就绪，但应用还没加载完配置，导致 502。
3. **没有 startupProbe**：JVM/大模型启动慢，liveness 先超时，陷入重启循环。
4. **超时太短**：GC 抖动就失败，频繁摘流/重启。
5. **探针路径有副作用**：每次探测都写数据库或触发重计算。

推荐拆分：

- `/healthz`：进程存活，返回 200；
- `/readyz`：依赖就绪（数据库连接、缓存、配置加载），可接流量；
- `/startupz`：启动完成。

## 6. 优雅终止与零停机

删除 Pod 时，大致过程：

```text
Pod 标记 Terminating
   │
   ├─► EndpointSlice 标记不 Ready（与后续步骤并行，传播有延迟）
   ├─► 执行 preStop Hook
   ├─► 发送 SIGTERM
   ├─► 等待 terminationGracePeriodSeconds
   └─► SIGKILL
```

配置：

```yaml
spec:
  terminationGracePeriodSeconds: 60
  containers:
    - name: app
      lifecycle:
        preStop:
          exec:
            command: ["/bin/sh", "-c", "sleep 10"]
```

要点：

- 应用要处理 `SIGTERM`：停止接收新请求、等待在途请求完成、关闭连接池；
- `preStop` 的 `sleep` 用于等待 Endpoint 传播，但时间要基于实测；
- `terminationGracePeriodSeconds` 必须大于 `preStop` + 业务排空时间；
- 滚动更新时 `maxUnavailable`/`maxSurge` 与 readiness 配合，避免容量不足；
- 长连接（WebSocket、gRPC 流）需要应用层主动断开或迁移。

## 7. 标签、选择器与命名空间

```yaml
metadata:
  name: web
  namespace: prod
  labels:
    app.kubernetes.io/name: web
    app.kubernetes.io/instance: prod-web
    app.kubernetes.io/version: "1.2.3"
  annotations:
    example.com/owner: team-a
```

- **Labels** 用于选择和分组，Service、Deployment、NetworkPolicy 都依赖它；
- **Annotations** 存放非标识性元数据，供工具/控制器使用；
- 选择器一旦写错，Service 可能没有 Endpoint，Deployment 可能管理到错误的 Pod；
- 使用一致的标签规范（如 `app.kubernetes.io/*`）能减少运维事故。

```bash
kubectl get pods -l app.kubernetes.io/name=web
kubectl get pods --show-labels
```

## 8. 安全上下文与 Pod Security

```yaml
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
  fsGroup: 10001
  seccompProfile:
    type: RuntimeDefault
containers:
  - name: app
    securityContext:
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      capabilities:
        drop: ["ALL"]
```

建议：

- 默认非 root；
- 关闭权限提升；
- 只读根文件系统，需要写入的目录挂 EmptyDir/PVC；
- 删除不必要的能力；
- 使用 Pod Security Standards（`baseline`/`restricted`）在命名空间级别约束；
- 不要把 `hostNetwork`、`hostPID`、`privileged` 当作解决网络/调试问题的默认手段。

## 9. 常用排障入口

```bash
kubectl get pod -o wide
kubectl describe pod <pod>
kubectl logs <pod> --all-containers=true
kubectl logs <pod> -c <container> --previous
kubectl exec -it <pod> -- sh
kubectl get events --sort-by=.lastTimestamp
kubectl top pod
```

排查顺序：

1. Pod 是否调度（`Pending` 看 Events）；
2. 镜像是否拉取成功；
3. Init 容器是否完成；
4. 容器是否反复退出（`CrashLoopBackOff`）；
5. 探针是否失败；
6. Service/Endpoint 是否正常；
7. 网络/DNS/Ingress 是否可达。

## 10. 小结

Kubernetes 的核心是“期望状态 + 控制器调谐”。Pod 是最小调度单元但不可长期依赖，真正稳定的抽象是 Deployment、Service、StatefulSet 等上层对象。把探针、优雅终止、资源声明和安全上下文设计好，比事后排障更重要。
