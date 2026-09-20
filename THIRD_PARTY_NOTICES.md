# 第三方来源与许可证

本项目自有代码使用根目录 MIT 许可；以下依赖继续遵循各自原许可。本声明与 `licenses/` 一并包含在 Docker 镜像中。

## Pastel-macOS

- 上游：https://github.com/EEliberto/Pastel-macOS
- 固定提交：`20934736051bfdcfdd6362920fbf04c249ce1af5`
- Apache-2.0，完整许可见 `licenses/pastel.txt`。
- `dependencies/pastel.json` 固定所需文件的 SHA256；构建时从该提交获取，仓库不附带源码。
- 运行时副本修改包括私密curl传输、认证诊断、下载跳转检查与大小限制；修改过的文件带有修改说明。桥接代码及本地补丁见 `bridge/` 与 `ios_download.py`。

## ipatool

- 上游：https://github.com/majd/ipatool
- 固定提交：`e5211d6fd9507c45ade787ede6a9d64596327fbe`
- MIT，Copyright (c) 2021 Majd Alfhaily，完整许可见 `licenses/ipatool.txt`。
- `dependencies/ipatool.json` 固定归档 SHA256；构建时获取源码归档，仓库不附带源码或压缩包。
- 在构建副本中调整缓存路径，加入 `bridge/ipatool-auth.go` 与 `bridge/ipatool-download-info.go`；运行镜像保留许可证。

## 构建与再分发

Node依赖由上游package-lock固定，Go依赖由go.mod/go.sum固定，基础镜像固定版本与digest。依赖的版权声明和许可证不因本项目采用MIT而被替代。运行时获取的Apple公共签名资源与Unicorn库继续使用各自许可；账号数据不作为项目源码分发。

鸣谢不能代替依赖许可要求。参考：[MIT](https://opensource.org/license/mit)、[Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0)。
