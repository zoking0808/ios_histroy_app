# 验证记录

2026-09-20，独立项目迁移验证：

- Docker Desktop 下构建 Linux arm64、amd64；两种架构各通过原有20项 Python 测试，Go认证桥测试通过。
- 两种架构的 `ios_download.py --prepare-auth` 均成功完成 SAP 签名握手，没有提交 Apple ID 或密码。
- 独立服务的健康检查、微信应用查询、230条历史版本记录与美区 ChatGPT 搜索获得真实上游响应。
- 桌面页面及微信查询已在浏览器检查。Windows宿主机及真实iOS设备安装未经代理测试。
- 用户反馈独立项目可以使用；代理未将此描述为自行完成真实账号登录或安装测试。

单地区扩展：界面扩充57个可选商店，仍使用单个 `country` 参数。地区切换回归覆盖上游参数、商店链接和各地区缓存隔离。不新增多地区合并查询，未改动账号登录、验证码与下载流程。

复查命令：

```sh
docker compose exec -T app python3 -m unittest discover -s tests -v
docker compose exec -T app python3 ios_download.py --prepare-auth
```

外部服务返回受实时网络和上游状态影响；上述结果是验证时的状态。

本次扩展完成后：21项 Python 测试通过，Docker 服务健康；6个新增地区真实查询成功。浏览器英国→韩国切换成功，390px手机布局无横向溢出；历史版本下载按钮正确填入应用与版本ID，登录前不显示验证码。

开源整理验证：删除 vendor 后，使用固定GitHub提交和SHA256重新构建Linux arm64镜像成功；24项测试通过（含下载校验、损坏缓存拒绝、地区隔离、认证与下载合成流程）。自有代码使用MIT，第三方许可证独立保留。此轮未发布GitHub或更改线上服务器。
新镜像的Apple SAP签名握手通过，未提交Apple ID或密码。
