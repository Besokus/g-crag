# term-extractor-test 单 Pod 演示部署

目标固定为 `prod-ai2 / nmt-llm / Deployment term-extractor-test`。执行入口：

```sh
./deploy/k8s/single-pod/deploy.sh
```

脚本会在运行时从本地 Git `origin` 解析 Kaniko 源码地址，不把仓库所有者写入部署文件；如需覆盖，
可显式设置 `SOURCE_GIT_CONTEXT`。镜像 OCI source label 默认是 `unknown`，需要登记源码地址时再设置 `SOURCE_URL`。

脚本会验证 kube context 和 revision 107/0 副本基线，保存原 Deployment 快照，
通过 ConfigMap overlay + Kaniko 并行构建镜像；公开基础镜像先经 Daocloud 代理同步到
内部 Harbor。前端、后端及 7 类基础组件最终都固定到 Harbor digest。脚本随后创建
50Gi RWX PVC 和 Secret（管理员登录为 `admin` / `admin123`，其余凭据随机），发布 10 容器单 Pod，运行幂等 Seed，并完成健康、端点、
SSE 问答和 Recreate 持久性检查。Ollama 显式禁用 GPU 可见性，只使用 CPU。

运行期只有 Deployment 的一个 Pod；Kaniko、Seed 和验收 Pod 都是临时 Job/Pod。
Secret 值只在本地生成并直接提交给 API Server，不写入仓库、不打印。

失败回滚：脚本收集诊断后会恢复执行前保存的 revision 107 规格并保持 replicas=0。
PVC 与 Secret 会保留，避免误删演示数据。
