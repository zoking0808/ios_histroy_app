# 独立 iOS 历史应用 API

Base URL：`PUBLIC_ORIGIN + /api/v1`。本机默认入口：`http://localhost:8088/`；公网必须HTTPS。

## 用户流程

搜索/选择应用版本 → 输入自己的Apple ID与密码 → 创建下载任务 → Apple要求时提交验证码 → 查询进度 → 获取下载链接。

支持两种模式：

- `official`：获取Apple官方原始包链接，匿名HEAD/小范围请求检查可访问性；不下载或保存IPA，仅在内存保留链接记录15分钟。Apple决定实际链接有效期。原始包可能仍需补写账号授权数据才能安装，不能与本站完整IPA视为相同。
- `package`：沿用已工作的完整IPA流程，服务端临时下载、校验、写入账号SINF等数据；文件保留1小时。下载接口支持单段Range与HEAD。

已购买的付费应用可使用已有许可，不按价格统一阻止；本站不会自动购买或申请新许可。Apple返回许可/版本不可用时不能直接推断用户未购买。

## 会话与加密

1. `GET /session` 获取 `csrfToken`、PEM格式 `publicKey`、可用 `modes` 等信息，并接收 会话Cookie（HTTPS为`__Host-ios-history`，HTTP localhost为`ios-history-local`）。
2. 保存并带上Cookie。HTTPS下具有Secure、HttpOnly、SameSite=Strict、Path=/属性；仅HTTP localhost开发模式省略Secure并使用不同Cookie名称。POST/DELETE还必须发送 `Origin: <PUBLIC_ORIGIN>` 和 `X-CSRF-Token`。
3. 浏览器生成32字节AES密钥、12字节nonce，以AES-256-GCM加密UTF-8 JSON，AAD为csrfToken的UTF-8字节；密文含GCM标签。
4. 用返回的RSA公钥，采用RSA-OAEP/SHA-256加密AES密钥。三个二进制字段均转无padding的Base64URL：`wrappedKey`、`nonce`、`ciphertext`。
5. 登录明文结构为 `{account, password, createdAt}`；验证码结构为 `{code, createdAt}`。createdAt是当前毫秒Unix时间，允许120秒偏差。**不要把明文账号密码放进请求查询串、日志或普通JSON字段。**

网页实现参考 `web/assets/download.js` 的 `envelope()`，不是外部加密服务。

## 接口

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/apps?q=名称或ID&country=cn` | 公开应用搜索/商店链接解析 |
| GET | `/versions/{appId}` | 公开历史版本查询 |
| GET | `/health` | 非敏感服务状态/并发上限 |
| GET | `/session` | 建立/续期浏览器会话，获取CSRF和加密公钥 |
| GET | `/jobs` | 仅列出当前会话所属任务 |
| POST | `/jobs` | 创建任务；JSON含appId、versionId、mode、consent=true、envelope |
| GET | `/jobs/{id}` | 当前所属任务的状态、进度、错误类别、文件名 |
| POST | `/jobs/{id}/code` | 提交验证码；JSON含envelope，与首次登录复用认证进程/Cookie/SAP |
| POST | `/jobs/{id}/link` | 请求下载地址；JSON可为`{}` |
| DELETE | `/jobs/{id}` | 取消任务或清除已生成文件/链接 |
| GET/HEAD | `/download/{id}?expires=...&signature=...` | 完整IPA下载；要求所属会话Cookie和有效签名 |

任务状态：`authenticating`、`awaiting_code`、`downloading`、`packaging`、`ready`、`failed`、`cancelled`。仅在`awaiting_code`时提交验证码，不能在登录前自行发送验证码。

任务状态和link响应包含 `fileName`，格式为“应用名_版本号.ipa”。完整包从实际Info.plist读取版本，并通过Content-Disposition/UTF-8 filename传给浏览器；官方链接仅附建议文件名，跨域下载的实际名称由Apple响应和浏览器决定。

`/link`返回：`url`、`mode`、`fileName`、`expiresAt`、`appleExpiryUnknown`。

- 完整包URL属于当前配置的站点，绑定当前浏览器会话；不能作为无鉴权的公开分享链接。
- 官方URL直接属于Apple，复制后可由下载工具请求；持有该临时链接者可能可以取回原始包，用户应自行保管。`expiresAt`在此模式仅指本站记录的保留截止时间，`appleExpiryUnknown=true`。

## 限额、数据和异常

- 全站最多1个处理中任务；每会话/IP最多6次创建/小时。鉴权阶段10分钟，完整下载处理阶段30分钟；超时清理。
- 完整包上限2GB，文件队列容量约8GB，开始任务前要求至少8GB磁盘余量。官方直链不受本站IPA存储限制。
- 失败任务保留短时间状态供查看；密码、验证码和临时Apple会话在任务结束时清理。官方链接不落盘；重启后需重新生成。
- 400参数/加密错误；401会话过期；403来源/CSRF/签名错误；404无任务或不属于该会话；409任务忙/状态不匹配；410过期；413请求过大；429频率限制；502Apple或下载服务未返回可用结果；503维护/容量不足。
- 新版日志仅记录App ID、版本ID、错误类别及白名单HTTP元信息，不记录密码、Cookie值、验证码、Apple令牌或完整官方链接。
- `DOWNLOAD_LICENSE_UNCONFIRMED`表示Apple当前会话没有返回许可；`DOWNLOAD_AUTH_EXPIRED`表示下载会话失效；`DOWNLOAD_UNAVAILABLE`表示未得到所选版本。204/404空响应不等于验证码错误。

这些接口不是Apple官方开放API；它们封装本站使用的ipatool/Pastel兼容实现。Apple的私有协议、账户状态、地区、版本可用性仍会影响结果。

`GET /api/v1/apps` 的 `country` 为单个小写地区代码，默认 `cn`。支持代码：`cn, hk, tw, us, jp, sg, kr, mo, my, th, vn, ph, id, in, pk, au, nz, gb, de, fr, it, es, pt, nl, be, ch, at, se, no, dk, fi, ie, pl, cz, gr, ro, hu, tr, ua, ru, ca, mx, br, ar, cl, co, pe, ae, sa, il, qa, kw, eg, za, ng, ke, ma`。不接受多地区参数；未知代码返回400。各地区使用独立缓存，返回的 `country` 与商店链接均对应本次选择。地区支持和应用上架情况由 Apple 决定。参考：[Apple 服务可用地区](https://support.apple.com/118205)。
