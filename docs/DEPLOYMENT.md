# 部署与迁移

## 本机与跨系统

整个目录复制到另一台机器，安装Docker/Compose后执行 `docker compose up -d --build` 即可。不要复制`.runtime`、运行数据卷或旧网站的凭据来“补齐依赖”。默认构建适配当前Linux容器架构。

需要单独构建两个架构时：

```sh
docker buildx build --platform linux/amd64 --load -t ios-history-app:amd64 .
docker buildx build --platform linux/arm64 --load -t ios-history-app:arm64 .
```

若发布到自己的镜像仓库，可用 `--platform linux/amd64,linux/arm64 --push -t <你的仓库>:<版本>`。本次没有推送镜像或部署到外部服务器。不同架构的本地加载/模拟支持依赖Docker安装方式，参见 [Docker多平台文档](https://docs.docker.com/build/building/multi-platform/)。

## HTTPS域名

示例`.env`：

```dotenv
HTTP_PORT=8088
BIND_ADDRESS=127.0.0.1
PUBLIC_ORIGIN=https://ios.example.com
REAL_IP_TRUSTED_CIDR=127.0.0.1/32
MIN_FREE_GB=8
```

由宿主机上已配置证书的Nginx代理至本机端口，示例片段：

```nginx
location / {
    proxy_pass http://127.0.0.1:8088;
    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto https;
    proxy_buffering off;
    proxy_read_timeout 90s;
}
```

该片段放在你自己的HTTPS server中，证书与域名由部署环境管理。所有API/页面应使用同一`PUBLIC_ORIGIN`。不要把PUBLIC_ORIGIN设成普通HTTP公网地址，应用会拒绝该配置。

Docker网关只信任`REAL_IP_TRUSTED_CIDR`中的反向代理转发IP。公网多用户场景需要把它设为容器实际看到的可信代理源地址/CIDR；不要设为`0.0.0.0/0`。未正确设置时仍可连接，但多用户可能共享同一IP限额。

## 数据与更新

- `app_data`：服务密钥、完整包和非敏感完成任务信息，访问权限受限。
- `device_data`：稳定设备GUID。
- `sap_cache`：经校验的签名公共资源。
- 官方链接仅在内存保留15分钟，容器重启后需重新生成；Apple实际URL有效期由Apple决定。
- 密码/验证码不作为配置持久化，未完成的认证任务不能跨重启恢复。

先阻止新任务：

```sh
docker compose exec app touch /data/maintenance
```

查看`/api/v1/health`中的activeJobs，并等待已有下载传输结束后更新：

```sh
docker compose up -d --build
docker compose exec app rm /data/maintenance
```

备份数据卷时，它包含私钥和个人安装包，应作为私密备份管理。停止服务使用 `docker compose down`；只有明确要清空所有数据时才使用`down -v`。

## 外部依赖与网络

构建需要访问Docker仓库、npm、Go模块源。运行需要访问Apple商店/下载/签名资源服务及公开版本索引。Docker解决系统运行环境差异，不解决网络封锁、账号风控或Apple撤下历史包的问题。

如需代理，用自己的Compose override为app添加代理环境变量；Windows/macOS可根据Docker Desktop配置使用`host.docker.internal`，Linux需配置实际可达的代理地址。不要关闭TLS证书验证，也不要把代理账号或Apple账号写进提交到版本库的文件。
