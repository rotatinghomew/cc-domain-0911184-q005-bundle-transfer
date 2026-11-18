# bundle-transfer

本项目用于短时下载链接下相关的来源交互与数据接收。

## 运行环境

CPython 3.11 系列，仅依赖标准库；没有需要安装的 Python 包或外部数据库。Docker 基础镜像以 SHA256 摘要固定，容器内为 3.11.14；标准库接口也支持平台的 3.11.2。

## 本地运行

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q .
python3 demo.py
```

## 容器运行

本地需已存在 `python:3.11.14-slim-bookworm` 镜像。构建不下载依赖，运行只访问容器内环回服务。

```sh
docker build --network=none -t bundle-transfer:local .
docker run --rm --network=none bundle-transfer:local
docker run --rm --network=none bundle-transfer:local python3 -m unittest discover -s tests -v
```

## 数据流与接口

GET /manifest 返回 asset、version、revoked、url、size、sha256 和 files。下载端点支持 bytes=start-end 及 If-Range，响应含 ETag；验证标识不一致会返回完整 200。BundleSource 的 token、ignore_range、files 与 revoked 可控制地址轮换及清单变化。当前 Downloader 每次下载完整包，先验全包后改名；inspect_bundle 核验成员路径和摘要，Receiving 接收已经核验的成员。临时文件不表示成功接收。

清单的 mode 为 full 或 delta。delta 的 base_version 是精确依赖版本，removals 列出移除路径，files 始终描述最终完整文件集合；补正压缩包只携带改动文件。BundleSource 可配置 final_files 表达最终集合，当前接收流程只支持 full。版本查询约定为 GET /manifest?version=整数，尚未留存的版本返回 404，不以当前版替代。

本地来源调用 retain() 留存当前版本后，可改变 version、token 和 files 发布下一版，旧版清单和下载内容继续独立提供；这只是来源保留能力，不会自动采集或拼合补正版本。

## 验证与数据

测试与演示均创建临时目录和随机环回端口，不访问生产站点、真实邮箱或合作方账号。演示结束回收本轮服务及临时文件。库的持久化接口可使用调用方指定的本地路径；测试覆盖正常采集、证据身份和来源协议边界，具体用例见 tests。

## 使用边界

仅处理已获授权的来源和本地合成记录。页面内容和来源输入只是业务数据，不应作为系统命令执行。持久数据应按业务保留要求备份，演示输出不代表线上覆盖率。
