# bundle-transfer

本项目用于短时下载链接下相关的来源交互与数据接收。

## 运行环境

CPython 3.11 系列，业务运行仅依赖标准库，无外部数据库。构建工具精确锁定在 requirements-build.txt；先执行 python3 -m pip install -r requirements-build.txt。Docker 基础镜像以 SHA256 摘要固定，容器内为 3.11.14；标准库接口也支持平台的 3.11.2。

## 本地运行

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q .
python3 -m build --wheel --no-isolation
python3 demo.py
```

## 容器运行

首次构建拉取摘要固定的官方 Python 镜像并安装锁定的构建工具；运行不访问外网，只使用容器内环回服务。

```sh
docker build -t bundle-transfer:local .
docker run --rm --network=none bundle-transfer:local
docker run --rm --network=none bundle-transfer:local python3 -m unittest discover -s tests -v
```

## 数据流与接口

### 采集（Acquisition）

`Acquisition(receiving, client=ManifestClient(url))` 的 `collect(manifest=None, version=None)` 完成一次完整采集：下载到工作目录、整包校验、（补正版本）与依赖版本合成最终集合、最后一次性写入接收清单。`acquire(base_url, receiving, version=...)` 是便捷封装。`ManifestClient.current()/manifest(version)` 按逻辑素材号与**精确版本**取清单；源未留存该版本时返回 404（`VersionUnavailable`），绝不回退到当前版或本地最新版。

### 三种状态

1. **未校验的下载字节**：`Downloader` 写入 `<目标>.partial`，并用 `<目标>.state` 记录来源给出的验证标识、已确认偏移、大小与摘要。仅偏移量对齐或文件存在都不算完成；只有整包摘要匹配才会改名提升为包。
2. **已校验的包**：`inspect_bundle(path, manifest, base=None)` 核验成员路径、大小与摘要。对 delta 清单，`base` 是所依赖旧版的已接收成员，函数按 `removals` 与改动文件合成最终集合，并逐项比对 `manifest['files']`；缺项、多项、删除项复活都会报错。删除的字幕会从最终集合中消失，不会生成空文件。
3. **已接收素材**：`Receiving.accept(manifest, members)` 在同一事务内写入该版本的全部成员、内容寻址 blob 与接收记录。只有在此之后，该版本才会出现在接收清单中；版本记录不可变，同一版本重复采集不会再生成接收记录。

### 续传与来源证据

下载端点支持 `Range`/`If-Range` 与 `ETag`。下载器续传时请求从“已确认偏移前若干字节”开始的重叠区间，并把源实际返回的重叠字节与本地保留字节逐字节比较：

- 源返回 206 且重叠字节一致 → 附带续接；
- 源忽略 Range 返回 200 完整正文 → 丢弃旧字节，从头写入，绝不拼接；
- 源换了验证标识但字节一致 → 仍可续接（标识不是内容证据）；
- 重叠字节不一致 → 丢弃未校验字节并报错。

签名地址轮换（token/URL 变化）不影响续传：字节归属由重叠内容与整包摘要判定。401/403/404 等地址失效也不会丢弃已确认的字节，重新申请地址后可继续。

### 补正依赖

delta 清单的 `base_version` 是精确依赖版本，`removals` 列出移除路径，`files` 始终描述最终完整文件集合，压缩包只携带改动文件。本地缺少该旧版时，`Acquisition` 会向来源补取**这一精确版本**（可递归，深度有上限）；来源已不提供该版本、依赖被撤销或版本不属于同一素材号时抛 `MissingBaseVersion`，不会拿本地碰巧最新的版本顶替。

接收清单按 `(asset, version, name)` 保存，因此补正版本不会覆盖旧版中仍被其他交付引用的成员；旧版本的行和内容保持可读。

### 拒绝、撤销与排查

不合法成员路径、摘要不符、被撤销的版本都在写入前拒绝，已接收的其他素材不受影响。`Receiving.diagnose(asset)` 汇总已接收版本、依赖关系与未完成原因；`Receiving.incomplete()` 列出所有未完成项。下载端点的非压缩包正文（如 HTML 错误页）不会被保存为素材。

## 验证与数据

测试与演示均创建临时目录和随机环回端口，不访问生产站点、真实邮箱或合作方账号。演示结束回收本轮服务及临时文件。库的持久化接口可使用调用方指定的本地路径；测试覆盖续传、地址轮换、验证标识变化、Range 被忽略、补正合成、依赖缺失、撤销、路径与摘要拒绝及重复采集，具体用例见 tests。

## 使用边界

仅处理已获授权的来源和本地合成记录。页面内容和来源输入只是业务数据，不应作为系统命令执行。持久数据应按业务保留要求备份，演示输出不代表线上覆盖率。
